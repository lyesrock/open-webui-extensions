"""Test du plugin Action 'Vérifier sur le web' (v5) contre le contrat réel d'Open WebUI 0.13.

v5 : le résultat devient un TOUR RÉEL DU CHAT via la branche TÂCHE DE FOND du backend
(session_id + chat_id => create_task détaché, accusé JSON immédiat), puis suivi par
relecture de l'état réel du message. Le mode repli (chat non sauvegardé) garde le
contrat historique de fusion par id. On reproduit le chargeur backend (utils/plugin.py),
l'accusé JSON de /api/chat/completions en mode tâche, l'API GET /api/v1/chats/{id},
et le contrat front des messages retournés.
"""

import asyncio
import inspect
import json
import re
import sys
import time
import types

sys.stdout.reconfigure(encoding="utf-8")

import os
PLUGIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factcheck_web.py")

# ---------------------------------------------------------------------------
# 1. Réplique exacte du parseur de frontmatter d'Open WebUI (utils/plugin.py)
# ---------------------------------------------------------------------------
frontmatter_pattern = re.compile(r"^\s*([a-z_]+):\s*(.*)\s*$", re.IGNORECASE)


def extract_frontmatter(content):
    frontmatter = {}
    lines = content.splitlines()
    if len(lines) < 1 or lines[0].strip() != '"""':
        return {}
    for line in lines[1:]:
        if '"""' in line:
            break
        match = frontmatter_pattern.match(line)
        if match:
            frontmatter[match.group(1).strip()] = match.group(2).strip()
    return frontmatter


content = open(PLUGIN_PATH, encoding="utf-8").read()
fm = extract_frontmatter(content)
assert fm.get("title") == "Vérifier sur le web", fm
assert fm.get("required_open_webui_version") == "0.13.0", fm

# ---------------------------------------------------------------------------
# 2. Chargement à la manière de load_function_module_by_id
# ---------------------------------------------------------------------------
module = types.ModuleType("function_factcheck")
exec(content, module.__dict__)

assert not hasattr(module, "Pipe") and not hasattr(module, "Filter") and hasattr(module, "Action")
instance = module.Action()
assert hasattr(instance, "valves") and hasattr(instance, "Valves")
assert getattr(module.Action, "description", "")

sig = inspect.signature(instance.action)
for p in ("body", "__user__", "__model__", "__request__", "__event_emitter__", "__event_call__"):
    assert p in sig.parameters, f"paramètre {p} absent"
REAL_FETCH_HISTORY = module._fetch_chat_history
REAL_RUN_COMPLETION = module._run_completion
REAL_SUBMIT = module._submit_chat_turn
REAL_AWAIT = module._await_chat_message
DONE_REPORT = "## Vérification factuelle\n\n**Verdict global :** Contient des erreurs\n\nSources : [INSEE](https://insee.fr)"
ACK = lambda chat_id: {"status": True, "task_ids": ["t1"], "chat_id": chat_id}
print("OK 1  chargement frontmatter/type/Valves/signature")

# ---------------------------------------------------------------------------
# Helpers de test
# ---------------------------------------------------------------------------
class FakeURL:
    scheme = "http"
    netloc = "127.0.0.1:18099"


class FakeRequest:
    url = FakeURL()
    headers = {"authorization": "Bearer fake-token"}

    class app:
        class state:
            MODELS = {}


events = []


async def emitter(event):
    events.append(event)


def log_lines():
    out = []
    for e in events:
        if e.get("type") == "logs":
            out.extend(entry["message"] for entry in e["data"]["logs"])
    return "\n".join(out)


def toasts():
    return [e for e in events if e.get("type") == "notification"]


async def _run_saved(body, inst=None):
    """Exécute l'action en mode chat (fire-and-forget) puis attend les tâches lancées."""
    res = await (inst or instance).action(body=body, __request__=FakeRequest(), __event_emitter__=emitter)
    pending = list(module._PENDING_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return res


# (fixtures BODY_* définies après body_msgs, section 3)

# ---------------------------------------------------------------------------
# 3. Unités : préfixe, instruction (cible dernière vs milieu), garde-fous
# ---------------------------------------------------------------------------
body_msgs = [
    {"id": "q1", "role": "user", "content": "question 1"},
    {"id": "a1", "role": "assistant", "content": "GLPI 11 exige 128 Mo de RAM et MySQL 5.1."},
    {"id": "q2", "role": "user", "content": "et Apache ?"},
    {"id": "a2", "role": "assistant", "content": "Apache 2.4 recommandé."},
]
prefix, truncated = module._build_prefix(body_msgs, 1, instance.valves)
assert not truncated
assert [m["id"] for m in prefix] == ["q1", "a1"], "le préfixe doit s'arrêter à la cible incluse"

# Chat SAUVEGARDÉ dont le leaf est a2 ; clic sur a2 = cas principal v4.
BODY_SAVED = {
    "model": "modele-test",
    "chat_id": "11111111-2222-3333-4444-555555555555",
    "id": "a2",
    "messages": [dict(m) for m in body_msgs],
}
# Chat NON sauvegardé (préfixe 'temporary:') => contrat de fusion historique.
BODY_TEMP = {
    "model": "modele-test",
    "chat_id": "temporary:sess-1:9a9a9a9a-0000-0000-0000-000000000000",
    "session_id": "sess-1",
    "id": "a2",
    "messages": [dict(m) for m in body_msgs],
}

instr_last = module._build_instruction(instance.valves, True, "extrait RAM", True)
assert "TA DERNIÈRE RÉPONSE" in instr_last, "la consigne doit désigner la dernière réponse"
assert "SUJETS DE VÉRIFICATION" in instr_last and "extrait RAM" in instr_last
assert "appelle-les directement" in instr_last, "consigne native anti-je-lance-les-recherches absente"
assert "plafond dur" in instr_last and "ARRÊT OBLIGATOIRE" in instr_last, "plafond anti-boucle natif absent"
assert "entre 3 et 6 appels AU TOTAL" in instr_last, "budget de recherches natif (min..max) absent"
assert len(instr_last) < 4000, "la consigne doit rester courte (pas de duplication du texte entier)"

instr_mid = module._build_instruction(instance.valves, True, "extrait RAM", False)
assert "TA DERNIÈRE RÉPONSE" not in instr_mid
assert "réponse ciblée" in instr_mid, "cible en milieu de chat : désignation par l'extrait"

instr_nosearch = module._build_instruction(instance.valves, False, "", False)
assert "AUCUN outil de recherche web" in instr_nosearch
print("OK 2  préfixe KV cache + désignation cible (dernière / milieu) + consigne courte")

# ---------------------------------------------------------------------------
# 2b. Emballage <details> (bulle compacte, rendu natif replié OWUI 0.13)
# ---------------------------------------------------------------------------
hidden = module._hide_instruction_block(instr_last)
assert hidden.startswith("<details>\n") and hidden.endswith("\n\n</details>")
# Simulation des regex exacts du detailsTokenizer d'OWUI (utils/marked/extension.ts) :
m = re.match(r"^<details(\s+[^>]*)?>\n", hidden)
assert m, "<details> doit être en début de ligne, suivi d'un saut de ligne"
# slice(detailsTag.length, -10).trim() dans le tokenizer : on retire la balise fermante
body = hidden[m.end():][: -len("</details>")].strip()
ms = re.match(r"^<summary>(.*?)</summary>\n", body)
assert ms, "summary attendu seul sur sa ligne"
assert "\n" not in ms.group(1), "le summary doit tenir sur une ligne"
inner = body[ms.end():].strip()
assert inner == instr_last, "consigne intégrale restaurée pour le modèle après retrait du bloc"
# profondeur de balises (findMatchingClosingTag) : ouvrante/fermante uniques dans le brut
assert hidden.count("<details") == 1 and hidden.count("</details") == 1
# consigne piégée contenant des balises details : neutralisée, jamais injectée
piège = module._hide_instruction_block("texte <details>piège</details> suite")
assert piège.count("<details") == 1 and piège.count("</details") == 1
assert "&lt;details>piège&lt;/details>" in piège
print("OK 2b emballage details : parseur OWUI simulé, consigne restaurée, balises piégées neutralisées")

# ---------------------------------------------------------------------------
# 4. Unités : chaînes de chat 0.13 (is_saved_chat_id, _chain_from_history)
# ---------------------------------------------------------------------------
assert module._is_saved_chat_id("11111111-2222-3333-4444-555555555555") is True
assert module._is_saved_chat_id("temporary:abc") is False
assert module._is_saved_chat_id("local:xyz") is False
assert module._is_saved_chat_id("channel:42") is False
assert module._is_saved_chat_id(None) is False

history = {
    "currentId": "a2",
    "messages": {
        "q1": {"role": "user", "content": "question 1", "parentId": None},
        "a1": {"role": "assistant", "content": "reponse 1", "parentId": "q1", "sources": [{"url": "x"}]},
        "q2": {"role": "user", "content": "question 2", "parentId": "a1"},
        "a2": {"role": "assistant", "content": "reponse 2", "parentId": "q2"},
        # branche morte (ré-génération) qui ne doit PAS être rejouée :
        "a1bis": {"role": "assistant", "content": "REGENERATION HORS CHENE", "parentId": "q1"},
        # message système hors chain :
        "sys": {"role": "system", "content": "blabla système", "parentId": None},
    },
}
chain = module._chain_from_history(history)
assert [m["id"] for m in chain] == ["q1", "a1", "q2", "a2"], f"chaîne linéaire du leaf attendue : {chain}"
assert all(m["role"] in ("user", "assistant") for m in chain)
assert "REGENERATION" not in json.dumps(chain, ensure_ascii=False)
assert chain[1]["sources"] == [{"url": "x"}], "les sources doivent être conservées (contexte des réponses)"
assert module._chain_from_history({}) == []

huge_hist = {"currentId": "m199", "messages": {}}
parent = None
for i in range(200):
    huge_hist["messages"][f"m{i}"] = {"role": "user" if i % 2 == 0 else "assistant", "content": f"c{i}", "parentId": parent}
    parent = f"m{i}"
big_chain = module._chain_from_history(huge_hist)
assert len(big_chain) == module._MAX_PREFIX_MESSAGES and big_chain[-1]["id"] == "m199"
print("OK 3  chat_id sauvegardé vs temporary + chaîne linéaire du leaf (branche morte exclue, garde-fou 120)")

# ---------------------------------------------------------------------------
# 5. MODE PRINCIPAL : tour réel du chat (user injecté + id assistant + chat_id)
# ---------------------------------------------------------------------------
captured = {}


async def fake_history(request, chat_id, base_url_override):
    captured["hist_chat"] = chat_id
    return history


async def fake_submit(request, model_id, chain, user_message, assistant_id, chat_id, session_id, ws, base, search_mode="legacy"):
    captured.update(chain=chain, user_msg=user_message, assistant_id=assistant_id, chat=chat_id, sid=session_id, ws=ws, mode=search_mode)
    return ACK(chat_id)


async def fake_await(request, chat_id, message_id, timeout, base, poll_seconds=5):
    captured["awaited_id"] = message_id
    return {"role": "assistant", "content": DONE_REPORT, "done": True, "parentId": captured["user_msg"].get("id")}


module._fetch_chat_history = fake_history
module._submit_chat_turn = fake_submit
module._await_chat_message = fake_await
module._web_search_readiness = lambda *a: asyncio.sleep(0, result=(True, []))

events.clear()

async def _ok4():
    res = await instance.action(body=BODY_SAVED, __user__={"id": "u1"}, __model__={"id": "modele-test"},
                                __request__=FakeRequest(), __event_emitter__=emitter, __event_call__=None)
    pending = list(module._PENDING_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return res


result = asyncio.run(_ok4())
assert result is None, "v4 : l'action ne doit PLUS fusionner de message en mode chat"
assert captured["hist_chat"] == BODY_SAVED["chat_id"] and captured["chat"] == BODY_SAVED["chat_id"]
assert [m["id"] for m in captured["chain"]] == ["q1", "a1", "q2", "a2"], "la chaîne complète du chat est rejouée (contexte courant)"
assert captured["user_msg"]["role"] == "user" and captured["user_msg"]["id"], "message user injecté requis"
assert captured["user_msg"].get("parentId") == "a2", "le user injecté doit être greffé sur le leaf (sinon tour orphelin racine)"
assert captured["assistant_id"] and captured["assistant_id"] != captured["user_msg"]["id"]
assert captured["sid"], "session_id obligatoire : c'est le déclencheur de la tâche de fond serveur"
assert captured["awaited_id"] == captured["assistant_id"], "le suivi doit porter sur le placeholder assistant soumis"
assert "Vérification factuelle" in captured["user_msg"]["content"], "l'instruction est le contenu du user injecté"
assert captured["user_msg"]["content"].startswith("<details>"), "mode principal : consigne emballée en bloc repliable"
assert "SUJETS DE VÉRIFICATION" in captured["user_msg"]["content"] and "Apache 2.4" in captured["user_msg"]["content"]
assert "TA DERNIÈRE RÉPONSE" in captured["user_msg"]["content"], "la cible est le leaf => désignation 'dernière réponse'"
assert any("soumise au chat" in log_lines() for _ in [1])
assert "tâche de fond serveur" in log_lines(), "log d'accusé de tâche de fond attendu"
assert any("dans le chat" in str(t) for t in toasts()), "toast de fin orienté vers le chat"
print("OK 4  mode principal : tour réel du chat (chaîne complète + user injecté + aucun merge)")

# clic sur une réponse NON leaf : la consigne doit désigner la cible par l'extrait
events.clear()
captured.clear()
BODY_MID = {
    "model": "modele-test",
    "chat_id": "11111111-2222-3333-4444-555555555555",
    "id": "a1",
    "messages": body_msgs[:2],
}
asyncio.run(_run_saved(BODY_MID))
assert "réponse ciblée" in captured["user_msg"]["content"], "cible hors leaf : désignation par extrait"
assert "GLPI 11 exige 128 Mo" in captured["user_msg"]["content"], "extrait de la cible dans SUJETS"
assert captured["assistant_id"] != "a1", "le nouveau message assistant n'est pas la cible"
print("OK 5  clic hors leaf : cible désignée par extrait, nouvelle réponse en leaf")

# repli si l'historique est injoignable : on rejoue le préfixe du clic
async def broken_history(request, chat_id, base):
    raise RuntimeError("GET /chats HTTP 404")


events.clear()
captured.clear()
real_hist_fn = module._fetch_chat_history
module._fetch_chat_history = broken_history
out = asyncio.run(_run_saved(BODY_SAVED))
assert out is None
assert [m["id"] for m in captured["chain"]] == ["q1", "a1", "q2", "a2"], "repli sur le préfixe du clic"
assert captured["chain"][-1]["id"] == "a2" and "Apache 2.4" in captured["chain"][-1]["content"]
assert "Historique du chat illisible" in log_lines() and "repli" in log_lines()
module._fetch_chat_history = real_hist_fn
print("OK 6  historique injoignable : repli préfixe du clic, log de secours")

# ---------------------------------------------------------------------------
# 7. MODE REPLI (temporary) : contrat historique de fusion par id
# ---------------------------------------------------------------------------
async def fake_completion(request, model_id, prefix, instruction, ws, timeout, base, sid=None, search_mode="native", emitter=None):
    captured.update(model_id=model_id, prefix=prefix, instruction=instruction, ws=ws, sid=sid, mode=search_mode)
    return "## Vérification factuelle\n\n**Verdict global :** Exact\n\nSources : [INSEE](https://insee.fr)"


module._run_completion = fake_completion
events.clear()
captured.clear()
result = asyncio.run(instance.action(body=BODY_TEMP, __request__=FakeRequest(), __event_emitter__=emitter))
assert result and result.get("messages"), "le repli doit retourner le contrat de fusion"
msg = result["messages"][0]
assert msg["id"] == "a2" and msg["role"] == "assistant"
assert msg["content"].startswith("Apache 2.4 recommandé."), "texte d'origine conservé (fusion)"
assert "Verdict global" in msg["content"]
assert captured["sid"] == "sess-1" and captured["mode"] == "native"
assert captured["prefix"][-1]["id"] == "a2" and captured["prefix"][0]["id"] == "q1"
assert any("rapport ajouté au message" in str(t) for t in toasts())
print("OK 7  repli temporary : fusion par id + session_id + toast 'ajouté au message'")

# ---------------------------------------------------------------------------
# 8. Progression keepalive pendant un appel long (mode chat)
# ---------------------------------------------------------------------------
module._PROGRESS_INTERVAL = 0.2


async def slow_await(request, chat_id, message_id, timeout, base, poll_seconds=5):
    await asyncio.sleep(0.7)
    return {"role": "assistant", "content": "## Rapport https://exemple.fr", "done": True}


module._await_chat_message = slow_await
events.clear()
asyncio.run(_run_saved(BODY_SAVED))
progress_status = [e for e in events if e.get("type") == "status" and "écoulées" in str(e.get("data", {}).get("description", ""))]
assert progress_status, "aucun battement de progression pendant l'appel"
print(f"OK 8  battement de progression visible ({len(progress_status)} statut(s))")

# ---------------------------------------------------------------------------
# 9. Pré-vol recherche web : strict (avorté) et permissif (repli sans outils)
# ---------------------------------------------------------------------------
async def no_search(request, model_id, search_mode):
    return False, ["recherche web désactivée côté instance"]


module._submit_chat_turn = fake_submit
module._await_chat_message = fake_await
module._web_search_readiness = no_search

events.clear()
captured.clear()
out = asyncio.run(_run_saved(BODY_SAVED))
assert out is None and "mode" not in {k: v for k, v in captured.items() if k == "ws"}, "strict_search doit avorter"
assert captured.get("hist_chat") is None, "aucune complétion ne doit être lancée en strict"
assert "interrompue" in log_lines()

inst_perm = module.Action()
inst_perm.valves = inst_perm.Valves(strict_search=False)
events.clear()
captured.clear()
out = asyncio.run(_run_saved(BODY_SAVED, inst_perm))
assert out is None and captured.get("chat"), "strict_search=False doit quand même soumettre le tour"
assert captured["ws"] is False, "repli sans recherche attendu"
assert captured["mode"] == "legacy", "sans outils, pas de function calling natif"
assert "AUCUN outil de recherche web" in captured["user_msg"]["content"], "la consigne doit prévenir l'invention de sources"
print("OK 9  pré-vol: strict avorte, permissif soumet sans recherche")

async def unknown(request, model_id, search_mode):
    return None, []


module._web_search_readiness = unknown
captured.clear()
out = asyncio.run(_run_saved(BODY_SAVED))
assert out is None and captured.get("ws") is True, "pré-vol inconnu ne doit pas bloquer"

async def cap_off(request, model_id, search_mode):
    return True, [f"outils web natifs désactivés sur le modèle '{model_id}'"]


module._web_search_readiness = cap_off
captured.clear()
out = asyncio.run(_run_saved(BODY_SAVED))
assert out is None, "capacité web_search désactivée => blocking en strict"
print("OK 10 pré-vol: capacités du modèle respectées, inconnu non bloquant")

# search_mode legacy transmis au tour de chat
module._web_search_readiness = unknown
inst_legacy = module.Action()
inst_legacy.valves = inst_legacy.Valves(search_mode="legacy")
captured.clear()
asyncio.run(_run_saved(BODY_SAVED, inst_legacy))
assert captured["mode"] == "legacy"
print("OK 11 search_mode legacy transmis en mode principal")

# ---------------------------------------------------------------------------
# 12. Vrais appels HTTP contre faux backend : GET chat + POST tâche de fond (accusé JSON)
# ---------------------------------------------------------------------------
module._fetch_chat_history = REAL_FETCH_HISTORY
module._run_completion = REAL_RUN_COMPLETION
module._submit_chat_turn = REAL_SUBMIT
module._await_chat_message = REAL_AWAIT
from aiohttp import web


async def http_test():
    seen = {}
    sse_events = []

    async def emitter12(e):
        sse_events.append(e)

    hist_payload = {"currentId": "a1", "messages": {
        "q1": {"role": "user", "content": "population japon", "parentId": None},
        "a1": {"role": "assistant", "content": "90 millions d'habitants.", "parentId": "q1"},
    }}

    async def chat_handler(req):
        # SHAPE REEL 0.13 (ChatResponse) : history imbrique dans data['chat'].
        return web.json_response({"id": "c1", "chat": {"history": hist_payload}})

    async def chat_str_handler(req):
        # Variante observée ailleurs : 'chat' en chaîne JSON.
        return web.json_response({"id": "c2", "chat": json.dumps({"history": hist_payload})})

    async def chat_legacy_handler(req):
        # Ancienne shape : history a la racine (retro-compat).
        return web.json_response({"id": "c3", "history": hist_payload})

    async def ack_handler(req):
        # Branche tâche de fond : accusé JSON immédiat, PAS de flux SSE.
        seen["path"] = req.path
        seen["auth"] = req.headers.get("Authorization")
        seen["payload"] = await req.json()
        return web.json_response({"status": True, "task_ids": ["task-abc"], "chat_id": "c1"})

    async def sse_good(req):
        seen["payload"] = await req.json()
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        await resp.write(b'data: {"choices":[{"delta":{"content":"## Rapport "}}]}\n\n')
        await resp.write(b'data: {"events":[{"type":"status","data":{"description":"recherche : population japon"}}]}\n\n')
        await resp.write(b'data: {"choices":[{"delta":{"content":"sources: https://exemple.fr"}}]}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/api/v1/chats/c1", chat_handler)
    app.router.add_get("/api/v1/chats/c2", chat_str_handler)
    app.router.add_get("/api/v1/chats/c3", chat_legacy_handler)
    app.router.add_post("/api/chat/completions", ack_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18099).start()

    app_sse = web.Application()
    app_sse.router.add_post("/api/chat/completions", sse_good)
    runner_sse = web.AppRunner(app_sse)
    await runner_sse.setup()
    await web.TCPSite(runner_sse, "127.0.0.1", 18097).start()

    try:
        # GET historique réel : les TROIS shapes doivent se normaliser à l'identique.
        # (Bug prod corrigé ici : la shape imbriquée 'chat' de 0.13 était ignorée,
        # la chaîne retombait silencieusement sur le préfixe du body d'action.)
        hist = await module._fetch_chat_history(FakeRequest(), "c1", "")
        assert hist["currentId"] == "a1" and len(hist["messages"]) == 2
        hist2 = await module._fetch_chat_history(FakeRequest(), "c2", "")
        assert hist2["currentId"] == "a1", "shape 'chat' en chaîne JSON doit être parsée"
        hist3 = await module._fetch_chat_history(FakeRequest(), "c3", "")
        assert hist3["currentId"] == "a1", "shape 'history' à la racine (vintage) doit rester acceptée"
        assert [m["id"] for m in module._chain_from_history(hist)] == ["q1", "a1"]

        user_msg = {"id": "uu1", "role": "user", "content": "CONSIGNE", "models": ["m1"], "timestamp": 1}
        ack = await module._submit_chat_turn(
            FakeRequest(), "m1",
            [{"id": "q1", "role": "user", "content": "population japon"},
             {"id": "a1", "role": "assistant", "content": "90 millions d'habitants."}],
            user_msg, "aa1", "c1", "sess-factice", True, "", "legacy",
        )
        assert ack == {"status": True, "task_ids": ["task-abc"], "chat_id": "c1"}
        assert seen["auth"] == "Bearer fake-token"
        p = seen["payload"]
        assert p["chat_id"] == "c1", "le tour doit viser le chat sauvegardé"
        assert p["session_id"] == "sess-factice", "session_id + chat_id => branche tâche de fond (détachée de la requête)"
        assert p["id"] == "aa1", "id du placeholder assistant transmis (insert_chat_turn)"
        assert p["user_message"] == user_msg, "user message explicite (sinon le serveur invente le parent)"
        assert p["messages"][-1] == user_msg, "la consigne doit fermer la liste des messages"
        assert p["stream"] is True, "stream maintenu : le middleware alimente les clients du chat"
        assert p["params"] == {"function_calling": "legacy"}
        assert "features" in p and p["features"]["web_search"] is True
        assert seen["path"] == "/api/chat/completions"

        # repli temporaire : pas de chat_id/id, session_id présent (inchangé v3)
        seen.clear()
        report2 = await module._run_completion(
            FakeRequest(), "m1", [{"id": "q1", "role": "user", "content": "x"}], "INSTR", True, 60, "http://127.0.0.1:18097", "sess-9", "native", emitter=emitter12,
        )
        assert report2 == "## Rapport sources: https://exemple.fr"
        p2 = seen["payload"]
        assert "chat_id" not in p2 and "id" not in p2, "le repli ne doit jamais écrire dans un chat"
        assert p2["session_id"] == "sess-9"
        assert any("population japon" in str(e) for e in sse_events), "les events backend du repli sont re-émis"
    finally:
        await runner.cleanup()
        await runner_sse.cleanup()

    # erreurs : accusé invalide, HTTP 500, GET 500
    async def bad_ack(req):
        return web.json_response({"status": False, "detail": "nope"})

    async def failing_handler(req):
        return web.Response(status=500, text="upstream exploded")

    app_bad = web.Application()
    app_bad.router.add_post("/api/chat/completions", bad_ack)
    runner_bad = web.AppRunner(app_bad)
    await runner_bad.setup()
    await web.TCPSite(runner_bad, "127.0.0.1", 18096).start()

    app2 = web.Application()
    app2.router.add_post("/api/chat/completions", failing_handler)
    app2.router.add_get("/api/v1/chats/c1", failing_handler)
    runner2 = web.AppRunner(app2)
    await runner2.setup()
    await web.TCPSite(runner2, "127.0.0.1", 18098).start()
    try:
        user_msg = {"id": "uu1", "role": "user", "content": "CONSIGNE", "models": ["m1"], "timestamp": 1}
        try:
            await module._submit_chat_turn(FakeRequest(), "m1", [{"role": "user", "content": "x"}], user_msg, "aa1", "c1", "s", False, "http://127.0.0.1:18096")
            raise AssertionError("un accusé status=False doit lever RuntimeError")
        except RuntimeError as exc:
            assert "accusé de tâche inattendu" in str(exc)
        try:
            await module._submit_chat_turn(FakeRequest(), "m1", [{"role": "user", "content": "x"}], user_msg, "aa1", "c1", "s", False, "http://127.0.0.1:18098")
            raise AssertionError("HTTP 500 doit lever RuntimeError")
        except RuntimeError as exc:
            assert "HTTP 500" in str(exc)
        try:
            await module._fetch_chat_history(FakeRequest(), "c1", "http://127.0.0.1:18098")
            raise AssertionError("GET 500 doit lever")
        except RuntimeError as exc:
            assert "HTTP 500" in str(exc)
    finally:
        await runner_bad.cleanup()
        await runner2.cleanup()


asyncio.run(http_test())
print("OK 12 transport réel : POST tâche de fond (session_id+chat_id+id+user_message, accusé JSON) + GET chat + repli SSE + erreurs")

# ---------------------------------------------------------------------------
# 13. Heuristique anti-mirage (mode repli) + cas limites
# ---------------------------------------------------------------------------
module._web_search_readiness = unknown


async def mirage(*a, **k):
    return "Je lance les recherches de vérification."  # symptôme signalé par l'utilisateur


module._run_completion = mirage
events.clear()
out = asyncio.run(instance.action(body=BODY_TEMP, __request__=FakeRequest(), __event_emitter__=emitter))
assert out is not None
assert "probablement PAS été exécutée" in log_lines(), "le mirage sans URL doit être signalé"
assert "legacy" in log_lines(), "la suggestion du repli legacy doit être dans le log"

async def await_mirage(request, chat_id, message_id, timeout, base, poll_seconds=5):
    return {"role": "assistant", "content": "Je lance les recherches de vérification.", "done": True}


module._submit_chat_turn = fake_submit
module._await_chat_message = await_mirage
events.clear()
out = asyncio.run(_run_saved(BODY_SAVED))
assert out is None and "probablement PAS été exécutée" in log_lines(), "anti-mirage aussi en mode chat"

module._run_completion = fake_completion
events.clear()
no_assistant = asyncio.run(instance.action(body={"model": "m", "chat_id": "temporary:s:1", "messages": [{"id": "u", "role": "user", "content": "salut"}]}, __request__=FakeRequest(), __event_emitter__=emitter))
assert no_assistant is None

empty = asyncio.run(instance.action(body={"model": "m", "id": "e", "chat_id": "temporary:s:2", "messages": [{"id": "e", "role": "assistant", "content": "   "}]}, __request__=FakeRequest(), __event_emitter__=emitter))
assert empty is None

inst2 = module.Action()
inst2.valves = inst2.Valves(max_chars=1000)
captured.clear()
long_body = {"model": "m", "id": "big", "chat_id": "temporary:s:3", "session_id": "s", "messages": [{"id": "big", "role": "assistant", "content": "X" * 5000}]}
out2 = asyncio.run(inst2.action(body=long_body, __request__=FakeRequest(), __event_emitter__=emitter))
assert "tronqué" in json.dumps(captured["prefix"], ensure_ascii=False), "message cible tronqué dans le préfixe"
assert out2["messages"][0]["content"].startswith("X" * 5000), "contenu sauvegardé intégral"


async def boom(*a, **k):
    raise RuntimeError("backend injoignable")


async def fast_empty_await(request, chat_id, message_id, timeout, base, poll_seconds=5):
    return {}


module._submit_chat_turn = boom
module._await_chat_message = fast_empty_await
events.clear()
out3 = asyncio.run(_run_saved(BODY_SAVED))
assert out3 is None
assert "backend injoignable" in log_lines(), "erreur persistée dans les logs du message"
assert any(t["data"]["type"] == "error" for t in toasts()), "toast d'erreur émis"

# tour jamais terminé (done=False, contenu vide) => toast warning délai
async def await_never_done(request, chat_id, message_id, timeout, base, poll_seconds=5):
    return {"role": "assistant", "content": "", "done": False}


module._submit_chat_turn = fake_submit
module._await_chat_message = await_never_done
events.clear()
out4 = asyncio.run(_run_saved(BODY_SAVED))
assert out4 is None
assert any(t["data"]["type"] == "warning" for t in toasts()), "aucun rapport => toast warning"
print("OK 13 anti-mirage (2 modes) + cas limites: jamais terminé, erreur backend, troncature, toasts d'état")

# ---------------------------------------------------------------------------
# 14. Fire-and-forget : l'action revient immédiatement, le tour s'achève quand même
# ---------------------------------------------------------------------------
captured.clear()
events.clear()


async def slow3s_await(request, chat_id, message_id, timeout, base, poll_seconds=5):
    await asyncio.sleep(3)
    captured["finished"] = True
    return {"role": "assistant", "content": "## Vérification factuelle https://exemple.org", "done": True}


module._web_search_readiness = unknown
module._fetch_chat_history = fake_history
module._submit_chat_turn = fake_submit
module._await_chat_message = slow3s_await


async def _ok14():
    t0 = time.monotonic()
    res = await instance.action(body=BODY_SAVED, __request__=FakeRequest(), __event_emitter__=emitter)
    elapsed = time.monotonic() - t0
    pending = list(module._PENDING_TASKS)
    assert res is None
    assert elapsed < 3.0, f"l'action doit revenir en ~1,5 s, pas {elapsed:.1f} s (sinon le fetch navigateur sera coupé par le proxy)"
    assert pending, "le tour doit continuer en arrière-plan (tâche encore en cours)"
    await asyncio.gather(*pending)
    assert captured.get("finished") is True, "le tour doit s'achever même après le retour de l'action"
    return elapsed


t_elapsed = asyncio.run(_ok14())
assert any("la réponse est dans le chat" in str(t) for t in toasts()), "toast de succès final attendu après achèvement"
print(f"OK 14 fire-and-forget : retour en {t_elapsed:.1f} s, tour achevé en arrière-plan, toast de fin")

# ---------------------------------------------------------------------------
# 15. Tolérance : soumission NON confirmée (exception à l'émission) MAIS tour bel
#     et bien achevé côté serveur => succès déclaré (pas de faux échec), d'après
#     l'état réel du chat (source de vérité).
# ---------------------------------------------------------------------------
captured.clear()
events.clear()
GRACE = {}


async def fake_history_done(request, chat_id, base):
    # simule la course observée en prod : le tour n'est pas encore persisté achevé à
    # la premiere sonde, il l'est quelques instants plus tard.
    GRACE["probes"] = GRACE.get("probes", 0) + 1
    if GRACE["probes"] < 3:
        return {"currentId": GRACE["id"], "messages": {GRACE["id"]: {"role": "assistant", "content": "", "done": False, "parentId": "uu1"}}}
    return {"currentId": GRACE["id"], "messages": {
        GRACE["id"]: {"role": "assistant", "content": "## Vérification factuelle https://src.fr", "done": True, "parentId": "uu1"},
    }}


async def submit_lost(request, model_id, chain, user_message, assistant_id, chat_id, session_id, ws, base, search_mode="legacy"):
    GRACE["id"] = assistant_id  # la requête a atteint le serveur : le placeholder existe
    raise RuntimeError("Connection closed before response")


async def fast_await(request, chat_id, message_id, timeout, base, poll_seconds=5):
    # le relanceur réel, mais cadence test (pas de sleep de 5 s)
    return await REAL_AWAIT(request, chat_id, message_id, min(timeout, 10), base, poll_seconds=0.05)


module._fetch_chat_history = fake_history_done
module._submit_chat_turn = submit_lost
module._await_chat_message = fast_await


async def _ok15():
    res = await instance.action(body=BODY_SAVED, __request__=FakeRequest(), __event_emitter__=emitter)
    await asyncio.gather(*list(module._PENDING_TASKS), return_exceptions=True)
    return res


out15 = asyncio.run(_ok15())
assert out15 is None
succ = [t for t in toasts() if t["data"]["type"] == "success"]
errs = [t for t in toasts() if t["data"]["type"] == "error"]
assert succ and not errs, f"tour achevé côté serveur => succès, pas erreur : {toasts()}"
assert "Soumission non confirmée" in log_lines()
assert "achevé côté serveur" in log_lines()
print("OK 15 soumission perdue + tour persisté => succès tolerant (état du chat = source de vérité)")

print("\nTOUS LES TESTS PASSENT")
