# AIFusion — AI Copilot for Autodesk Fusion 360

**[English](README.md) · [简体中文](README.zh-CN.md)**

AIFusion is a **native Fusion 360 add-in** that embeds a docked AI chat palette directly inside Fusion. Describe what you want to build in natural language — the AI drives the Fusion API for you: sketching, modelling, editing features, and verifying the result through viewport screenshots.

[Visit my blog](https://blog.csdn.net/acldy833/article/details/163825853?sharetype=blogdetail&sharerId=163825853&sharerefer=PC&sharesource=acldy833&spm=1011.2480.3001.8118)

---

## Features

- **Docked chat palette** — the AI copilot lives inside Fusion, no browser tab needed
- **6 AI providers** — DeepSeek, Kimi (Moonshot), Zhipu GLM, OpenAI, Anthropic Claude, Google Gemini, using latest flagship models (2026-09)
- **Automatic provider detection** — if exactly one API key is configured, it is selected automatically
- **Agentic tool loop** — the model plans, queries model state (`read`), and runs Python scripts against the Fusion API (`execute`), up to 10 rounds per request
- **Visual verification loop** — after building, the palette captures the Fusion viewport and feeds it back to vision-capable models for a design sanity check (up to 3 repair rounds); only the latest screenshot is kept, and images are auto-downscaled client-side
- **Attachments** — attach images or PDFs (≤ 8 MB each, max 4) as design reference; oversized images are downscaled in the browser before upload
- **Capability-aware adaptation** — image input is gated per model (e.g. GLM-5.3 and deepseek-v4-pro are text-only, so images are replaced by a notice server-side instead of failing)
- **Verified API knowledge in the prompt** — hard-won Fusion API facts (cm→mm `MM=0.1` constant, `SketchTexts.createInput(text, position)` signature, `root.bRepBodies` access, …) are always merged into the system prompt, even if the client sends its own system message
- **Model aliases** — type `sonnet5`, `fable`, `gpt5.6` or `gemini-3.7` and the server maps them to real model IDs
- **Sampling control** — default `temperature: 0.2` for reproducible CAD script generation; override per request or in `config.json` (set `"none"` to follow the provider default, e.g. for reasoning models)
- **Honest truncation reporting** — Anthropic `max_tokens` stop reason is surfaced as OpenAI-style `finish_reason: "length"`, so a half-written script can be detected instead of silently executed
- **Truncation recovery** — reasoning models share one output budget between thinking and the answer (DeepSeek's thinking mode defaults to 64K). When reasoning eats all of it the provider returns `HTTP 200` with an empty message; the palette shows the reasoning, retries with a lower `reasoning_effort` (`low`, then `none`) and only then reports an actionable error
- **Provider call log** — every attempt and response (`finish_reason`, token usage, `TRUNCATED` / `EMPTY_MESSAGE` flags, elapsed time) is appended to `aifusion_debug.log`, so a failed round can be diagnosed after the fact; per-call timeout is configurable via `provider_read_timeout` (default 420 s) and a timeout is no longer retried three times
- **Project history & session restore** — conversations, tool calls and results are persisted to a local SQLite database (WAL + full synchronous); reopen the palette and restore any session, switching models keeps the context
- **Hardened local server** — payload caps (messages, chars, body size, image size, script size), provider retries with backoff, CORS restricted to loopback origins, per-request `base_url` whitelist, atomic config writes, API keys always masked in API responses
- **Polished UI** — collapsible tool-call cards with status/duration/summary/copy, markdown rendering (headings, lists, code), smart auto-scroll with jump-to-bottom, live round & elapsed-time indicator, one-click example prompts, session/project history panel, one-click clear
- **Auto-dependency install** — missing Python packages (`flask`, `requests`) are installed automatically on first launch; the real Python's `site-packages` is injected into Fusion's interpreter path automatically

## Supported AI Providers (latest flagship models, 2026-09)

| Provider             | API Format          | Models                                    | Image input                    |
| -------------------- | ------------------- | ----------------------------------------- | :----------------------------: |
| **DeepSeek**         | OpenAI-compatible   | deepseek-flash, deepseek-v4-pro           | ✅ (deepseek-flash, native multimodal) |
| **Kimi (Moonshot)**  | OpenAI-compatible   | kimi-k3                                   | ✅                             |
| **Zhipu GLM**        | OpenAI-compatible   | glm-5.3-flash, glm-5.3                    | ✅ (glm-5.3-flash, native multimodal) |
| **OpenAI**           | OpenAI-compatible   | gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna  | ✅                             |
| **Anthropic Claude** | Native Messages API | claude-sonnet-5, claude-fable-5           | ✅                             |
| **Google Gemini**    | OpenAI-compatible   | gemini-3.7-flash                          | ✅                             |

> **Note:** `glm-5.3` (text flagship) and `deepseek-v4-pro` remain text-only (`glm-5.3-flash` is natively multimodal since 2026-08; `deepseek-flash` since 2026-09). Image gating is handled automatically — send an image to a text-only model and the server converts it to a notice instead of throwing a 400 error.

**Model aliases** (resolved server-side): `sonnet5` / `sonnet-5` → `claude-sonnet-5`, `fable` → `claude-fable-5`, `gpt5.6` / `gpt-5.6` → `gpt-5.6-sol`, `gemini3.7` / `gemini-3.7` → `gemini-3.7-flash`.

## System Requirements

- **Windows** 10/11 (macOS is listed in the manifest but not yet tested)
- **Autodesk Fusion 360** (any recent version with Add-In support)
- **Python** — Fusion 360 bundles its own Python, no separate install needed
- **Internet connection** — for AI API calls

## Quick Install

### Option A — One-click installer (recommended)

1. Download the ZIP from GitHub and extract it anywhere (e.g. your Desktop)
2. Double-click **`install.bat`**
3. When asked, **paste your DeepSeek API Key** (or press Enter to skip and add it later in the Fusion palette settings)
4. Fully restart Fusion 360 → `Shift+S` → **Add-Ins** → **AIFusion** → **Run** → tick **Run on Startup**

The script automatically:

- Locates Fusion's `AddIns` folder and copies the add-in there (idempotent — if an installation already exists, it skips the copy)
- Installs `flask`/`requests` via pip when a system Python is available (the add-in also auto-installs them on first launch)
- Writes `local_server/config.json` for you via the guided `tools/setup_config.ps1` — no manual JSON editing

### Option B — Manual install

#### 1. Download

```powershell
# Clone from GitHub
git clone https://github.com/zhou19830318/AIFusion.git "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion"
```

Or download the ZIP and extract to:

```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion\
```

#### 2. Install Dependencies

```batch
cd "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion"
install_deps.bat
```

This script (run as Administrator if pip has permission problems):

1. Finds a real Python (prefers Fusion 360's bundled Python under `%LOCALAPPDATA%\Autodesk\webdeploy\production`)
2. Bootstraps pip if missing (ensurepip → get-pip.py fallback)
3. Installs `flask` and `requests`

> **Note:** The add-in auto-installs dependencies on first launch too. Running `install_deps.bat` beforehand just avoids the first-launch delay.

#### 3. Configure an API Key

Either use the palette Settings (⚙) inside Fusion, or edit `local_server\config.json` directly:

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "deepseek_api_key": "sk-your-deepseek-key-here"
}
```

Full key set: `deepseek_api_key`, `kimi_api_key`, `zhipu_api_key`, `openai_api_key`, `anthropic_api_key`, `google_api_key` (each provider also has a `<provider>_base_url` override). See `local_server/config.example.json` for the complete template. Provider keys are also read from environment variables (e.g. `DEEPSEEK_API_KEY`) as a fallback.

**Where to get keys:**

- DeepSeek: [platform.deepseek.com](https://platform.deepseek.com)
- Zhipu GLM: [open.bigmodel.cn](https://open.bigmodel.cn)
- OpenAI: [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Anthropic: [console.anthropic.com](https://console.anthropic.com)
- Google Gemini: [aistudio.google.com](https://aistudio.google.com) (free tier available)
- Kimi: [platform.moonshot.cn](https://platform.moonshot.cn)

> **Security:** `local_server/config.json` contains API keys and is **excluded from version control** (see `.gitignore`). Never push `config.json` to a public repository.

#### 4. Enable in Fusion 360

1. Fully quit Fusion 360 and restart
2. Press `Shift+S` → **Add-Ins** tab → find **AIFusion**
3. Click **Run**, then tick **Run on Startup**
4. The palette appears docked on the right; a toolbar button on the **Solid** tab reopens it any time

## Using the Copilot

- **Chat in natural language.** The AI analyzes the live design first (`read`), plans, then builds via Python scripts (`execute`), and verifies results with bounding boxes / volumes / screenshots.
- **Examples** (clickable in the empty state): *"建模：一个 80×60×30 mm 的外壳，正面留 USB 和 BOOT 开孔"*, *"修改：把所有圆角半径改成 3mm，并加 2mm 壁厚"*, *"查询：列出当前设计所有实体的尺寸和体积"*.
- **Attachments:** click the paperclip button to attach reference images or PDFs (≤ 8 MB each, max 4).
- **Vision verification:** enable *"设计完成后自动截图视觉校验"* in Settings to have vision models screenshot-check their own builds (max 3 repair rounds).
- **History:** the ⏱ button opens the history panel — switch projects, create sessions, or restore a previous conversation. History is stored locally at `.aifusion/history.sqlite3` (override the directory with the `AIFUSION_DATA_DIR` environment variable).
- **Clear:** the ✕ button **permanently deletes** the current session's stored history (events + snapshot) after a confirmation prompt, so a reopened palette comes back empty. Other sessions are untouched — use *New session* instead if you want to keep the old conversation available for restore.

## Project Structure

```
AIFusion/
├── AIFusion.py                  # Add-in entry point (palette, toolbar, server lifecycle)
├── AIFusion.manifest            # Fusion add-in manifest (v1.0.0, runOnStartup)
├── install.bat                  # One-click installer (double-click to deploy)
├── install_deps.bat             # Dependency installer (flask + requests)
├── tools/
│   └── setup_config.ps1         # Guided API-key configuration (used by install.bat)
├── resources/
│   └── palette.html             # Loading/fallback page shown while the server starts
├── bridge/                      # Message bridge (HTML ↔ Python), shipped as .pyc
│   ├── palette.pyc
│   └── selection_watcher.pyc
├── handlers/                    # Fusion API tool handlers, shipped as .pyc
│   ├── __init__.pyc             # Tool router (dispatch)
│   ├── read.pyc                 # Query model state (incl. viewport screenshot)
│   ├── create.pyc               # Create simple features
│   ├── execute.pyc              # Execute Python scripts
│   ├── update.pyc               # Modify features
│   ├── delete.pyc               # Delete features
│   └── api_documentation.pyc    # API doc search
├── local_server/
│   ├── server.py                # Flask backend (multi-provider routing, Anthropic adapter,
│   │                            #   payload guards, retries, CORS hardening, config mgmt)
│   ├── history_store.py         # SQLite history (events + snapshots, WAL, privacy redaction)
│   ├── chat_ui.html             # Chat interface (loaded in the Fusion palette)
│   ├── config.json              # Local only — provider & API keys (git-ignored)
│   ├── config.example.json      # Key-less configuration template
│   ├── requirements.txt         # Python dependencies (flask, requests)
│   └── __init__.py              # Package init
├── tests/
│   └── test_server.py           # Server unit tests (run from the repo root)
├── specs/                       # Design documents & known constraints
└── README.md / README.zh-CN.md  # This documentation (EN / ZH)
```

> **Note:** `bridge/` and `handlers/` ship only as compiled `.pyc` files (CPython 3.14) with no source. They are the black-box boundary that actually drives the Fusion API; the add-in requires a compatible interpreter to import them.

## How It Works

```
Fusion 360 add-in (AIFusion.py)
  └─ spawns local Flask server (127.0.0.1:8765, daemon thread)
       └─ serves chat_ui.html palette + REST API
            └─ proxies chat to 6 LLM providers
Fusion bridge (bridge/*.pyc)
  └─ routes tool calls between the palette and Fusion via adsk.fusionSendData
```

1. Fusion loads `AIFusion.py` → ensures dependencies → health-checks and starts the local Flask server on port **8765** (if a stale instance holds the port, it scans **8765–8780** and the palette follows the chosen port; override with the `AIFUSION_PORT` environment variable)
2. A docked palette (Qt WebView) loads `chat_ui.html`
3. The user types a request → `POST /api/chat` → the server injects the CAD system prompt and routes to the configured provider/model
4. The AI returns tool calls (e.g. `execute` with a Python script, or `read` with `queryType=screenshot`)
5. The JS bridge sends tool calls to Fusion via `adsk.fusionSendData`
6. `handlers/*.pyc` execute the Fusion API calls in-process
7. Results flow back to the AI for the next turn (up to 10 rounds per user request)

The two Fusion tools exposed to every provider:

- **`read`** — query the design: `bodies`, `faces`, `edges`, `features`, `sketches`, `sketchProfiles`, `parameters` / `userParameters` / `modelParameters`, `materialLibraries` / `materialAppearance`, `selection` / `selectionSets`, `timeline` / `timelineStatus`, `document`, `projects`, measurements (`volume`, `area`, `length`, `centroid`), `similar`, `edgesByType`, `apiDocumentation`, `screenshot`
- **`execute`** — run a Python script against the Fusion API (the script must define `run(_context)`)

## Server Hardening & Limits

| Guard                        | Value / behaviour                                                              |
| ---------------------------- | ------------------------------------------------------------------------------ |
| Request body                 | ≤ 32 MB                                                                        |
| Messages per request         | ≤ 80                                                                           |
| Total text content           | ≤ 120,000 chars (base64 image data counted at a nominal 512 chars per image)   |
| Single image (data URL)      | ≤ 6,000,000 chars (≈ 4.5 MB binary; below provider per-image limits)           |
| `execute` script argument    | ≤ 120,000 chars (rejects runaway scripts that would crowd out the system prompt) |
| Tools array                  | ≤ 32 entries                                                                   |
| `tool` messages              | must carry a non-empty `tool_call_id` (validated up-front with a clear 400)    |
| Provider retries             | 408/429/5xx retried up to 2 times with backoff; timeouts 10 s connect / 180 s read |
| `max_tokens`                 | 8192; Anthropic `max_tokens` stop reason is reported as `finish_reason: "length"` |
| Temperature                  | default 0.2; resolution payload > `config.json`; `"none"`/empty omits the parameter (for reasoning models) |
| CORS                         | loopback origins only (127.0.0.1 / localhost, any port)                        |
| Per-request `base_url`       | restricted to the provider's default/configured host or a loopback address (blocks key-exfiltration via attacker-controlled `base_url`) |
| Config writes                | atomic (`mkstemp` + `os.replace`); keys masked as `***last4` in GET and the masked value is never written back |
| Errors                       | normalized JSON with `code`, `retryable`, `request_id` — never a raw key       |

## Visual Verification (multi-modal sanity check)

For vision-capable models, a design loop can end with a screenshot check:

```
execute(build) → read(screenshot) → Fusion captures viewport PNG (base64)
  → client downscales to ≤1024px JPEG and injects it as a multimodal user message
  → model judges: OK → final summary; not OK → fixes with execute … (max 3 rounds)
```

- Screenshot support ships in the `read` tool (`queryType: "screenshot"`)
- Enabled via the palette settings toggle *"设计完成后自动截图视觉校验"* (persisted in localStorage)
- Vision-capable models (deepseek-flash, Kimi K3, GPT-5.6, Claude, Gemini) screenshot-check their own builds; text-only models skip image injection automatically
- Only the newest verification screenshot is kept in the conversation — older ones are replaced, not accumulated

## Attachments (reference images / PDFs)

Click the **paperclip** button next to the input box to attach reference material:

- **Types:** PNG, JPG, WebP, GIF images; PDF
- **Limits:** ≤ 8 MB per file, max 4 files
- Images larger than 512 KB are downscaled in the browser (≤ 2048 px, JPEG) before upload
- Attachments are base64-encoded and injected as multimodal user messages
- On text-only models the attachment is replaced by a textual notice (with a hint to switch to a vision model)
- Claude receives PDFs via Anthropic `document` blocks; other vision providers receive them as data URLs

## HTTP API

| Endpoint                             | Purpose                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------ |
| `GET /chat`                          | Serves the chat UI (palette)                                                    |
| `POST /api/chat`                     | Chat completion with tool calling; multi-provider routing; vision gating & image stripping |
| `GET /api/models`                    | Providers, model lists, aliases, vision capability (`vision_all` / `vision_models`), configured status |
| `GET /api/config`                    | Read config (API keys masked as `***last4`)                                     |
| `POST /api/config`                   | Update config (masked values are ignored, real keys preserved)                  |
| `GET /api/health`                    | Liveness probe used by the add-in startup                                       |
| `GET /api/projects`                  | List local projects                                                             |
| `POST /api/projects`                 | Create a project                                                                |
| `GET/POST /api/projects/<id>/sessions` | List / create sessions of a project                                           |
| `GET /api/sessions/<id>`             | Read a session snapshot and its event history                                   |
| `POST /api/sessions/<id>/events`     | Append a session event and update the snapshot                                  |
| `DELETE /api/sessions/<id>/history`  | Permanently delete one session's events + snapshot (backs the palette's clear button) |
| `POST /api/sessions/<id>/archive`    | Archive a session                                                               |

## Project History & Sessions

- Conversations, AI replies, tool calls and (summarized) tool results are stored in a local **SQLite** database with **WAL** journaling and **FULL** synchronous mode: every change appends an immutable event and updates a fast-restorable session snapshot
- Default location: `.aifusion/history.sqlite3` inside the add-in folder (so it backs up and migrates with the project); override with `AIFUSION_DATA_DIR`
- On startup the palette auto-opens the most recent project/session; the history panel lets you switch projects, create sessions, and restore any session — switching models never clears the context
- **Privacy:** the history store never persists API keys, `Authorization` headers, image/PDF data URLs, or screenshot base64 — attachments are kept only as restorable notices
- **Clearing:** the ✕ button calls `DELETE /api/sessions/<id>/history`, which removes that session's events and snapshot while keeping the session itself. Clearing only the panel was never enough: the snapshot is exactly what the palette restores on startup, so a client-only clear resurrected the conversation on the next launch

## Development & Testing

```powershell
# from the repo root
python -m unittest discover -s tests
```

The suite (74 tests) covers payload validation, provider routing, the Anthropic adapter (message merging, finish-reason mapping), temperature and `reasoning_effort` resolution, config redaction/save, the history API (projects/sessions/events/archive/rehydration, data-URL redaction, clear-history), vision-capability stripping, long-session trimming, provider-call logging and truncated-response handling (server contract plus palette static contracts). It must run from the **repo root** (not from `local_server/`). `package.json` also exposes `npm run build:local` for a quick `py_compile` syntax check.

## FAQ

### The plugin shows "No API key set"

Open Settings (⚙), select your provider, paste the API key, and click Save. The key is stored in the local `local_server\config.json`.

### I see `ModuleNotFoundError: No module named 'flask'`

The add-in auto-installs dependencies and injects the real Python's `site-packages` into Fusion's interpreter path. Run `install_deps.bat` as Administrator if that fails, then restart the add-in.

### The chat reports "Cannot connect to …"

Check your internet connection, verify the API key (no extra spaces), confirm the provider subscription is active, or try another provider.

### I see `DeepSeek returned 400: unknown variant 'image_url'`

Only possible on the old text-only `deepseek-v4-flash` (deprecated; the name now routes to the multimodal `deepseek-flash`) or `deepseek-v4-pro`. The current version detects text-only models and automatically strips images. For image attachments and the screenshot verification loop, stay on `deepseek-flash`.

### The chat seems stuck at "Thinking…"

The status bar shows elapsed time and round progress — long multi-tool tasks can take several minutes (especially reasoning models). If the counter is frozen without counting, restart the add-in and report an `aifusion_debug.log` excerpt.

### I see `Error: Empty response` / `模型输出被截断`

Reasoning models share **one** output budget between thinking and the answer (DeepSeek's thinking mode defaults to 64K tokens). If the reasoning consumes all of it, the provider still answers `HTTP 200` — but with `content: ""`, no tool calls and `finish_reason: "length"`. The round produced nothing, and the model never got to write or call a tool.

The palette now recognises this: it shows the reasoning that *was* produced, retries twice with a lower `reasoning_effort` (`low`, then `none`), and only then reports an actionable error instead of a bare "Empty response".

Every provider call is also logged to `aifusion_debug.log`:

```
[2026-09-13 16:23:41] [provider] deepseek-flash openai attempt 1 -> status=200 finish_reason=length content_chars=0 tool_calls=0 reasoning_chars=60866 prompt_tokens=42807 completion_tokens=16184 reasoning_tokens=15886 TRUNCATED EMPTY_MESSAGE elapsed=142.7s
```

Useful knobs in `local_server\config.json`:

- `provider_read_timeout` (default `420`, range 30–3600) — how long a single provider call may take. Timeouts are no longer retried three times, which used to turn one slow round into ~9 minutes of dead time.
- Switching to a non-thinking model, or clearing the session and re-opening a more focused task, avoids the shared-budget problem entirely.

### The palette says the local server lacks the latest API

**Fully quit and relaunch Fusion 360.** Restarting only the add-in is *not* enough: the Flask server runs inside Fusion's own Python process, so `local_server.*` stays in `sys.modules` for the whole Fusion session and a re-import would hand back the module that was loaded first. The launcher now evicts those modules before importing (`_reload_local_modules()` in `AIFusion.py`), so an add-in restart does pick up edited code — but a Fusion session started *before* your change still holds the old build. If the log shows `Server NOT ready after …`, the launcher automatically tries ports 8765–8780 to work around a stale instance holding the default port.

### The model draws incorrectly or crashes

The system prompt merges verified Fusion API guidance. When you hit an error:

1. Check `aifusion_debug.log` for the exact API error (log rotates at ~500 KB). Provider calls are logged too: `[provider] chat rid=… provider=… model=… finish_reason=… prompt_tokens=… completion_tokens=… reasoning_tokens=…`, plus `TRUNCATED` / `EMPTY_MESSAGE` flags for empty answers
2. The repair loop in the prompt covers common failure patterns (stale body references, wrong property names, `healthState != 0`, …)
3. File an issue on GitHub with the log excerpt

## Privacy & Security

- **API keys:** stored in `local_server\config.json`, which is **git-ignored and never pushed**; use `config.example.json` as the repository template; keys are masked in every API response
- **Fusion files:** never uploaded; all Fusion API work happens in-process
- **Chat data & attachments:** only conversation text, tool results, and your attached reference images are sent to the chosen AI provider
- **Network:** only `127.0.0.1:<port>` → your chosen AI provider's API endpoint; the local API rejects non-loopback browser origins and attacker-controlled `base_url` values
- **History:** stored locally only; sensitive material (keys, raw image data) is never written to the database

## Uninstall

1. Quit Fusion 360
2. Delete the folder:
   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion
   ```
3. Restart Fusion 360

## Contributing

Contributions welcome. The `.pyc` files in `bridge/` and `handlers/` implement the Fusion API bridge and currently have no readable source; `local_server/` (server.py, chat_ui.html, history_store.py), `AIFusion.py`, and the test suite are the open, main-editable parts. Design documents and known constraints live under `specs/`.

## License

This project is licensed under the MIT License. See `LICENSE` for details.
