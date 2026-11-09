# ✅ Corriger sur le web (In-place Web Correction Action)

One-click **silent correction** of any assistant answer, using **your instance's own web
search**. Unlike a fact-check report, nothing is appended and no new message is created:
the clicked answer is **replaced in place** — each factual error is removed and the
corrected value is written as a marked link, `**[corrected](source-url "Corrigé : wrong")**`,
right inside the original text. Readers never see that a correction pass happened: the
wrong value survives only in the link's tooltip — *struck through* there — while the
replacement shows with a subtle highlight.

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
- 🎨 **Configurable rendering** (valves): highlight color, or classic `~~wrong~~ → corrected`
  strikethrough; the wrong value always stays in the link tooltip (struck through there in
  `replace` style). Purely server-side — no custom CSS required, but a `custom.css` rule
  targeting `a[title^="Corrigé"]` makes the replacement pop (see *Display* below)

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
| `correction_display` | `replace` | `replace` = wrong value removed, replacement highlighted; `strikethrough` = legacy `~~wrong~~ **corrected**` rendering |
| `correction_color` | `jaune` | Highlight color of the replacement (`jaune`/`orange`/`vert`/`rose`/`bleu`/`cyan`/`aucune`) — `replace` style only |
| `show_citations` | `true` | `false` strips the web citation badges (`[1]`, `[2]`…) from the corrected text |
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
- **Numeric replacement (`[2021](url)`)** → Open WebUI's built-in *citation* marked
  extension captures any `[…]` whose text starts with a digit and destroys the link at
  render time. The action writes an invisible leading space (`[ 2021](url)`) so the link
  survives untouched, and it neutralizes any arrow/prefix a model may still emit inside a
  correction marker (`[→ X]` → `[X]`) so the visible text never carries a stray glyph
- **Update write fails** (network, permissions) → corrected text discarded, original
  kept; error toast
- **Temporary chats** → front-end merge contract fallback (undoable)

## 🎨 Display (optional custom CSS)

The replacement is a markdown link whose `title` starts with `Corrigé :` (plus the wrong
value, struck through in the tooltip). Without any CSS it reads as a normal link; to make
it pop, add to **Admin Panel → Interface → Custom CSS** (or serve
`/static/custom.css` on your instance):

```css
.markdown-prose a[title^="Corrigé"], .markdown a[title^="Corrigé"] {
  background: #fef08a; color: #1f2937; text-decoration: none;
  padding: 0 2px; border-radius: 2px;
}
```

## 📄 License

MIT — see [LICENSE](../LICENSE).
