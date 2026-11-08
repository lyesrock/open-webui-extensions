# Open WebUI Extensions

Personal collection of extensions for [Open WebUI](https://github.com/open-webui/open-webui),
used and tested in real production deployments. Each extension ships with its own
documentation, valves reference and a **dependency-free test suite** that replays the
real Open WebUI backend contract.

## Available Extensions

### ✅ Web Fact-Check Action — "Vérifier sur le web" ([actions/factcheck_web.py](actions/factcheck_web.py))

Adds a **Verify on the web** button under every assistant message. One click produces a
sourced fact-check report of that answer — verdicts (confirmed / refuted / unverified),
cross-checked web citations, optional corrected rewrite — inserted **as a normal turn of
the conversation**, so the report shares the model's context window and KV cache instead
of a detached side prompt with a cold cache.

Features:

- 🔎 Real fact-checking using the instance's own web search engine (no external keys)
- 🧠 Chat-turn reporting: the check runs in the conversation context (KV-cache friendly,
  measured cache reuse on clicks)
- 🙈 Verification instruction hidden in a native collapsible `<details>` block
- 🛑 Hard search budgets (`max_searches` valve, explicit stop rules, discouraged
  `fetch_url`) — no runaway loops, no context blow-up
- 📊 Progress toasts + persistent logs while the check runs (fire-and-forget action)
- 🧯 Fallback for models without function calling (`legacy` pre-injected search mode)
- 🧪 Dependency-free tests replaying the Open WebUI 0.13 backend contract:
  `python actions/test_factcheck_web.py`

Full documentation: [actions/factcheck_web.md](actions/factcheck_web.md)
(installation, valves, production tips).

## 🚀 Installation

**Recommended (any Open WebUI ≥ 0.5, no restart):**

1. Open your instance → **Workspace → Functions → +**
   (older Open WebUI versions: Admin Panel → Extensions)
2. Pick the extension type (**Action** / **Pipe** / **Filter** / **Tool** — stated in
   each extension's doc) and paste the corresponding `.py` file content
3. Activate it and configure its valves; enable **Global** to expose it to every user
   (admin required)

**Filesystem loading (optional):**

1. Clone this repository and copy the desired file(s) into your functions folder (the
   directory mounted at `data/functions`, or the path set by `FUNCTIONS_FOLDER_PATH`):
   ```bash
   git clone https://github.com/lyesrock/open-webui-extensions.git
   cp open-webui-extensions/actions/factcheck_web.py /path/to/open-webui/data/functions/
   ```
2. Restart Open WebUI.

## 🛠️ Developing

To create new Open WebUI extensions:

1. Use the existing ones here as templates
2. Follow the structure for your type: **Pipes** use `inlet`/`outlet` methods, **Actions**
   use an `async def action(...)` entry point, **Filters** use
   `message_filter`/`response_filter`
3. Configure options with Pydantic valves
4. Document with a complete frontmatter docstring (`title`, `author`, `version`,
   `description`, `required_open_webui_version`) and a companion `.md`

## 📄 License

MIT — see [LICENSE](LICENSE).

## 🤝 Contributing

Contributions are welcome — open an issue or a pull request.
