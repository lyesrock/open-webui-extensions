"""
title: Corriger sur le web
author: lyesrock
version: 1.1.0
description: Recherche le web en arrière-plan puis corrige la réponse cliquée EN PLACE (aucun rapport, aucun nouveau message) ; le texte faux est SUPPRIMÉ et le remplacement marqué **[corrigé](url "Corrigé : faux")** (surligné jaune à l'affichage)
required_open_webui_version: 0.13.0
license: MIT
"""

import asyncio
import json
import logging
import re
import time
import uuid

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Intervalle des battements de progression (rend le travail de l'action visible dans le chat).
_PROGRESS_INTERVAL = 10
_TOAST_EVERY = 20
_MAX_PREFIX_MESSAGES = 120
# Cf. utils/chat_id.py (0.13) : tout chat_id sans ce préfixe est un chat sauvegardé.
_NON_SAVED_CHAT_PREFIXES = ("temporary:", "local:", "channel:")
# Tâches fire-and-forget en cours (référence forte ; le module en cache survit aux requêtes).
_PENDING_TASKS = set()

# Garde-fous anti-paraphrase, mesurés sur le TEXTE VISIBLE (hors URLs/titres de liens
# et hors segments barrés ~~~~) : le balisage de correction triple la longueur brute,
# mais le rendu visible doit rester celui du texte d'origine.
_MIN_RATIO = 0.6     # en dessous : le modèle a résumé au lieu de reproduire
_MAX_RATIO = 1.5     # au-dessus : le modèle a ajouté du commentaire (rapport déguisé)
_MIN_OVERLAP = 0.55  # fraction des mots d'origine devant subsister dans le texte corrigé
_IDENTICAL_BAND = (0.92, 1.08)  # sans aucun marqueur ~~, ratio dans cette bande = verbatim
_RELAXED_BAND = (0.5, 1.6)      # sans marqueur, reproduction fidèle même reformulée à la marge


async def _emit(emitter, payload):
    """Envoie un événement au front-end sans jamais propager d'exception (socket fermée, onglet fermé...)."""
    if emitter is None:
        return
    try:
        await emitter(payload)
    except Exception:
        log.exception("correct-web: échec d'émission de l'événement %s", payload.get("type"))


async def _log(emitter, message, level="info"):
    """Journal persistant affiché sous le message (panneau View Logs)."""
    await _emit(emitter, {"type": "logs", "data": {"logs": [{"message": message, "metadata": {"level": level}}]}})


async def _toast(emitter, ttype, content):
    """Popup visible même si l'utilisateur a quitté la conversation."""
    await _emit(emitter, {"type": "notification", "data": {"type": ttype, "content": content}})


async def _progress_task(emitter, label, interval=None):
    """Battement de statut périodique + toast tous les _TOAST_EVERY secondes."""
    if emitter is None:
        return
    interval = interval or _PROGRESS_INTERVAL
    elapsed = 0
    try:
        while True:
            await asyncio.sleep(interval)
            elapsed += interval
            await _emit(emitter, {"type": "status", "data": {"description": f"{label} ({elapsed} s écoulées)…"}})
            if elapsed % _TOAST_EVERY == 0:
                await _toast(emitter, "info", f"Correction web en cours… {elapsed} s écoulées.")
    except asyncio.CancelledError:
        pass


def _extract_target(body: dict):
    """Retourne (message_id, texte_à_corriger, index_de_la_cible).

    Le front passe dans body['id'] l'id du message assistant cliqué ; repli sur le
    dernier assistant si l'id est absent (vieilles versions).
    """
    messages = body.get("messages") or []
    target_id = body.get("id")
    target_idx = None

    if target_id:
        for idx, message in enumerate(messages):
            if message.get("id") == target_id and message.get("role") == "assistant":
                target_idx = idx
                break
    if target_idx is None:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "assistant":
                target_idx = idx
                break
    if target_idx is None:
        return None, "", -1

    content = str(messages[target_idx].get("content") or "").strip()
    return messages[target_idx].get("id"), content, target_idx


def _build_prefix(messages: list, target_idx: int, max_chars: int) -> tuple:
    """Préfixe de CONTINUITÉ KV cache : messages du front jusqu'à la cible incluse.

    Rejoué à l'identique, le préfixe tokenisé est celui de la conversation d'origine
    jusqu'au message cliqué : le prefix cache du serveur d'inférence est réutilisé.
    Retourne (prefix, tronque).
    """
    prefix = [dict(message) for message in messages[: target_idx + 1]]
    if not prefix:
        return [], False
    truncated = False

    target = prefix[-1]
    content = str(target.get("content") or "")
    if len(content) > max_chars:
        target["content"] = content[:max_chars] + "\n[... texte tronqué pour la correction ...]"
        truncated = True

    if len(prefix) > _MAX_PREFIX_MESSAGES:
        prefix = prefix[-_MAX_PREFIX_MESSAGES:]
        while prefix and prefix[0].get("role") == "assistant":
            prefix = prefix[1:]
        truncated = True

    return prefix, truncated


async def _web_search_readiness(request, model_id: str, search_mode: str):
    """Pré-vol : reproduit les conditions d'injection de la recherche web côté backend."""
    blocking = []
    searchable = None

    try:
        from open_webui.models.config import Config  # pyright: ignore[reportMissingImports]

        searchable = bool(await Config.get("web.search.enable"))
    except Exception:
        try:
            from open_webui import env as owui_env  # pyright: ignore[reportMissingImports]

            if getattr(owui_env, "WEBUI_SEARCH_ENGINE", "") or getattr(owui_env, "RAG_WEB_SEARCH_ENGINE", ""):
                searchable = True
        except Exception:
            pass

    if searchable is False:
        blocking.append(
            "recherche web DÉSACTIVÉE côté instance : Administration > Paramètres > Web Search "
            "(activer l'option, choisir un moteur et la clé API), puis autoriser la permission "
            "'features.web_search' pour l'utilisateur."
        )

    try:
        models_map = getattr(request.app.state, "MODELS", None) or {}
    except Exception:
        models_map = {}
    model = models_map.get(model_id) or {}
    meta = (model.get("info") or {}).get("meta") or {}

    if (meta.get("capabilities") or {}).get("web_search", True) is False:
        blocking.append(
            f"le modèle '{model_id}' déclare la capability 'web_search' = false "
            "(Workspace > Models > modifier > capabilities)."
        )
    if search_mode == "native" and (meta.get("builtinTools") or {}).get("web_search", True) is False:
        blocking.append(
            f"outils web natifs désactivés sur le modèle '{model_id}' (meta.builtinTools.web_search) "
            "— basculer la valve search_mode sur 'legacy'."
        )

    return searchable, blocking


_REGLE_SOURCE = (
    "   - le lien markdown ne contient QUE les mots de remplacement, et pointe vers la page qui "
    "établit le fait — une URL de page de résultats de recherche (contenant « ?q= », « /search » ou "
    "semblable) est INTERDITE comme source ;\n"
)
_REGLE_VALEUR = (
    "   - le remplacement doit être une VALEUR CONCRÈTE énoncée par la source : remplacer par "
    "« N/A », « MàJ », « inconnu », « indisponible » ou vide est INTERDIT — si la source ne "
    "donne pas clairement une valeur différente, LAISSE le texte tel quel ;\n"
)


def _correction_rules(display: str):
    """(règle 3 complète, règle 5 sortie stricte) selon le style d'affichage de la valve
    correction_display. Le pipeline interne (sanitizeurs, validation) accepte et ramène de
    toute façon les DEUX formats : cette consigne ne fait que demander le style choisi."""
    if display == "strikethrough":
        regle3 = (
            "3. Marque CHAQUE correction exactement ainsi :\n"
            "   ~~séquence erronée~~ **[séquence corrigée](URL \"titre de la source\")**\n"
            "   (le double tilde barre la faute, le gras rend le remplacement immédiatement repérable)\n"
            "   - barre UNIQUEMENT les mots faux : 1 à 8 mots, jamais une phrase ou une ligne entière ;\n"
            + _REGLE_SOURCE +
            "   - si la source citée contredit aussi un autre mot de la phrase (un acteur, un lieu, une "
            "quantité), barre-LE aussi : corrige TOUS les mots faux d'une même phrase, pas seulement le premier ;\n"
            "   - tout le reste de la phrase reste DEBOUT, mot pour mot.\n"
            "   EXACTE (un seul mot barré) : « Le ProLiant DL380 Gen10 est sorti en ~~2012~~ "
            "**[→ 2017](https://exemple.fr/hpe \"HPE ProLiant Gen10\") » —\n"
            "   INTERDITE (phrase entière barrée) : « ~~Le ProLiant DL380 Gen10 est sorti en 2012~~ "
            "**[Le ProLiant DL380 Gen10 est sorti en 2017](https://exemple.fr/hpe \\\"HPE\\\") ».\n"
            + _REGLE_VALEUR +
            "   - quand le remplacement est un NOMBRE SEUL (année, quantité), écris le texte du lien "
            "avec une flèche : **[→ 2021](URL \"titre\")** — sans la flèche, Open WebUI interprète "
            "« [2021] » comme une note de bas de page et le lien est détruit à l'affichage ;\n"
        )
        regle5 = (
            "5. SORTIE STRICTE : uniquement le texte corrigé. Aucun préambule, aucun titre, aucune section, "
            "aucune liste de corrections, aucune remarque finale. Tout balisage HTML autre que ~~…~~ et les "
            "liens markdown serait affiché en texte brut — n'en emploie aucun.\n"
        )
    else:
        regle3 = (
            "3. Corrige CHAQUE erreur EN PLACE : SUPPRIME TOTALEMENT les mots faux (1 à 8 mots, jamais "
            "une phrase ou une ligne entière) et écris à leur place le remplacement exactement ainsi :\n"
            "   **[séquence corrigée](URL \"Corrigé : séquence erronée\")**\n"
            "   (les mots erronés ne doivent PLUS APPARAÎTRE dans le texte — ils ne survivent que dans le "
            "titre du lien, après « Corrigé : » ; le texte barré est INTERDIT — n'écris jamais ~~…~~)\n"
            + _REGLE_SOURCE +
            "   - le titre du lien commence OBLIGATOIREMENT par « Corrigé : » suivi des mots supprimés "
            "(sans guillemets à l'intérieur du titre) ;\n"
            "   - si la source citée contredit aussi un autre mot de la phrase (un acteur, un lieu, une "
            "quantité), supprime-LE aussi et remplace-le de la même façon : corrige TOUS les mots faux "
            "d'une même phrase, pas seulement le premier ;\n"
            "   - tout le reste de la phrase reste DEBOUT, mot pour mot.\n"
            "   EXACTE (un seul mot remplacé) : « Le ProLiant DL380 Gen10 est sorti en "
            "**[→ 2017](https://exemple.fr/hpe \"Corrigé : 2012\") » —\n"
            "   INTERDITE (phrase entière remplacée) : « **[Le ProLiant DL380 Gen10 est sorti en 2017]"
            "(https://exemple.fr/hpe \"Corrigé : Le ProLiant DL380 Gen10 est sorti en 2012\") ».\n"
            + _REGLE_VALEUR +
            "   - quand le remplacement est un NOMBRE SEUL (année, quantité), écris le texte du lien "
            "avec une flèche : **[→ 2021](URL \"Corrigé : valeur fausse\")** — sans la flèche, Open WebUI "
            "interprète « [2021] » comme une note de bas de page et le lien est détruit à l'affichage ;\n"
        )
        regle5 = (
            "5. SORTIE STRICTE : uniquement le texte corrigé. Aucun préambule, aucun titre, aucune section, "
            "aucune liste de corrections, aucune remarque finale. Tout balisage HTML serait affiché en texte "
            "brut — n'emploie que le gras ** et les liens markdown ; jamais de ~~barré~~.\n"
        )
    return regle3, regle5


def _build_instruction(valves, web_search_on: bool, target_excerpt: str) -> str:
    """Consigne de correction ajoutée en fin du préfixe rejoué (invisible pour
    l'utilisateur : complétion NON persistée, jamais insérée dans le chat).

    La cible est TOUJOURS la dernière réponse rejouée : la chaîne est coupée au
    message cliqué, donc le message cliqué est le leaf du préfixe — le prefix KV
    cache de la réponse d'origine est exactement reutilisé.
    """
    lang = (valves.language or "auto").strip().lower()
    if lang in ("", "auto"):
        langue = "la même langue que le texte à corriger"
    else:
        langue = "la langue '" + lang + "'"
    regle3, regle5 = _correction_rules(_resolve_display(valves))

    max_s = max(1, valves.max_searches)
    if web_search_on and valves.search_mode == "native":
        tool_note = (
            "Tu DISPOSES d'outils de recherche web (`search_web`, `fetch_url`) dans CETTE conversation : "
            "appelle-les directement, sans jamais annoncer que tu vas chercher.\n"
            "BUDGET D'APPELS — plafond dur à ne JAMAIS dépasser (le contexte sature vite) :\n"
            f"- au plus {max_s} appels `search_web` AU TOTAL : une requête courte et neutre par point "
            "suspect ; des points proches se traitent en UNE seule requête ;\n"
            "- n'appelle `fetch_url` QUE si un snippet est insuffisant pour trancher un point précis — "
            "jamais par défaut : une page entière sature la fenêtre de contexte ;\n"
            f"- ARRÊT OBLIGATOIRE : dès que chaque point suspect est tranché par une source fiable, ou "
            f"dès que {max_s} recherches ont été faites, CESSE toute recherche et rends le texte corrigé "
            "avec ce que tu as (ce qui reste non tranché est LAISSÉ INCHANGÉ — ne corrige pas au hasard) ;\n"
            "- n'utilise QUE des URL réellement retournées par ces appels. N'écris AUCUN texte avant le "
            "texte corrigé : le premier caractère de ta réponse doit être le premier caractère du texte "
            "à corriger (ou du premier balisage de correction)."
        )
    elif web_search_on:
        tool_note = (
            "Les résultats de recherches web déjà effectuées te sont fournis dans le contexte : ne corrige "
            "QU'à partir de ces résultats et n'utilise QUE des URL qui y figurent. Ne relance aucune recherche."
        )
    else:
        tool_note = (
            "AUCUNE recherche web n'est possible : ne corrige RIEN (tes souvenirs ne sont pas des sources) — "
            "retourne le texte strictement inchangé."
        )

    return (
        "Tu es un service de correction factuelle. TA SORTIE REMPLACERA LA RÉPONSE CI-DESSUS dans la "
        "conversation : personne ne doit savoir qu'une correction a eu lieu.\n"
        + tool_note
        + "\n\n\nTÂCHE\n"
        "1. Reproduis TA DERNIÈRE RÉPONSE (le message assistant immédiatement au-dessus de celui-ci) "
        "intégralement : structure, titres, listes, tableaux, markdown, longueur, ordre — ne résume pas, "
        "ne reformule pas ce qui est correct, ne commente rien.\n"
        "2. Repère les AFFIRMATIONS FACTUELLES FAUSSES de cette réponse (chiffres, dates, noms, événements, "
        "références produits, causalités) et tranche-les par la recherche web. Une réponse sans erreur "
        "factuelle est un résultat normal : ne cherche pas à tout prix.\n"
        "   PÉRIMÈTRE STRICT — tu ne corrigis que des faits SUR LE MONDE EXTÉRIEUR. NE corrige JAMAIS : "
        "les phrases qui parlent de l'assistant lui-même (ses outils, ses capacités, ses limites, son "
        "environnement, ce qu'il peut ou ne peut pas faire) ; les opinions, conseils et tournures. "
        "Si la réponse dit « je n'ai pas d'outil de recherche web » ou « je n'ai pas accès à internet », "
        "NE TOUCHE PAS À CETTE PHRASE, même si toi tu disposes d'outils de recherche : ces phrases sont "
        "HORS SUJET et ne sont pas des erreurs.\n"
        + regle3
        + "4. Si rien n'est faux (ou non tranchable), retourne le texte mot pour mot, sans un caractère de plus.\n"
        + regle5
        + "6. Rédige dans " + langue + ".\n\n"
        "SUJET DE LA CORRECTION (extrait de la réponse à corriger — pour formuler les requêtes de recherche) :\n"
        + target_excerpt
        + "\n\nPÉRIMÈTRE : seule ta dernière réponse est à corriger ; la conversation antérieure ne sert "
        "qu'au contexte. Ne poursuvis pas la conversation."
    )


def _completion_headers(request):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    authorization = request.headers.get("authorization")
    if authorization:
        headers["Authorization"] = authorization
    return headers


def _completion_base(request, base_url_override):
    return (base_url_override or "").strip().rstrip("/") or f"{request.url.scheme}://{request.url.netloc}"


def _is_saved_chat_id(chat_id) -> bool:
    return bool(chat_id) and not str(chat_id).startswith(_NON_SAVED_CHAT_PREFIXES)


async def _fetch_chat_history(request, chat_id: str, base_url_override: str) -> dict:
    """Historique COMPLET du chat via l'API (shape 0.13 : data['chat']['history'])."""
    import aiohttp

    url = f"{_completion_base(request, base_url_override)}/api/v1/chats/{chat_id}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=_completion_headers(request)) as response:
            if response.status != 200:
                raw = await response.text()
                raise RuntimeError(f"GET /chats HTTP {response.status} : {raw[:200]}")
            data = await response.json()
    data = data or {}
    chat = data.get("chat")
    if isinstance(chat, str):
        try:
            chat = json.loads(chat)
        except Exception:
            chat = {}
    chat = chat if isinstance(chat, dict) else {}
    return chat.get("history") or data.get("history") or {}


def _chain_to_message(history: dict, message_id: str) -> list:
    """Chaîne linéaire du chat du plus ancien JUSQU'AU MESSAGE CLIQUÉ inclus (walk
    parentId depuis la cible, puis inversion). Couper à la cible (et non au leaf)
    reutilise exactement le prefix KV de la réponse d'origine et exclut les tours
    ultérieurs, qui n'ont rien à voir avec la correction."""
    messages_map = history.get("messages") or {}
    if not message_id or message_id not in messages_map:
        return []
    chain = []
    seen = set()
    current_id = message_id
    while current_id and current_id in messages_map and current_id not in seen:
        seen.add(current_id)
        message = messages_map[current_id]
        role = message.get("role")
        if role in ("user", "assistant"):
            entry = {"id": current_id, "role": role, "content": str(message.get("content") or "")}
            if message.get("sources"):
                entry["sources"] = message["sources"]
            chain.append(entry)
        current_id = message.get("parentId")
    chain.reverse()
    if len(chain) > _MAX_PREFIX_MESSAGES:
        chain = chain[-_MAX_PREFIX_MESSAGES:]
    return chain


async def _stream_correction(request, payload, base_url_override, timeout_seconds, relay_status_to=None):
    """Self-call NON PERSISTÉ (pas de chat_id, pas de session_id) : la génération ne
    crée AUCUN message dans le chat — c'est la recherche « en arrière-plan ». Les
    événements de statut du backend sont re-émis vers le message cliqué pour que
    l'utilisateur voie l'activité de recherche réelle."""
    import aiohttp

    url = f"{_completion_base(request, base_url_override)}/api/chat/completions"
    headers = _completion_headers(request)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    pieces = []
    chunk_count = 0
    first_chunk_seen = False

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                raw = await response.text()
                raise RuntimeError(f"HTTP {response.status} : {raw[:300]}")

            buffer = ""
            stream_done = False

            def _handle_line_sync(line):
                line = line.strip()
                if not line.startswith("data:"):
                    return None
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    return "done"
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    return None
                if not isinstance(chunk, dict):
                    return None
                return chunk

            async def _consume_chunk(chunk):
                nonlocal chunk_count, first_chunk_seen
                chunk_count += 1

                error = chunk.get("error")
                if error:
                    detail = error if isinstance(error, str) else str(error)
                    raise RuntimeError(f"erreur du backend pendant le flux : {detail[:300]}")

                for event in chunk.get("events") or []:
                    if isinstance(event, dict) and event.get("type") == "status" and relay_status_to is not None:
                        await _emit(relay_status_to, event)

                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    piece = (choice.get("delta") or {}).get("content")
                    if isinstance(piece, str) and piece:
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            if relay_status_to is not None:
                                await _emit(
                                    relay_status_to,
                                    {"type": "status", "data": {"description": "Rédaction de la version corrigée…"}},
                                )
                        pieces.append(piece)

            async for raw in response.content.iter_any():
                if stream_done:
                    break
                buffer += raw.decode("utf-8", errors="replace")
                while True:
                    newline = buffer.find("\n")
                    if newline == -1:
                        break
                    line, buffer = buffer[:newline], buffer[newline + 1:]
                    chunk = _handle_line_sync(line)
                    if chunk == "done":
                        stream_done = True
                        break
                    if chunk is not None:
                        await _consume_chunk(chunk)
            if not stream_done and buffer.strip():
                chunk = _handle_line_sync(buffer)
                if isinstance(chunk, dict):
                    await _consume_chunk(chunk)

    content = "".join(pieces).strip()
    if not content and chunk_count == 0:
        raise RuntimeError("flux SSE vide : aucune donnée reçue du backend")
    return content


async def _update_chat_message(request, chat_id: str, message_id: str, content: str, base_url_override: str):
    """Remplace le contenu du message cliqué EN PLACE (POST /chats/{id}/messages/{mid},
    MessageForm{content}) — vérifié routers/chats.py 0.13 : upsert history + table
    chat_message, puis événement websocket 'chat:message' → l'UI affiche la correction
    en direct, sans rechargement. Message assistant : aucune restriction de rôle."""
    import aiohttp

    url = f"{_completion_base(request, base_url_override)}/api/v1/chats/{chat_id}/messages/{message_id}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url, json={"content": content}, headers=_completion_headers(request)
        ) as response:
            if response.status != 200:
                raw = await response.text()
                raise RuntimeError(f"POST update HTTP {response.status} : {raw[:200]}")


def _patch_output_message(output, content):
    """0.13 : quand une réponse a un timeline `output` (blocs function_call + message
    final), le FRONT REND CE TIMELINE, pas la colonne `content` (vérifié DOM + API).
    Sans sync du bloc `message` final, la correction écrite dans `content` reste
    INVISIBLE même après rechargement. Pur → testable. Retourne (output, changed)."""
    if not isinstance(output, list) or not output:
        return output, False
    changed = False
    for block in output:
        if isinstance(block, dict) and block.get("type") == "message":
            block["content"] = [{"type": "output_text", "text": content}]
            changed = True
    return output, changed


async def _sync_output_and_broadcast(chat_id: str, message_id: str, content: str):
    """Applique le bloc `message` final du timeline `output` au contenu corrigé — dans
    le JSON history ET la ligne chat_message (upsert ORM, même transaction) — puis
    diffuse `chat:messages` (salle chat:{id}, shared=True ; socket/main.py l.1657-1716)
    pour que l'onglet ouvert fusionne le message via mergeChatMessages → mise à jour
    LIVE du timeline, sans rechargement. Best effort : toute exception remonte à
    l'appelant qui loggue ; l'écriture `content` est déjà faite."""
    from open_webui.models.chats import Chats
    from open_webui.socket.main import get_event_emitter

    chat = await Chats.get_chat_by_id(chat_id)
    if not chat:
        return False
    history = (chat.chat or {}).get("history") or {}
    msg = (history.get("messages") or {}).get(message_id)
    if not msg:
        return False
    patched, changed = _patch_output_message(msg.get("output"), content)
    if not changed:
        return False
    msg["output"] = patched
    await Chats.upsert_message_to_chat_by_id_and_message_id(chat_id, message_id, {"output": patched})
    try:
        emitter = await get_event_emitter(
            {"user_id": chat.user_id, "chat_id": chat_id, "message_id": message_id}, False
        )
        if emitter:
            await emitter(
                {
                    "type": "chat:messages",
                    "data": {"messages": {message_id: msg}, "currentId": history.get("currentId")},
                }
            )
    except Exception:
        log.exception("correct-web: broadcast chat:messages echoue — un rechargement affichera la correction")
    return True


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n?|\n?```\s*$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_DEL_RE = re.compile(r"~~.*?~~", re.DOTALL)


def _visible_text(md: str) -> str:
    """Rendu visible approximatif : segments barrés supprimés, liens réduits à leur
    texte affiché. Seule cette longueur doit être comparable à l'original."""
    return _LINK_RE.sub(r"\1", _DEL_RE.sub("", md)).strip()


def _strip_fences(text: str) -> str:
    """Le modèle peut envelopper la sortie dans un bloc ``` — on la décape."""
    text = text.strip()
    return _FENCE_RE.sub("", text).strip()


# v1.1.0 — marqueur de correction : **[valeur](URL "Corrigé : valeur fausse")**.
# Le texte faux est SUPPRIMÉ du corps (plus de ~~barré~~) ; il ne survit que dans le
# titre du lien, qui sert de repère au CSS. La COULEUR du surlignage (valve) est un
# jeton optionnel dans le titre (« Corrigé #orange : … ») : purement cosmétique,
# ignoré par tous les calculs, jamais imposé au modèle (injecté côté serveur).
_CORR_LINK_RE = re.compile(
    r"(?:\*\*)?\[([^\]\n]{1,160})\]\(\s*([^)\s][^)\n]{0,240}?)\s+\"Corrigé(?: #[a-z]+)?\s*:\s*([^\"\n]{1,80})\"\s*\)(?:\*\*)?"
)
_LEGACY_SEG_RE = re.compile(
    r"~~([^~\n]{1,160})~~\s*(?:\*\*)?\[([^\]\n]{1,160})\]\(\s*([^)\n]{1,240}?)\s*\)(?:\*\*)?"
)
_TITLE_SRC_RE = re.compile(r"^(\S+)\s+\"([^\"]*)\"$")
_TITLE_MARKER_RE = re.compile(r"\"[Cc]orrig[ée]?(?: #[a-z]+)?\s*:\s*([^\"\n]{1,80})\"")
_LEFTOVER_TILDE_RE = re.compile(r"~~([^~\n]{1,160})~~")
_EMPTY_BOLD_RE = re.compile(r"~~([^~\n]{1,160})~~\s*\*\*\s*\*\*")
_DANGLING_BOLD_RE = re.compile(r"~~([^~\n]{1,160})~~\s*\*\*[ \t]*$", re.MULTILINE)
_CITATION_BADGE_RE = re.compile(r"[ \t]?\[\d{1,4}(?:,\s*\d{1,4})*\](?!\()")
_COLOR_TOKEN_RE = re.compile(r"Corrigé(?: #[a-z]+)?\s*:")
_SELF_TOPIC_RE = re.compile(
    r"\b(outils?\s+de\s+recherche|outils?|accès|connect|capacit|modèle|intelligence artificielle)\b",
    re.IGNORECASE,
)


def _resolve_display(valves) -> str:
    """Style d'affichage des corrections (valve correction_display) : 'strikethrough'
    (ancien ~~barré~~) ou 'replace' (défaut, faux supprimé + corrigé surligné). Tout
    style inconnu/absent retombe sur 'replace' — robuste aux valves déjà enregistrées."""
    return "strikethrough" if str(getattr(valves, "correction_display", "")).strip().lower() == "strikethrough" else "replace"


_CORRECTION_COLORS = ("jaune", "orange", "vert", "rose", "bleu", "cyan", "aucune")


def _resolve_color(valves) -> str:
    """Couleur de surlignage (valve correction_color) ; 'jaune' = valeur par défaut =
    aucune injection (la règle CSS de base s'applique déjà)."""
    color = str(getattr(valves, "correction_color", "")).strip().lower()
    return color if color in _CORRECTION_COLORS and color != "jaune" else ""


def _resolve_keep_citations(valves) -> bool:
    """Valve show_citations : conserver (défaut) ou retirer les badges de citation
    web [1][2] du texte corrigé. Absent/ancien dict de valves = True (comportement actuel)."""
    value = getattr(valves, "show_citations", True)
    return value is not False


def _apply_color(md: str, color: str) -> str:
    """Injecte le jeton couleur dans le titre de chaque marqueur (« Corrigé #orange : »).
    Le CSS cible le jeton ; les sanitizers le tolèrent et le normalisent (la couleur est
    donc re-choisie à chaque nouvelle correction, jamais héritée du texte précédent)."""
    if not color:
        return md
    return _COLOR_TOKEN_RE.sub(f"Corrigé #{color} :", md)


def _finalize_render(md: str, valves) -> str:
    """Rendu final selon les valves d'affichage — appliqué APRÈS validation/sanitization
    (qui travaillent toujours sur le format canonique interne). Ordre important :
    style -> couleur -> suppression des badges de citation."""
    if _resolve_display(valves) == "strikethrough":
        md = _render_strikethrough(md)
    md = _apply_color(md, _resolve_color(valves))
    if not _resolve_keep_citations(valves):
        md = _CITATION_BADGE_RE.sub("", md)
    return md


def _convert_legacy(md: str) -> str:
    """ANCIEN format `~~ancien~~ **[nouveau](URL "source")**` -> format canonique interne
    `**[nouveau](URL "Corrigé : ancien")**`. Quel que soit le style demandé, la détection,
    le comptage et les sanitizers travaillent TOUJOURS sur le format canonique ; le rendu
    final est ensuite adapté par la valve (voir _render_strikethrough)."""
    md = _LEGACY_SEG_RE.sub(_legacy_to_new, md)
    md = _EMPTY_BOLD_RE.sub(r"\1", md)
    md = _DANGLING_BOLD_RE.sub(r"\1", md)
    md = _TITLE_MARKER_RE.sub(lambda m: '"Corrigé : ' + m.group(1) + '"', md)
    # ~~restant sans lien de correction associe -> texte nu (plus jamais de barre)
    md = _LEFTOVER_TILDE_RE.sub(r"\1", md)
    return md


def _legacy_to_new(m):
    old, new, rest = m.group(1), m.group(2), m.group(3).strip()
    t = _TITLE_SRC_RE.match(rest)
    if t:
        title = f"Corrigé : {old} — {t.group(2)}"[:200]
        return f'**[{new}]({t.group(1)} "{title}")**'
    return f'**[{new}]({rest} "Corrigé : {old}")**'


_CANON_TITLE_SPLIT = " — "


def _title_old(title: str) -> str:
    """Dans un titre canonique « ancien — source », seule la partie « ancien » est le
    texte d'origine à restaurer quand une correction est annulée par les sanitizers."""
    return title.partition(_CANON_TITLE_SPLIT)[0].strip()


def _render_strikethrough(md: str) -> str:
    """Rendu valve 'strikethrough' : le marqueur canonique est rejoué en ancien style
    `~~faux~~ **[corrigé](URL "source")**` — l'information du titre (« Corrigé : faux —
    source ») est redistribuée barré/titre de lien. Idempotent : sans marqueur canonique,
    le texte ressort inchangé."""

    def _mk(m):
        new, url, title = m.group(1), m.group(2), m.group(3)
        old, _, src = title.partition(_CANON_TITLE_SPLIT)
        old = old.strip() or "…"
        src = src.strip().replace('"', "'")
        link = f'{url} "{src}"' if src else url
        return f"~~{old}~~ **[{new}]({link})**"

    return _CORR_LINK_RE.sub(_mk, md)


def _strip_self_corrections(md: str) -> str:
    """Sanitizer déterministe : une correction dont le titre « Corrigé : » parle de
    l'assistant lui-même (ses outils, son accès, ses capacités) est ALWAYS annulée — la
    séquence d'origine est restaurée. Le modèle « corrige » spontanément « je n'ai pas
    d'outil de recherche » en croyant bien faire : c'est hors sujet."""

    def _restore(m):
        if _SELF_TOPIC_RE.search(m.group(3)):
            return _title_old(m.group(3))
        return m.group(0)

    return _CORR_LINK_RE.sub(_restore, md)


_NULL_REPL_RE = re.compile(
    r"^\s*(?:N/?A|M[àa]J|MAJ|inconnu(?:e)?|indisponible|non trouv[ée]s?|-{1,3}|\?+|…|\.)\s*$",
    re.IGNORECASE,
)
_EMPTY_MARKER_RE = re.compile(
    r"(?:\*\*)?\[\s*\]\(\s*[^)\s][^)\n]{0,240}?\s+\"Corrigé(?: #[a-z]+)?\s*:\s*([^\"\n]{1,80})\"\s*\)(?:\*\*)?"
)


def _revert_null_replacements(md: str) -> str:
    """Sanitizer déterministe : une correction dont le remplacement n'apporte AUCUNE
    information (N/A, MàJ, inconnu, indisponible, vide) est annulée — le texte d'origine
    (titre « Corrigé : … ») est restauré. Empêche les fausses corrections quand la
    recherche du modèle tombe sur des pages périmées qui ne contredisent rien."""

    def _restore(m):
        if _NULL_REPL_RE.match(m.group(1)):
            return _title_old(m.group(3))
        return m.group(0)

    md = _CORR_LINK_RE.sub(_restore, md)
    md = _EMPTY_MARKER_RE.sub(lambda m: _title_old(m.group(1)), md)
    return md


_NUMERIC_LINK_TEXT_RE = re.compile(r"(\*\*)\[(\d[\d\s.,\u00a0\u202f]*)\]\(")


def _arrow_numeric_links(md: str) -> str:
    """L'extension « citation » d'Open WebUI capture tout [texte](url) dont le texte est
    purement numérique (« [2021] ») et le rend en note — le lien est détruit à l'affichage.
    Préfixer le texte par « → » empêche la capture de citation sans changer le sens."""
    return _NUMERIC_LINK_TEXT_RE.sub(r"\1[→ \2](", md)


def _word_overlap(original: str, corrected: str) -> float:
    """Fraction des mots d'origine qui subsistent dans le texte corrigé
    (insensible à l'ordre, robuste aux corrections ponctuelles)."""
    orig = {w for w in re.findall(r"\w+", original.lower(), flags=re.UNICODE) if len(w) > 1}
    corr = {w for w in re.findall(r"\w+", corrected.lower(), flags=re.UNICODE) if len(w) > 1}
    if not orig:
        return 1.0
    return len(orig & corr) / len(orig)


def _validate_correction(original: str, corrected: str):
    """Retourne (verdict, raison) :
      ("update", n_corrections)  → remplacer le message ;
      ("unchanged", None)        → rien à corriger (texte verbatim) ;
      ("reject", raison)         → sortie non fiable, texte d'origine préservé.
    """
    corrected = _convert_legacy(_strip_fences(corrected))
    if not corrected:
        return "reject", "sortie vide"

    n_corr = len(_CORR_LINK_RE.findall(corrected))
    ratio = len(corrected) / max(1, len(original))

    if n_corr == 0:
        overlap0 = _word_overlap(original, corrected)
        # Aucun marqueur de correction = RIEN ne sera écrit de toute façon : ce n'est donc
        # jamais un « rejet » (ni alarme garde-fou). Deux cas : reproduction fidèle
        # (rien à corriger) ou le modèle a voulu réécrire/répondre au lieu de corriger
        # (refusé par contrat, texte conservé tel quel).
        if overlap0 < 0.75 or not (_RELAXED_BAND[0] <= ratio <= _RELAXED_BAND[1]):
            return "unchanged", "reesquisse"
        return "unchanged", "reformule" if (overlap0 < 0.99 or not (_IDENTICAL_BAND[0] <= ratio <= _IDENTICAL_BAND[1])) else None

    if corrected.lstrip().startswith("## "):
        return "reject", "le modèle a produit un rapport (titre '##') au lieu du texte corrigé"
    ratio = len(_visible_text(corrected)) / max(1, len(original))
    if ratio < _MIN_RATIO:
        return "reject", f"texte visible trop court ({ratio:.0%} de l'original) : résumé au lieu de reproduction"
    if ratio > _MAX_RATIO:
        return "reject", f"texte visible trop long ({ratio:.0%} de l'original) : commentaires ajoutés"
    overlap = _word_overlap(original, corrected)
    if overlap < _MIN_OVERLAP:
        return "reject", f"trop peu de mots d'origine conservés ({overlap:.0%}) : paraphrase détectée"

    return "update", n_corr


async def _correct_in_place(request, model_id, chain, instruction, web_search_on, valves, emitter, chat_id, message_id):
    """Flux complet : recherche en arrière-plan (complétion non persistée) → validation
    → remplacement du message cliqué via l'API (événement 'chat:message' → UI en direct).
    Toute sortie douteuse est REJETÉE : le texte d'origine reste intact, toujours."""
    progress = asyncio.create_task(_progress_task(emitter, "Recherche des sources (correction en arrière-plan)"))
    started = time.monotonic()
    try:
        payload = {
            "model": model_id,
            "messages": list(chain) + [{"role": "user", "content": instruction}],
            "stream": True,
            "features": {"web_search": bool(web_search_on)},
        }
        if web_search_on and valves.search_mode == "legacy":
            payload["params"] = {"function_calling": "legacy"}
        corrected = await _stream_correction(
            request, payload, valves.base_url, valves.timeout_seconds, relay_status_to=emitter
        )
    except Exception as exc:
        log.exception("correct-web: la génération de correction a échoué")
        await _log(emitter, f"Échec de la correction : {exc}", "error")
        await _emit(emitter, {"type": "status", "data": {"description": "Échec de la correction — détail dans les logs.", "done": True}})
        await _toast(emitter, "error", "Correction web échouée — le texte d'origine est resté intact.")
        return None
    finally:
        progress.cancel()

    duration = round(time.monotonic() - started, 1)
    corrected = _strip_self_corrections(_revert_null_replacements(_arrow_numeric_links(_convert_legacy(corrected))))
    original = str(chain[-1].get("content") or "") if chain else ""
    verdict, detail = _validate_correction(original, corrected)

    if verdict == "unchanged":
        note = " (le modèle a reformulé à la marge sans corriger aucune faute)" if detail == "reformule" else \
               " (le modèle a voulu réécrire la réponse au lieu de la corriger — refusé par contrat, texte conservé)" if detail == "reesquisse" else ""
        await _log(emitter, f"Terminée en {duration} s : aucune erreur factuelle à corriger sur ce message — texte conservé.{note}")
        await _emit(emitter, {"type": "status", "data": {"description": "Aucune erreur à corriger.", "done": True}})
        await _toast(emitter, "info", "Aucune erreur factuelle à corriger — la réponse est restée inchangée.")
        return None
    if verdict == "reject":
        await _log(emitter, f"Sortie rejetée ({detail}) — le texte d'origine est conservé.", "warn")
        await _emit(emitter, {"type": "status", "data": {"description": "Correction rejetée (garde-fou) — voir les logs.", "done": True}})
        await _toast(emitter, "warning", "La correction produite a été rejetée par les garde-fous — texte d'origine conservé.")
        return None

    corrected = _finalize_render(corrected, valves)

    try:
        await _update_chat_message(request, chat_id, message_id, corrected, valves.base_url)
    except Exception as exc:
        log.exception("correct-web: l'écriture du message corrigé a échoué")
        await _log(emitter, f"Échec de la mise à jour du message : {exc}", "error")
        await _emit(emitter, {"type": "status", "data": {"description": "Échec de la mise à jour — détail dans les logs.", "done": True}})
        await _toast(emitter, "error", "Correction calculée mais non écrite — texte d'origine conservé.")
        return None

    try:
        synced = await _sync_output_and_broadcast(chat_id, message_id, _strip_fences(corrected))
    except Exception:
        log.exception("correct-web: sync du timeline output / broadcast chat:messages echoue")
        synced = False
    if not synced:
        # Message sans timeline (génération sans outils) : `content` suffit, l'event
        # 'chat:message' du POST a déjà mis l'UI à jour.
        log.info("correct-web: pas de timeline output a sync (message sans outils) — content seul")

    await _log(emitter, f"Correction appliquée en {duration} s ({detail} erreur(s) corrigée(s), en place).")
    await _emit(emitter, {"type": "status", "data": {"description": "Réponse corrigée en place.", "done": True}})
    await _toast(emitter, "success", f"Réponse corrigée en place — {detail} erreur(s) factuelle(s) corrigée(s).")
    return corrected


class Action:
    description: str = (
        "Recherche le web en arrière-plan puis corrige cette réponse EN PLACE : le faux est "
        "supprimé et le corrigé surligné (ou ~~barré~~ selon la valve correction_display), "
        "sans rapport ni nouveau message"
    )

    class Valves(BaseModel):
        language: str = Field(default="auto", description="'auto' ou code de langue ('fr', 'en')")
        max_searches: int = Field(
            default=6, ge=1, le=25,
            description="Plafond dur d'appels search_web (anti-boucle/saturation contexte)",
        )
        web_search: bool = Field(
            default=True,
            description="Utiliser la recherche web de l'instance (elle doit être activée côté admin)",
        )
        search_mode: str = Field(
            default="native",
            json_schema_extra={"input": {"type": "select", "options": ["native", "legacy"]}},
            description="'native' = outils de recherche pour le modèle (function calling) ; "
            "'legacy' = recherches pré-injectées (modèles sans function calling)",
        )
        correction_display: str = Field(
            default="replace",
            json_schema_extra={"input": {"type": "select", "options": ["replace", "strikethrough"]}},
            description="Affichage des corrections : 'replace' = le texte faux est SUPPRIMÉ, le corrigé "
            "surligné (la faute reste en infobulle) ; 'strikethrough' = ancien style "
            "~~texte faux~~ [corrigé](lien source), les deux restent surlignés à l'affichage",
        )
        correction_color: str = Field(
            default="jaune",
            json_schema_extra={"input": {"type": "select", "options": ["jaune", "orange", "vert", "rose", "bleu", "cyan", "aucune"]}},
            description="Couleur du surlignage des corrections (style 'replace') — appliqué au rendu, "
            "la palette correspondante est définie dans la feuille de style globale de l'instance",
        )
        show_citations: bool = Field(
            default=True,
            description="Conserver les badges de citation web [1] [2] dans le texte corrigé "
            "(décocher pour les retirer de la réponse corrigée)",
        )
        strict_search: bool = Field(
            default=True,
            description="Refuser de corriger si la recherche web est impossible "
            "(sans sources, une correction n'a aucune valeur)",
        )
        debug: bool = Field(default=False, description="Journaliser la configuration résolue")
        completion_model: str = Field(
            default="", description="Modèle utilisé pour la correction (vide = modèle de la conversation)"
        )
        base_url: str = Field(
            default="", description="URL interne du backend Open WebUI pour l'appel de complétion (vide = auto)"
        )
        timeout_seconds: int = Field(default=300, ge=30, le=900, description="Délai maximum de la correction")
        max_chars: int = Field(
            default=16000, ge=500, le=32000, description="Longueur maximale du texte soumis à correction"
        )
        priority: int = Field(default=1, description="Ordre d'affichage du bouton (plus bas = plus à gauche)")

    def __init__(self):
        self.valves = self.Valves()

    async def action(
        self,
        body: dict,
        __user__=None,
        __model__=None,
        __request__=None,
        __event_emitter__=None,
        __event_call__=None,
    ):
        message_id, full_content, target_idx = _extract_target(body)
        if not message_id or not full_content:
            await _log(__event_emitter__, "Correction web : rien à corriger dans ce message.")
            return None

        if __request__ is None:
            await _log(__event_emitter__, "Correction web : contexte '__request__' indisponible.", "error")
            return None

        valves = self.valves
        model_id = (valves.completion_model or "").strip() or body.get("model") or (__model__ or {}).get("id")
        if not model_id:
            await _log(__event_emitter__, "Correction web : modèle à interroger indisponible.", "error")
            return None

        chat_id = body.get("chat_id")
        saved = _is_saved_chat_id(chat_id)

        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Correction web : démarrage…"}})
        await _toast(
            __event_emitter__, "info",
            "Correction web lancée en arrière-plan — la réponse sera remplacée directement dans la bulle.",
        )
        await _log(__event_emitter__, f"Correction lancée sur le message (modèle : {model_id}).")

        if valves.debug:
            version = "?"
            engine = "?"
            try:
                from open_webui.env import VERSION as owui_version  # pyright: ignore[reportMissingImports]

                version = owui_version
            except Exception:
                pass
            try:
                from open_webui.models.config import Config  # pyright: ignore[reportMissingImports]

                engine = str(await Config.get("web.search.engine", "?") or "?")
            except Exception:
                pass
            await _log(
                __event_emitter__,
                f"Debug : backend v{version}, modèle={model_id}, mode={valves.search_mode}, "
                f"moteur recherche={engine}, base_url={valves.base_url or 'auto'}, chat sauvegardé={saved}",
            )

        web_search = bool(valves.web_search)
        if web_search:
            searchable, blocking = await _web_search_readiness(__request__, model_id, valves.search_mode)
            for reason in blocking:
                await _log(__event_emitter__, f"Pré-vol recherche web : {reason}", "warn")
            if blocking and valves.strict_search:
                await _emit(
                    __event_emitter__,
                    {"type": "status", "data": {"description": "Correction impossible : voir les logs du message.", "done": True}},
                )
                await _log(__event_emitter__, "Correction interrompue (strict_search).", "error")
                return None
            if blocking:
                web_search = False
            if web_search:
                mode_note = (
                    "outils de recherche natifs (function calling)"
                    if valves.search_mode == "native"
                    else "recherches pré-injectées (mode RAG legacy)"
                )
                await _log(__event_emitter__, f"Recherche web active : {mode_note}.")

        # Chaîne rejouée : du début du chat JUSQU'AU message cliqué inclus (coupure à
        # la cible = prefix KV exactement celui de la réponse d'origine). En cas
        # d'échec API, repli sur le body du front (qui contient déjà cette chaîne).
        prefix, truncated = _build_prefix(body.get("messages") or [], target_idx, valves.max_chars)
        if truncated:
            await _log(__event_emitter__, f"Contexte réduit pour la correction (max_chars={valves.max_chars}).", "warn")

        chain = prefix
        if saved:
            try:
                history = await _fetch_chat_history(__request__, chat_id, valves.base_url)
                built = _chain_to_message(history, str(message_id))
                if built:
                    chain = built
            except Exception as exc:
                await _log(__event_emitter__, f"Historique du chat illisible ({exc}) — repli sur le contexte du message cliqué.", "warn")

        if not chain:
            await _log(__event_emitter__, "Aucune chaîne à rejouer : correction impossible.", "error")
            return None

        instruction = _build_instruction(valves, web_search, full_content[:600])

        if saved:
            # FIRE-AND-FORGET : l'appel de l'action se termine immédiatement ; la
            # correction s'écrit dans le message via l'API REST + événement websocket
            # 'chat:message' (UI mise à jour en direct). Aucune connexion SSE à tenir
            # côté navigateur → insensible aux timeouts proxy/onglet.
            task = asyncio.create_task(
                _correct_in_place(
                    __request__, model_id, chain, instruction, web_search, valves,
                    __event_emitter__, chat_id, str(message_id),
                )
            )
            _PENDING_TASKS.add(task)
            task.add_done_callback(_PENDING_TASKS.discard)
            await asyncio.sleep(1.5)
            return None

        # MODE REPLI (chat temporaire/local non sauvegardé) : pas de table chat_message
        # → pas d'écriture API possible. Contrat historique des Actions : renvoyer un
        # message porteur de l'id existant ; le front le FUSIONNE par id et conserve
        # l'original dans 'originalContent' (annulable par l'utilisateur). Toujours
        # garde-fous + validation, et aucun rapport : le contenu renvoyé est le texte
        # corrigé seul.
        started = time.monotonic()
        progress = asyncio.create_task(_progress_task(__event_emitter__, "Recherche des sources (correction en arrière-plan)"))
        try:
            payload = {
                "model": model_id,
                "messages": list(chain) + [{"role": "user", "content": instruction}],
                "stream": True,
                "features": {"web_search": bool(web_search)},
            }
            if web_search and valves.search_mode == "legacy":
                payload["params"] = {"function_calling": "legacy"}
            corrected = await _stream_correction(
                __request__, payload, valves.base_url, valves.timeout_seconds, relay_status_to=__event_emitter__
            )
        except Exception as exc:
            log.exception("correct-web: la génération de correction a échoué")
            await _log(__event_emitter__, f"Échec de la correction : {exc}", "error")
            await _emit(__event_emitter__, {"type": "status", "data": {"description": "Échec de la correction — détail dans les logs.", "done": True}})
            await _toast(__event_emitter__, "error", "Correction web échouée — le texte d'origine est resté intact.")
            return None
        finally:
            progress.cancel()

        duration = round(time.monotonic() - started, 1)
        corrected = _strip_self_corrections(_revert_null_replacements(_arrow_numeric_links(_convert_legacy(corrected))))
        verdict, detail = _validate_correction(full_content, corrected)
        if verdict == "unchanged":
            await _log(emitter=__event_emitter__, message=f"Terminée en {duration} s : aucune erreur à corriger — texte inchangé.")
            await _toast(__event_emitter__, "info", "Aucune erreur factuelle à corriger — la réponse est restée inchangée.")
            return None
        if verdict == "reject":
            await _log(__event_emitter__, f"Sortie rejetée ({detail}) — texte d'origine conservé.", "warn")
            await _toast(__event_emitter__, "warning", "Correction rejetée par les garde-fous — texte d'origine conservé.")
            return None

        corrected = _finalize_render(corrected, valves)
        await _log(__event_emitter__, f"Correction appliquée en {duration} s ({detail} erreur(s)) — fusion front par id.")
        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Réponse corrigée en place.", "done": True}})
        await _toast(__event_emitter__, "success", f"Réponse corrigée en place — {detail} erreur(s) factuelle(s) corrigée(s).")
        return {"messages": [{"id": message_id, "role": "assistant", "content": _strip_fences(corrected)}]}
