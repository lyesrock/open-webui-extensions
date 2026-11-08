# ✅ Vérifier sur le web (Web Fact-Check Action)

One-click fact-checking for any assistant answer, using **your instance's own web search**.
The report is produced **as a normal turn of the conversation** — the conversation model
answers inside its own context window, sharing its KV cache — instead of a detached
side-prompt that starts a cold, separate context each time.

## ✨ Features

- 🔎 Verifies factual claims (numbers, dates, names, events, citations) of the message you
  click, with `confirmed / refuted / unverified` verdicts and inline source URLs
- 🧠 **Chat-turn reporting**: submitted through the backend's background-task branch
  (`/api/chat/completions` + `session_id`), so the check reuses the conversation's
  context window — measured high `cached_tokens` on clicks vs. zero with side-channel
  approaches
- 🙈 The injected instruction is wrapped in a native collapsible `<details>` block
  (« Vérification web — consigne automatique ») so the chat stays clean
- 🛑 **Bounded cost**: `max_searches` hard ceiling on `search_web` calls, explicit stop
  rules, `fetch_url` discouraged (full pages blow up the context) — no runaway tool loops
- 📊 Visible progress: periodic status heartbeat + toasts while the action works; the
  action itself returns immediately (fire-and-forget)
- 🧯 Honest by construction: refuses to run when web search is unavailable (valve
  `strict_search`), cites only URLs actually returned by the searches, requires two
  independent sources for any contested claim
- 🔁 `legacy` fallback (pre-injected searches) for models without function calling
- 🧪 Ships with a dependency-free test suite that replays the real Open WebUI 0.13
  backend contract (frontmatter parser, module loader, background-task acknowledgement,
  chat history API, edge cases): `python actions/test_factcheck_web.py`

## 🧭 How it works

1. Open WebUI passes the clicked assistant message in `body["messages"]` / `body["id"]`.
2. The action rebuilds the message chain from the leaf via `GET /api/v1/chats/{id}`
   (dead branches excluded) and injects the verification instruction as a **hidden user
   message** (collapsed `<details>` block) naming the answer to audit.
3. It posts that turn to `/api/chat/completions` in background-task mode
   (`session_id` + `chat_id`) → the backend produces the report inside the real chat.
4. It waits for the turn to genuinely finish by re-reading the chat state, then emits
   result toasts and persistent logs.

## 🚀 Installation

1. Open WebUI → **Workspace → Functions → +** → type **Action**
   (older Open WebUI versions: Admin Panel → Extensions)
2. Paste the content of [`factcheck_web.py`](factcheck_web.py).
3. Activate it (enable **Global** for every user — admin required) and adjust the valves.

Prerequisites:

- Open WebUI **≥ 0.13** (tested on 0.13.x)
- Web search enabled and configured in Admin Settings (the action reuses your engine —
  SearXNG, Brave, DuckDuckGo… — no extra keys)
- A model with function calling recommended (`native` default mode); `legacy` mode works
  with any model

## ⚙️ Valves

| Valve | Default | Description |
|---|---|---|
| `language` | `auto` | Report language (`auto` or a code like `fr`, `en`) |
| `max_checks` | 8 | Max factual claims verified per message |
| `min_searches` | 3 | Minimum web searches before concluding |
| `max_searches` | 6 | **Hard ceiling** of `search_web` calls (anti-loop; native mode) |
| `include_correction` | true | Append a corrected version of the verified text |
| `web_search` | true | Use the instance's web search |
| `search_mode` | `native` | `native` = model tool-calling (shares the KV cache window); `legacy` = pre-injected results |
| `strict_search` | true | Refuse to fact-check when web search is unavailable |
| `debug` | false | Log the resolved configuration (View Logs on the message) |

## 🏭 Production tip

In native mode the server-side tool-call loop is bounded by the
`CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` environment variable, which defaults to **1024**
— far too permissive against pathological loops. Set it to something sane (e.g. **16**):

```bash
# classic docker install
-e CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=16
# existing Swarm service
docker service update <your-openwebui-service> --env-add CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=16
```

Note for Docker Swarm/Dokploy users: variables added with `docker service update --env-add`
are lost on the next panel-driven redeploy — declare them in your deployment definition too.

## 🧪 Tests

```bash
python actions/test_factcheck_web.py
```

No dependencies: it mocks the backend end-to-end (plugin loader, background-task ack,
chat API, emitters) and covers the traps found in the wild — saved vs `temporary:` chats,
dead history branches, closed tabs mid-run, backend failures, truncation, anti-phantom
success reporting.

## 📄 License

MIT — see the [repository LICENSE](../LICENSE). Copyright (c) 2026 Lyes Sbahi.
