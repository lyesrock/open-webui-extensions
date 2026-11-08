"""
title: Vérifier sur le web
author: lyesrock
version: 5.2.1
description: Vérifie les faits d'une réponse via la recherche web de l'instance et restitue le résultat comme un tour normal du chat (le modèle de la conversation répond dans son propre contexte)
required_open_webui_version: 0.13.0
license: MIT
"""

import asyncio
import json
import logging
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


async def _emit(emitter, payload):
    """Envoie un événement au front-end sans jamais propager d'exception (socket fermée, onglet fermé...)."""
    if emitter is None:
        return
    try:
        await emitter(payload)
    except Exception:
        log.exception("fact-check: échec d'émission de l'événement %s", payload.get("type"))


async def _log(emitter, message, level="info"):
    """Journal persistant affiché sous le message (panneau View Logs) — contrairement au status, il reste visible."""
    await _emit(emitter, {"type": "logs", "data": {"logs": [{"message": message, "metadata": {"level": level}}]}})


async def _toast(emitter, ttype, content):
    """Popup (toast) affiché en haut de l'écran du navigateur, visible même si l'utilisateur a quitté la conversation."""
    await _emit(emitter, {"type": "notification", "data": {"type": ttype, "content": content}})


async def _progress_task(emitter, label, interval=None):
    """Battement de statut périodique : prouve que l'action travaille encore (N s écoulées).

    Émet aussi un toast de progression toutes les _TOAST_EVERY secondes : la ligne de
    status est trop discrète sur les messages déjà terminés (cas des Actions).
    """
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
                await _toast(emitter, "info", f"Vérification web en cours… {elapsed} s écoulées.")
    except asyncio.CancelledError:
        pass


def _extract_target(body: dict):
    """Retourne (message_id, texte_à_vérifier, question_antezieure, index_de_la_cible).

    Le front Open WebUI passe dans `body['id']` l'id du message assistant sur lequel
    le bouton a été cliqué : on cible ce message précis, avec repli sur le dernier
    message assistant si l'id est absent (vieilles versions).
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
        return None, "", "", -1

    target = messages[target_idx]
    content = str(target.get("content") or "").strip()

    user_prompt = ""
    for idx in range(target_idx - 1, -1, -1):
        if messages[idx].get("role") == "user":
            user_prompt = str(messages[idx].get("content") or "").strip()
            break
    return target.get("id"), content, user_prompt, target_idx


def _build_prefix(messages: list, target_idx: int, valves) -> tuple:
    """Préfixe de CONTINUITÉ : les messages exactement tels que le front les envoie,
    jusqu'au message cible inclus.

    En rejouant ce préfixe à l'identique (mêmes messages, même modèle, mêmes features),
    le préfixe tokenisé de la requête est identique à celui de la conversation d'origine :
    le KV/prefix cache du serveur d'inférence (APC type vLLM) est réutilisé et la
    vérification se poursuit comme un tour naturel de la discussion. Seule la consigne
    ajoutée en fin de liste est du texte neuf.

    Retourne (prefix, tronque) — tronque=True si le contenu a dû être réduit
    (message cible trop long ou conversation démesurée).
    """
    prefix = [dict(message) for message in messages[: target_idx + 1]]
    if not prefix:
        return [], False
    truncated = False

    target = prefix[-1]
    content = str(target.get("content") or "")
    if len(content) > valves.max_chars:
        target["content"] = content[: valves.max_chars] + "\n[... texte tronqué pour la vérification ...]"
        truncated = True

    if len(prefix) > _MAX_PREFIX_MESSAGES:
        # Garde-fou fenêtre de contexte : au-delà, on ne peut plus rejouer tout
        # l'historique ; on garde les derniers tours (le cache est de toute façon
        # périmé sur une conversation aussi longue).
        prefix = prefix[-_MAX_PREFIX_MESSAGES:]
        while prefix and prefix[0].get("role") == "assistant":
            prefix = prefix[1:]
        truncated = True

    return prefix, truncated


async def _web_search_readiness(request, model_id: str, search_mode: str):
    """Pré-vol : reproduit les conditions d'injection de la recherche web côté backend.

    Retourne (searchable, blocking_reasons) :
      searchable : True = recherche dispo, False = impossible, None = version trop ancienne pour vérifier.
      blocking_reasons : empêchent la vérification dans le mode choisi (outils jamais injectés).
    """
    blocking = []
    searchable = None

    try:
        # Imports résolus uniquement à l'exécution, dans le process backend d'Open WebUI
        # (le plugin y est exec()'é) — jamais depuis la racine du repo : Pylance ne peut
        # pas les statiquer, d'où l'ignore ciblé.
        from open_webui.models.config import Config  # pyright: ignore[reportMissingImports]

        searchable = bool(await Config.get("web.search.enable"))
    except Exception:
        # Config (base) est apparu après 0.6.x : repli sur les constantes d'environnement.
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

    models_map = {}
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


def _build_instruction(valves, web_search_on: bool, target_excerpt: str = "", target_is_last: bool = True) -> str:
    """Consigne de vérification ajoutée EN FIN de la conversation existante.

    Le texte à vérifier n'est PAS dupliqué en entier : quand il est le dernier
    message du préfixe rejoué (cas normal), la consigne se contente de le désigner
    (continuité KV cache). Si le message cliqué n'est PAS le dernier du chat,
    l'extrait fourni dans la consigne devient la définition du sujet.

    target_excerpt : court extrait de la réponse vérifiée. En mode legacy, le
    middleware génère les requêtes de recherche UNIQUEMENT à partir du dernier
    message user (chat_web_search_handler -> generate_queries(prompt=last_user)) :
    sans extrait, les requêtes portent sur le texte de la consigne et les sources
    injectées sont hors-sujet. L'extrait remonte le vrai sujet dans ce dernier
    message, sans toucher au préfixe caché.
    """
    lang = (valves.language or "auto").strip().lower()
    if lang in ("", "auto"):
        langue = "la même langue que la réponse vérifiée"
    else:
        langue = "la langue '" + lang + "'"

    if valves.include_correction:
        correction_block = (
            "9. Termine par un bloc \"Version corrigée\" reprenant le texte initial avec "
            "UNIQUEMENT les corrections nécessaires (ne réécris pas ce qui est exact)."
        )
    else:
        correction_block = (
            "9. Ne réécris pas le texte : limite-toi au verdict et aux corrections ponctuelles."
        )

    max_s = max(valves.min_searches, valves.max_searches)
    if web_search_on and valves.search_mode == "native":
        tool_note = (
            "Tu DISPOSES d'outils de recherche web (`search_web`, `fetch_url`) dans CETTE conversation : "
            "appelle-les directement, sans jamais annoncer que tu vas chercher.\n"
            "BUDGET D'APPELS — plafond dur à ne JAMAIS dépasser (le contexte sature vite) :\n"
            f"- au plus {max_s} appels `search_web` AU TOTAL : une requête courte et neutre par THÈME, "
            "plusieurs affirmations proches se traitent en UNE seule requête ;\n"
            "- n'appelle `fetch_url` QUE si un snippet est insuffisant pour trancher un point précis — "
            "jamais par défaut : une page entière sature la fenêtre de contexte ;\n"
            "- ARRÊT OBLIGATOIRE : dès que chaque affirmation contestée est croisée par deux sources "
            f"indépendantes, ou dès que {max_s} recherches ont été faites, CESSE toute recherche et rends "
            "le rapport avec ce que tu as (ce qui reste non documenté se marque « non confirmé ») ;\n"
            "- ne cite QUE des URL réellement retournées par ces appels. N'écris AUCUN texte avant le "
            "rapport : le premier caractère de ta réponse finale doit être le titre "
            "\"## Vérification factuelle\"."
        )
        step2 = (
            "2. Regroupe les affirmations en recherches thématiques : entre "
            + str(valves.min_searches) + " et " + str(max_s) + " appels AU TOTAL — jamais plus "
            "(plafond anti-boucle). Une recherche par thème, pas par affirmation. "
            "Requêtes neutres, jamais suggestives.\n"
        )
    elif web_search_on:
        tool_note = (
            "Les résultats de recherches web déjà effectuées te sont fournis dans le contexte : base "
            "chaque verdict EXCLUSIVEMENT sur ces résultats et ne cite QUE des URL qui y figurent."
        )
        step2 = (
            "2. Les recherches web sont PRÉ-INJECTÉES dans le contexte : ne relance aucune recherche, "
            "travaille uniquement avec les sources fournies.\n"
        )
    else:
        tool_note = (
            "AUCUN outil de recherche web ne t'est fourni : toute affirmation que tu ne peux pas établir "
            "avec certitude doit être marquée \"non confirmé\", jamais présentée comme vérifiée."
        )
        step2 = (
            "2. Aucune recherche n'étant possible, établis le verdict sur ce que tu peux prouver avec "
            "certitude ; sinon « non confirmé ».\n"
        )

    target_ref = (
        "TA DERNIÈRE RÉPONSE dans cette conversation — le message assistant immédiatement "
        "au-dessus de celui-ci"
        if target_is_last
        else "la réponse ciblée par cette vérification, extraite plus bas dans la consigne "
        "(bloc « SUJETS DE VÉRIFICATION »)"
    )

    return (
        "Tu es un service de vérification journalistique des faits (fact-checking).\n"
        "Contrôle l'exactitude factuelle de " + target_ref + " en effectuant des recherches web,\n"
        + tool_note
        + "\npuis croise les sources avant toute conclusion.\n\n"
        "MÉTHODE OBLIGATOIRE\n"
        "1. Extrais jusqu'à " + str(valves.max_checks) + " affirmations factuelles vérifiables "
        "(chiffres, dates, noms, événements, causalités, comparaisons, citations) de cette réponse. "
        "Ignore opinions, mise en forme et évidences.\n"
        + step2 +
        "3. Croise au moins DEUX sources indépendantes et fiables pour toute affirmation contestée "
        "ou chiffrée. Écarte d'office sites partisans, propagandistes, fermes de contenu et pages "
        "non datées. Privilégie presse établie, sources officielles, bases de données.\n"
        "4. Vérifie la fraîcheur de l'information : une donnée peut être vraie hors contexte ou périmée.\n"
        "5. Ne tranche jamais sur l'autorité d'une source unique pour un point disputé.\n\n"
        "RÈGLES\n"
        "- N'utilise AUCUNE connaissance non vérifiée par recherche pour conclure ; tes souvenirs "
        "servent seulement à formuler de meilleures requêtes.\n"
        "- Si une recherche échoue ou ne donne rien, dis-le explicitement au lieu d'inventer.\n"
        "- Distingue : établi / attribué à un tiers / non confirmé.\n"
        "- Ne qualifie pas ton rendu de \"test\" ni de \"cycle\" : présente-le comme un fil de vérification.\n"
        "- Rédige en " + langue + ".\n\n"
        "FORMAT DE SORTIE (à respecter exactement)\n\n"
        "## Vérification factuelle\n\n"
        "**Verdict global :** Exact / Partiellement exact / Contient des erreurs / Non vérifiable\n\n"
        "| # | Affirmation | Verdict | Ce que disent les sources |\n"
        "|---|---|---|---|\n"
        "| 1 | ... | OK / partiel / faux / non confirmé | ... |\n\n"
        "Puis, pour chaque affirmation partielle ou fausse uniquement :\n\n"
        "### Affirmation n\n"
        "- Texte initial : \"...\"\n"
        "- Verdict : imprécis / faux (nature de l'erreur : chiffre, date, attribution, ordre de grandeur, contexte omis)\n"
        "- Correction : ...\n"
        "- Sources : [Titre - Éditeur](url), [Titre - Éditeur](url)\n"
        + correction_block
        + (
            "\n\nSUJETS DE VÉRIFICATION (extraits de la réponse à vérifier — pour formuler "
            "les requêtes de recherche et identifier la réponse ciblée) :\n" + target_excerpt
            if target_excerpt
            else ""
        )
        + "\n\nPÉRIMÈTRE : seule la réponse ciblée est à vérifier — la conversation antérieure ne sert "
        "qu'à comprendre son contexte. Ne poursuivs pas la conversation : produis UNIQUEMENT le rapport "
        "de vérification, sans préambule."
    )


_DETAILS_SUMMARY = "🔎 Vérification web — consigne automatique (cliquer pour dérouler)"


def _hide_instruction_block(instruction: str) -> str:
    """Emballe la consigne dans un bloc <details> repliable.

    Rendu natif OWUI 0.13 (marked + detailsTokenizer, MarkdownTokens -> Collapsible,
    replié par défaut sauf réglage « expandDetails ») : dans le chat, la bulle de
    l'utilisateur devient une simple ligne compacte au lieu du pavé complet de la
    consigne. Le MESSAGE STOCKÉ EN BASE — donc rejoué au modèle à chaque tour, et
    consultable en déroulant la ligne — reste la consigne intégrale : ni perte de
    consigne côté modèle, ni impact sur le KV prefix cache (le bloc reste en queue
    de chaîne).

    Le tokenizer marked d'OWUI impose : « <details> » en début de ligne, SAUT DE
    LIGNE après la balise, « <summary>...</summary> » seul sur sa ligne ; il compte
    la profondeur des balises <details> pour trouver la fin — toute occurrence de
    ces balises dans la consigne est donc neutralisée. La ligne summary doit tenir
    sur une ligne (le regex summary n'a pas le flag s).
    """
    safe = instruction.replace("<details", "&lt;details").replace("</details", "&lt;/details")
    summary = _DETAILS_SUMMARY.replace("\n", " ")
    return "<details>\n<summary>" + summary + "</summary>\n\n" + safe + "\n\n</details>"


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
    """Historique COMPLET du chat via l'API : le body de l'action ne contient que la
    chaîne jusqu'au message cliqué — on veut la fenêtre de contexte réellement en cours."""
    import aiohttp

    url = f"{_completion_base(request, base_url_override)}/api/v1/chats/{chat_id}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=_completion_headers(request)) as response:
            if response.status != 200:
                raw = await response.text()
                raise RuntimeError(f"GET /chats HTTP {response.status} : {raw[:200]}")
            data = await response.json()
    # Shape réel 0.13 (routes/chats.py, ChatResponse) : l'historique est IMBRIQUÉ
    # dans data['chat'] = {"history": {...}, "messages": [...]}. Les vieilles versions
    # l'exposaient au niveau racine ; certains déploiements renvoient 'chat' en
    # chaîne JSON. On normalise les trois.
    data = data or {}
    chat = data.get("chat")
    if isinstance(chat, str):
        try:
            chat = json.loads(chat)
        except Exception:
            chat = {}
    chat = chat if isinstance(chat, dict) else {}
    return chat.get("history") or data.get("history") or {}


def _chain_from_history(history: dict) -> list:
    """Chaîne linéaire user/assistant du chat, du plus ancien au leaf currentId
    (reconstruction via parentId), prête à être rejouée au modèle."""
    messages_map = history.get("messages") or {}
    current_id = history.get("currentId")
    chain = []
    seen = set()
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
    if len(chain) > _MAX_PREFIX_MESSAGES:  # même garde-fou que _build_prefix
        chain = chain[-_MAX_PREFIX_MESSAGES:]
    return chain


async def _run_completion(
    request,
    model_id: str,
    prefix: list,
    instruction: str,
    web_search_enabled: bool,
    timeout_seconds: int,
    base_url_override: str,
    session_id: str | None = None,
    search_mode: str = "native",
    emitter=None,
):
    """MODE DE REPLI (chat non sauvegardé / API chat injoignable) : self-call en
    complétion temporaire, préfixe rejoué + consigne en fin de liste ; le rapport est
    ensuite fusionné par l'action dans le message cliqué (contrat historique v3).

    Quand le chat est sauvegardé, c'est _submit_chat_turn (tâche de fond serveur) qui
    est utilisé : le résultat devient un vrai tour de la conversation, répondu par le
    modèle du chat dans son propre contexte (continuité KV cache incluse, cf. sa
    docstring).
    """
    payload = {
        "model": model_id,
        "messages": list(prefix) + [{"role": "user", "content": instruction}],
        "stream": True,
        "features": {"web_search": bool(web_search_enabled)},
    }
    if web_search_enabled and search_mode == "legacy":
        # Force le chemin RAG du middleware (recherches effectuées AVANT la génération),
        # y compris pour les modèles sans function calling natif.
        payload["params"] = {"function_calling": "legacy"}
    if session_id:
        payload["session_id"] = session_id

    return await _stream_payload(request, payload, base_url_override, timeout_seconds, relay_status_to=emitter)


async def _submit_chat_turn(
    request,
    model_id: str,
    chain: list,
    user_message: dict,
    assistant_message_id: str,
    chat_id: str,
    session_id: str,
    web_search_enabled: bool,
    base_url_override: str,
    search_mode: str = "legacy",
):
    """MODE PRINCIPAL : soumet la vérification au CHAT PRINCIPAL comme un tour normal
    de la conversation — exactement comme si l'utilisateur avait tapé « vérifie ta
    réponse » : le modèle du chat répond dans SON contexte, en nouveau message.

    Points vérifiés dans le code 0.13.0 (main.py, route chat_completion) :
      - session_id ET chat_id ensemble => branche TÂCHE DE FOND (create_task,
        open_webui/tasks.py) : la génération tourne dans une tâche asyncio détachée
        de la requête HTTP ; la réponse est un accusé JSON quasi immédiat
        {'status': True, 'task_ids': [...], 'chat_id'}. AUCUNE connexion SSE longue
        côté action : plus de timeout en cours de génération, et le cycle de vie du
        tour est celui d'un message normal (annulable depuis l'UI, persisté, diffusé
        sur les sockets de la conversation) ;
      - le serveur insère lui-même le message user (form_data['user_message']) et le
        placeholder assistant (form_data['id']) via Chats.insert_chat_turn, puis
        diffuse 'chat:messages' : le front affiche le tour immédiatement ;
      - la réponse est persistée dans le chat (comme un message ordinaire).

    stream=True : dans la tâche de fond, le middleware garde le chemin streaming
    (outils natifs possibles selon le mode ; deltas diffusés aux clients du chat).
    Le préfixe rejoué est la chaîne exacte du chat (continuité KV cache).
    """
    import aiohttp

    payload = {
        "model": model_id,
        "messages": list(chain) + [user_message],
        "user_message": user_message,
        "id": assistant_message_id,
        "chat_id": chat_id,
        "session_id": session_id,
        "stream": True,
        "features": {"web_search": bool(web_search_enabled)},
    }
    if web_search_enabled and search_mode == "legacy":
        payload["params"] = {"function_calling": "legacy"}

    url = f"{_completion_base(request, base_url_override)}/api/chat/completions"
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=_completion_headers(request)) as response:
            if response.status != 200:
                raw = await response.text()
                raise RuntimeError(f"HTTP {response.status} : {raw[:300]}")
            ack = await response.json()
    if not isinstance(ack, dict) or not ack.get("status"):
        raise RuntimeError(f"accusé de tâche inattendu : {str(ack)[:200]}")
    return ack


async def _await_chat_message(request, chat_id: str, message_id: str, timeout_seconds: int, base_url_override: str, poll_seconds: int = 5):
    """Attend la fin de génération en relisant l'ÉTAT RÉEL du message côté serveur
    (source de vérité) : done + contenu, ou erreur. Indépendant de tout flux SSE."""
    deadline = time.monotonic() + timeout_seconds + 15
    state: dict = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_seconds)
        state = await _chat_message_state(request, chat_id, message_id, base_url_override)
        if state.get("error") or (state.get("done") and (state.get("content") or state.get("output"))):
            return state
        if state.get("done"):
            return state
    return state


async def _stream_payload(request, payload, base_url_override, timeout_seconds, relay_status_to=None):
    """Transport SSE partagé : agrège le contenu du flux. relay_status_to re-émet les
    événements de statut du backend vers le message vérifié (mode repli uniquement —
    en mode chat, le middleware les émet déjà vers la conversation elle-même)."""
    import aiohttp

    base_url = _completion_base(request, base_url_override)
    url = f"{base_url}/api/chat/completions"
    headers = _completion_headers(request)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    pieces: list[str] = []
    chunk_count = 0
    first_chunk_seen = False

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                raw = await response.text()
                raise RuntimeError(f"HTTP {response.status} : {raw[:300]}")

            # Parse SSE avec buffer : les frontières de chunks réseau ne coïncident
            # pas avec les lignes "data:" (iter_lines n'existe pas chez aiohttp).
            buffer = ""
            stream_done = False

            async def _handle_line(line):
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
                return await _consume_chunk(chunk)

            async def _consume_chunk(chunk):
                nonlocal chunk_count, first_chunk_seen
                chunk_count += 1

                error = chunk.get("error")
                if error:
                    detail = error if isinstance(error, str) else str(error)
                    raise RuntimeError(f"erreur du backend pendant le flux : {detail[:300]}")

                # Le flux SSE peut porter les événements de statut (puces "recherche
                # web en cours") : on les re-émet vers le message vérifié pour que
                # l'utilisateur voie l'activité réelle de recherche (mode repli).
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
                                    {"type": "status", "data": {"description": "Rédaction du rapport de vérification…"}},
                                )
                        pieces.append(piece)
                return None

            async for raw in response.content.iter_any():
                if stream_done:
                    break
                buffer += raw.decode("utf-8", errors="replace")
                while True:
                    newline = buffer.find("\n")
                    if newline == -1:
                        break
                    line, buffer = buffer[:newline], buffer[newline + 1 :]
                    if await _handle_line(line) == "done":
                        stream_done = True
                        break
            if not stream_done and buffer.strip():
                await _handle_line(buffer)

    content = "".join(pieces).strip()
    if not content and chunk_count == 0:
        raise RuntimeError("flux SSE vide : aucune donnée reçue du backend")
    return content


async def _chat_message_state(request, chat_id, message_id, base_url_override=""):
    """État réel d'un message du chat côté serveur (source de vérité), indépendant
    du flux SSE de suivi : le middleware persiste et termine le tour même si le
    consommateur SSE (l'action) a été interrompu."""
    try:
        history = await _fetch_chat_history(request, chat_id, base_url_override)
    except Exception:
        return {}
    return (history.get("messages") or {}).get(message_id) or {}


async def _complete_chat_turn(request, model_id, chain, user_message, assistant_id, chat_id, web_search, valves, emitter):
    """Achèvement en arrière-plan du tour de vérification : soumission en TÂCHE DE
    FOND côté serveur OWUI (session_id+chat_id), puis suivi par relecture de l'état
    réel du message. Logs/status/toasts émis sur le message cliqué.

    Robustesse : la génération ne dépend plus d'aucune connexion HTTP maintenue par
    l'action (pas de timeout SSE, survit aux déconnexions navigateur comme un message
    normal, annulable depuis l'UI). La tâche asyncio de suivi survit elle aussi à la
    requête de l'action (fire-and-forget).
    """
    progress = asyncio.create_task(_progress_task(emitter, "Vérification en cours (recherches web)"))
    started = time.monotonic()
    submit_error = None
    try:
        try:
            ack = await _submit_chat_turn(
                request,
                model_id,
                chain,
                user_message,
                assistant_id,
                chat_id,
                # session_id factice : c'est le déclencheur de la branche tâche de
                # fond (main.py) ; il ne correspond à aucune connexion socket.
                str(uuid.uuid4()),
                web_search,
                valves.base_url,
                valves.search_mode if web_search else "legacy",
            )
            task_ids = ack.get("task_ids") or []
            await _log(emitter, f"Vérification lancée en tâche de fond serveur (task {(task_ids[0] if task_ids else '?')[:8]}).")
        except Exception as exc:
            log.exception("fact-check: la soumission de la tâche a échoué")
            submit_error = exc
            await _log(
                emitter,
                f"Soumission non confirmée ({exc}) — la requête a pu malgré tout atteindre le serveur : "
                "suivi de l'état réel du message avant de conclure.",
                "warn",
            )
        # L'état réel du message est la source de vérité dans TOUS les cas (pas de
        # flux SSE à maintenir côté action). Si la soumission n'est pas confirmée,
        # on raccourcit l'attente : une tâche démarrée apparaîtrait rapidement.
        wait_seconds = valves.timeout_seconds if submit_error is None else min(60, valves.timeout_seconds)
        state = await _await_chat_message(request, chat_id, assistant_id, wait_seconds, valves.base_url)
    finally:
        progress.cancel()

    duration = round(time.monotonic() - started, 1)
    content = state.get("content") or ""
    if state.get("error"):
        await _log(emitter, f"Le serveur a marqué le tour en erreur : {str(state.get('error'))[:200]}", "error")
        await _emit(emitter, {"type": "status", "data": {"description": "Échec côté serveur — voir les logs.", "done": True}})
        await _emit(emitter, {"type": "notification", "data": {"type": "error", "content": "Vérification web échouée (erreur serveur) — détail dans les logs."}})
        return
    if submit_error is not None and not state:
        await _log(emitter, f"Échec de la vérification : {submit_error}", "error")
        await _emit(emitter, {"type": "status", "data": {"description": "Échec de la vérification — détail dans les logs.", "done": True}})
        await _emit(emitter, {"type": "notification", "data": {"type": "error", "content": "Vérification web échouée — détail dans les logs de l'action."}})
        return
    if not state.get("done") or not content:
        await _log(emitter, f"Délai dépassé ({wait_seconds} s) sans réponse terminée.", "error")
        await _emit(emitter, {"type": "status", "data": {"description": "Délai dépassé — voir les logs.", "done": True}})
        await _emit(emitter, {"type": "notification", "data": {"type": "warning", "content": "Vérification web : délai dépassé sans réponse terminée."}})
        return

    # Heuristique anti-mirage : un vrai fact-check sourcé contient des URL.
    if web_search and "http" not in content.lower():
        await _log(
            emitter,
            "Aucune URL de source dans la réponse : la recherche web n'a probablement PAS été "
            "exécutée par le modèle (function calling non supporté ?) — essayez search_mode='legacy'.",
            "warn",
        )

    if submit_error is not None:
        await _log(emitter, "La soumission n'avait pas été confirmée, mais le tour s'est bien exécuté et achevé côté serveur.", "warn")

    await _log(emitter, f"Vérification terminée en {duration} s ({len(content)} caractères).")
    await _emit(emitter, {"type": "status", "data": {"description": "Vérification terminée.", "done": True}})
    await _emit(
        emitter,
        {"type": "notification", "data": {"type": "success", "content": f"Vérification web terminée en {duration} s — la réponse est dans le chat."}},
    )


class Action:
    description: str = "Vérifie les faits d'une réponse du modèle via la recherche web de l'instance"

    class Valves(BaseModel):
        language: str = Field(default="auto", description="'auto' ou code de langue ('fr', 'en')")
        max_checks: int = Field(default=8, ge=1, le=25, description="Max d'affirmations vérifiées par message")
        min_searches: int = Field(default=3, ge=1, le=20, description="Recherches web minimales avant de conclure")
        max_searches: int = Field(
            default=6, ge=1, le=25,
            description="Plafond dur d'appels search_web (anti-boucle/saturation contexte), mode natif",
        )
        include_correction: bool = Field(default=True, description="Ajouter une version corrigée du texte")
        web_search: bool = Field(
            default=True,
            description="Utiliser la recherche web de l'instance (elle doit être activée et configurée côté admin)",
        )
        search_mode: str = Field(
            default="native",
            json_schema_extra={"input": {"type": "select", "options": ["native", "legacy"]}},
            description="'native' = outils de recherche pour le modèle (function calling) ; "
            "'legacy' = recherches pré-injectées (modèles sans function calling)",
        )
        strict_search: bool = Field(
            default=True,
            description="Refuser de vérifier si la recherche web est impossible "
            "(sinon le rapport n'a aucune valeur)",
        )
        debug: bool = Field(
            default=False,
            description="Journaliser la configuration résolue (version backend, modèle, mode, moteur de recherche)",
        )
        completion_model: str = Field(
            default="", description="Modèle utilisé pour la vérification (vide = modèle de la conversation)"
        )
        base_url: str = Field(
            default="", description="URL interne du backend Open WebUI pour l'appel de complétion (vide = auto)"
        )
        timeout_seconds: int = Field(default=300, ge=30, le=900, description="Délai maximum de la vérification")
        max_chars: int = Field(
            default=16000, ge=500, le=32000, description="Longueur maximale du texte soumis à vérification"
        )
        priority: int = Field(default=0, description="Ordre d'affichage du bouton (plus bas = plus à gauche)")

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
        message_id, full_content, user_prompt, target_idx = _extract_target(body)
        if not message_id or not full_content:
            await _log(__event_emitter__, "Fact-check : rien à vérifier dans ce message.")
            return None

        if __request__ is None:
            await _log(__event_emitter__, "Fact-check : contexte '__request__' indisponible, impossible de relancer de complétion.", "error")
            return None

        valves = self.valves
        model_id = (valves.completion_model or "").strip() or body.get("model") or (__model__ or {}).get("id")
        if not model_id:
            await _log(__event_emitter__, "Fact-check : modèle à interroger indisponible.", "error")
            return None

        # Feedback immédiat et PERSISTANT dès le clic : le panneau View Logs du message
        # garde la trace du déroulé, même après la fin de la vérification.
        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Vérification factuelle : démarrage…"}})
        # Toast immédiat : le clic doit produire un feedback visible dans la seconde
        # (la ligne de status seule est trop discrète sur un message déjà terminé).
        await _toast(
            __event_emitter__,
            "info",
            "Vérification web lancée — cela prend généralement 30 à 70 s.",
        )
        await _log(__event_emitter__, f"Vérification lancée sur le message (modèle : {model_id}).")

        # Préfixe de repli : messages du front jusqu'au message vérifié inclus
        # (continuité KV cache). En mode principal (chat sauvegardé), c'est la chaîne
        # complète du chat qui est rejouée, ce préfixe ne sert que de secours.
        prefix, truncated = _build_prefix(body.get("messages") or [], target_idx, valves)
        if truncated:
            await _log(__event_emitter__, f"Contexte réduit pour la vérification (max_chars={valves.max_chars}).", "warn")

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
                f"moteur recherche={engine}, base_url={valves.base_url or 'auto'}",
            )

        web_search = bool(valves.web_search)
        if web_search:
            searchable, blocking = await _web_search_readiness(__request__, model_id, valves.search_mode)
            for reason in blocking:
                await _log(__event_emitter__, f"Pré-vol recherche web : {reason}", "warn")
            if blocking and valves.strict_search:
                await _emit(
                    __event_emitter__,
                    {"type": "status", "data": {"description": "Vérification impossible : voir les logs du message.", "done": True}},
                )
                await _log(__event_emitter__, "Vérification interrompue (strict_search). Corrigez la config, ou désactivez strict_search.", "error")
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

        chat_id = body.get("chat_id")
        if _is_saved_chat_id(chat_id):
            # MODE PRINCIPAL : la vérification devient un tour réel du chat (le modèle
            # du chat répond dans la conversation). FIRE-AND-FORGET OBLIGATOIRE : si
            # l'action attend la fin de la complétion (60-120 s), la coupure du fetch
            # navigateur (timeout proxy, onglet refermé) annule en cascade le self-call
            # interne et laisse un message assistant VIDE dans le chat (prouvé en prod :
            # "Chat processing was cancelled"). La tâche asyncio survive à la requête.
            try:
                history = await _fetch_chat_history(__request__, chat_id, valves.base_url)
                chain = _chain_from_history(history)
            except Exception as exc:
                await _log(__event_emitter__, f"Historique du chat illisible ({exc}) — repli sur le contexte du message cliqué.", "warn")
                chain = prefix
            if not chain:
                chain = prefix
            # La cible n'est "dernière réponse" que si c'est réellement le leaf
            # du chat (sinon la consigne la désigne par l'extrait SUJETS).
            target_is_last = bool(chain) and chain[-1].get("id") == message_id
            instruction = _build_instruction(valves, web_search, full_content[:600], target_is_last)
            user_message = {
                "id": str(uuid.uuid4()),
                "role": "user",
                # parentId OBLIGATOIRE : sans parent, insert_chat_turn (models/chats.py)
                # rattache le tour à la RACINE de l'arbre et la conversation visible
                # serait coupée de son passé. On greffe sur le leaf courant.
                "parentId": chain[-1].get("id") if chain else None,
                # Bloc <details> repliable : bulle compacte dans le chat, consigne
                # intégrale conservée en base (donc dans le prompt du modèle).
                "content": _hide_instruction_block(instruction),
                "models": [model_id],
                "timestamp": int(time.time()),
            }
            await _log(
                __event_emitter__,
                f"Vérification soumise au chat ({len(chain)} messages rejoués) : "
                "la réponse du modèle s'écrira comme nouveau message de la conversation.",
            )
            task = asyncio.create_task(
                _complete_chat_turn(
                    __request__, model_id, chain, user_message, str(uuid.uuid4()), chat_id, web_search, valves, __event_emitter__
                )
            )
            _PENDING_TASKS.add(task)
            task.add_done_callback(_PENDING_TASKS.discard)
            # Laisse décoller la requête interne (insert_chat_turn crée immédiatement
            # le message user + le placeholder assistant) sans bloquer le client.
            await asyncio.sleep(1.5)
            return None

        # MODE REPLI (chat temporaire) : contrat de fusion dans le message cliqué.
        instruction = _build_instruction(valves, web_search, full_content[:600], True)
        progress = asyncio.create_task(_progress_task(__event_emitter__, "Vérification en cours (recherches web)"))
        started = time.monotonic()
        try:
            report = await _run_completion(
                __request__,
                model_id,
                prefix,
                instruction,
                web_search,
                valves.timeout_seconds,
                valves.base_url,
                body.get("session_id"),
                valves.search_mode if web_search else "native",
                emitter=__event_emitter__,
            )
        except Exception as exc:
            log.exception("fact-check: la vérification a échoué")
            await _log(__event_emitter__, f"Échec de la vérification : {exc}", "error")
            await _emit(
                __event_emitter__,
                {"type": "status", "data": {"description": "Échec de la vérification — détail dans les logs.", "done": True}},
            )
            # Toast visible même si l'utilisateur a quitté la conversation.
            await _emit(
                __event_emitter__,
                {"type": "notification", "data": {"type": "error", "content": "Vérification web échouée — détail dans les logs de l'action."}},
            )
            return None
        finally:
            progress.cancel()

        duration = round(time.monotonic() - started, 1)
        if not report:
            await _log(__event_emitter__, "Le modèle n'a produit aucun rapport de vérification.", "error")
            await _emit(
                __event_emitter__,
                {"type": "status", "data": {"description": "Aucun rapport produit — voir les logs.", "done": True}},
            )
            await _emit(
                __event_emitter__,
                {"type": "notification", "data": {"type": "warning", "content": "Vérification web : aucun rapport produit (voir les logs de l'action)."}},
            )
            return None

        # Heuristique anti-mirage : un vrai fact-check sourcé contient des URL.
        # Un modèle sans outils récite "je lance les recherches" puis s'arrête.
        if web_search and "http" not in report.lower():
            await _log(
                __event_emitter__,
                "Aucune URL de source dans le rapport : la recherche web n'a probablement PAS été "
                "exécutée par le modèle (function calling non supporté ?) — essayez search_mode='legacy'.",
                "warn",
            )

        await _log(__event_emitter__, f"Vérification terminée en {duration} s ({len(report)} caractères de rapport).")
        await _emit(__event_emitter__, {"type": "status", "data": {"description": "Vérification terminée.", "done": True}})
        await _emit(
            __event_emitter__,
            {"type": "notification", "data": {"type": "success", "content": f"Vérification web terminée en {duration} s — rapport ajouté au message."}},
        )

        merged = f"{full_content}\n\n---\n\n{report}"
        # REPLI (chat non sauvegardé) : les messages retournés doivent porter l'"id"
        # d'un message EXISTANT de l'historique ; le front fusionne le contenu par id
        # et conserve l'original dans "originalContent". Il ne relance JAMAIS une
        # génération à partir d'un message retourné.
        return {"messages": [{"id": message_id, "role": "assistant", "content": merged}]}
