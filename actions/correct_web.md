# ✅ Corriger sur le web (In-place Web Correction Action)

One-click **silent correction** of any assistant answer, using **your instance's own web
search**. Unlike a fact-check report, nothing is appended and no new message is created:
the clicked answer is **replaced in place** — each factual error becomes
`~~wrong~~ **[corrected](source-url)**` right inside the original text. Readers never see
that a correction pass happened (and the strikethrough makes the change transparent).

## ✨ Features

- 🔧 **In-place editing**: the action updates the clicked message through
  `POST /api/v1/chats/{id}/messages/{message_id}`; Open WebUI then broadcasts its
  `chat:message` websocket event and every open client updates live — no refresh, no
  extra turn, no report
- 🧠 **KV-cache friendly**: the correction runs as a *non-persisted* completion that
  replays the exact message chain up to the clicked answer (cut at the target, later
  turns excluded) — the inference server reuses the original prefix cache
- 🖊 **Minimal, sourced diffs**: only the wrong words (1–8) are struck through; the
  replacement is **bold** and is a markdown link to a *content page* that proves it
  (search-results pages like `…?q=…` are forbidden, and the model's memory is never a
  source). If a sentence contains several wrong words (an actor AND a date), all are fixed
- 🚫 **The assistant is out of scope**: statements about the assistant itself (its tools,
  access, capabilities) are never "corrected" — enforced by the prompt AND by a
  deterministic server-side sanitizer that restores the original wording if the model
  disobeys
- 🛑 **Bounded cost**: `max_searches` hard ceiling, explicit stop rules, `fetch_url`
  discouraged — no runaway tool loops
- 🧯 **Trust-verified output**: before writing anything back, the action validates the
  result (visible length vs. original, word-overlap anti-paraphrase, no report
  headings, verbatim opening). Any doubtful output is **rejected and the original text
  is kept**, untouched
- 🙈 Nothing is ever added to the conversation: no injected user message, no assistant
  follow-up, no visible instruction
- 🧪 Ships with a dependency-free test suite: `python actions/test_correct_web.py`

## 🧭 How it works

1. Open WebUI passes the clicked assistant message in `body["messages"]` / `body["id"]`.
2. The action rebuilds the chain **up to the clicked message** via
   `GET /api/v1/chats/{id}` (walk `parentId` from the target, not the leaf).
3. It calls `/api/chat/completions` **without `chat_id`/`session_id`** — a temporary,
   non-persisted completion: the model searches the web with the instance tools and
   returns the corrected full text; nothing touches the conversation.
4. The output passes trust guards (length on *visible* text — markup excluded — word
   overlap, no report markers). Only a passing result is written back in place; the
   backend event refreshes the UI live.
5. Unsaved (temporary) chats have no server-side message table to update: the action
   falls back to the front-end merge contract (returns the corrected content under the
   existing message id; the original stays in `originalContent`, undoable).

## 🚀 Installation

1. Open WebUI → **Workspace → Functions → +** → type **Action**
   (older Open WebUI versions: Admin Panel → Extensions)
2. Paste the content of [`correct_web.py`](correct_web.py).
3. Activate it (enable **Global** for every user — admin required) and adjust the valves.

Prerequisites:

- Open WebUI **≥ 0.13** (tested on 0.13.x)
- Web search enabled and configured in Admin Settings (reuses your engine — SearXNG,
  Brave, DuckDuckGo… — no extra keys)
- A model with function calling recommended (`native` default); `legacy` mode works
  with any model

## ⚙️ Valves

| Valve | Default | Description |
|---|---|---|
| `language` | `auto` | `'auto'` (same as the answer) or a code (`fr`, `en`) |
| `max_searches` | `6` | Hard ceiling on `search_web` calls per correction |
| `web_search` | `true` | Use the instance's web search |
| `search_mode` | `native` | `native` = model tools; `legacy` = pre-injected searches |
| `strict_search` | `true` | Refuse to correct when search is unavailable |
| `debug` | `false` | Log resolved backend config (version, engine, model) |
| `completion_model` | *(empty)* | Model used for the pass (empty = conversation model) |
| `base_url` | *(empty)* | Internal backend URL for self-calls (empty = auto) |
| `timeout_seconds` | `300` | Maximum wall time for the correction |
| `max_chars` | `16000` | Maximum length of the answer submitted |
| `priority` | `1` | Button order (lower = further left) |

## 🧨 Edge cases handled

- **No factual error found** → text is left unchanged, info toast only
- **Model summarizes instead of reproducing** → rejected (visible-length floor)
- **Model appends commentary/report** → rejected (visible-length ceiling, `## ` check)
- **Model paraphrases the answer** → rejected (word-overlap floor)
- **Model strikes whole sentences** → instruction forbids it with an explicit
  good/bad example; guards measure *visible* text so legitimate markup length never
  false-triggers
- **Model "corrects" the assistant's own tools/capabilities** → the sanitizer strips
  that segment and restores the original wording before anything is written
- **Update write fails** (network, permissions) → corrected text discarded, original
  kept; error toast
- **Temporary chats** → front-end merge contract fallback (undoable)

## 📄 License

MIT — see [LICENSE](../LICENSE).
