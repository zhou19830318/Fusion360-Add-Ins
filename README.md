# Fusion360-Add-Ins — AI Copilot for Autodesk Fusion 360

**[English](README.md) · [中文](README.zh-CN.md)**

Fusion360-Add-Ins (AI Fusion) is a **native Fusion 360 add-in** that embeds a docked AI chat palette directly inside Fusion. Describe what you want to build in natural language — the AI drives the Fusion API for you: sketching, modelling, editing features, and verifying the result through viewport screenshots.

## Demo Video

Click the link below to view the Fusion360-Add-Ins demo:
[c25926637e665985cc7f7c9c3a4c6ff0.mp4](https://github.com/zhou19830318/Fusion360-Add-Ins/blob/main/c25926637e665985cc7f7c9c3a4c6ff0.mp4)

## Features

- **Docked chat palette** — the AI copilot lives inside Fusion, no browser tab needed
- **6 AI providers** — DeepSeek, Kimi (Moonshot), Zhipu GLM, OpenAI, Anthropic Claude, Google Gemini, all using the latest flagship models (2026-08)
- **Automatic provider detection** — if exactly one API key is configured, it is selected automatically
- **Visual verification loop** — after building, the add-in captures the Fusion viewport (`saveAsImageFile`) and feeds it back to vision-capable models for a design sanity check (up to 3 repair rounds)
- **Attachments** — attach images or PDFs (≤8 MB each, max 4) as reference for the model; non-vision models degrade gracefully with a notice
- **Capability-aware adaption** — image input is gated per model (e.g. DeepSeek's official API and GLM-5.3 are text-only, so images are stripped server-side instead of failing)
- **Verified API knowledge base** — exact signatures for Fusion API operations embedded in the system prompt, eliminating guesswork
- **Model aliases** — type `sonnet5`, `fable`, `gpt5.6` or `gemini-3.7` and the server maps them to real model IDs
- **Auto-dependency install** — missing Python packages (`flask`, `requests`) are installed automatically on first launch; the real Python's `site-packages` is injected into Fusion's interpreter path automatically
- **Progress feedback** — the UI shows elapsed time, round progress and busy-state notices so long tasks never look frozen
- **All operations in-process** — Fusion API calls stay on your machine; only the chat protocol and optional reference images leave your machine

## Supported AI Providers (latest flagship models, 2026-08)

| Provider | API Format | Models | Image input |
|----------|-----------|--------|:---:|
| **DeepSeek** | OpenAI-compatible | deepseek-v4-pro, deepseek-v4-flash | ❌ (official API is text-only) |
| **Kimi (Moonshot)** | OpenAI-compatible | kimi-k3 | ✅ |
| **Zhipu GLM** | OpenAI-compatible | glm-5.3 | ❌ |
| **OpenAI** | OpenAI-compatible | gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna | ✅ |
| **Anthropic Claude** | Native Messages API | claude-sonnet-5, claude-fable-5 | ✅ |
| **Google Gemini** | OpenAI-compatible | gemini-3.7-flash | ✅ |

> **Note:** DeepSeek's and GLM's *official* APIs accept text only. The image gating is handled automatically — send an image while on a text-only model and the server converts it to a notice instead of throwing a 400 error.

## System Requirements

- **Windows** 10/11 (macOS supported by manifest but not yet tested)
- **Autodesk Fusion 360** (any recent version with Add-In support)
- **Python** — Fusion 360 bundles its own Python, no separate install needed
- **Internet connection** — for AI API calls

## Quick Install

### 1. Download

```powershell
# Clone from GitHub
git clone https://github.com/zhou19830318/Fusion360-Add-Ins.git "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins"
```

Or download the ZIP and extract to:
```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins\
```

### 2. Install Dependencies

**Run as Administrator:**

```batch
cd "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins"
install_deps.bat
```

This script:
1. Finds a real Python with pip
2. Bootstraps pip if missing (Fusion's bundled Python has no pip by default)
3. Installs `flask` and `requests`

> **Note:** The add-in auto-installs dependencies on first launch too. Running `install_deps.bat` beforehand just avoids the first-launch delay.

### 3. Configure an API Key

Open `local_server\config.json` and add at least one API key:

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "deepseek_api_key": "sk-your-deepseek-key-here",
  "openai_api_key": "",
  "anthropic_api_key": "",
  "google_api_key": ""
}
```

**Where to get keys:**
- DeepSeek: [platform.deepseek.com](https://platform.deepseek.com)
- Zhipu GLM: [open.bigmodel.cn](https://open.bigmodel.cn)
- OpenAI: [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Anthropic: [console.anthropic.com](https://console.anthropic.com)
- Google Gemini: [aistudio.google.com](https://aistudio.google.com) (free tier available)
- Kimi: [platform.moonshot.cn](https://platform.moonshot.cn)

> **Security:** `local_server/config.json` contains API keys and is **excluded from version control** (see `.gitignore`). For a key-less template use `local_server/config.example.json`. Never push `config.json` to a public repository.

### 4. Enable in Fusion 360

1. Fully quit Fusion 360 and restart
2. Press `Shift+S` → **Add-Ins** tab → find **Fusion360-Add-Ins**
3. Click **Run**, then tick **Run on Startup**
4. The palette appears docked on the right side

## Project Structure

```
Fusion360-Add-Ins/
├── AIFusion.py                  # Add-in entry point (loaded by Fusion)
├── AIFusion.manifest            # Fusion add-in manifest
├── install_deps.bat             # Dependency installer (run once)
├── .gitignore                   # Excludes config.json (API keys), logs, caches
├── resources/
│   └── palette.html             # Loading/fallback page
├── bridge/
│   ├── palette.pyc              # Message bridge (HTML ↔ Python)
│   └── selection_watcher.pyc    # Selection change monitor
├── handlers/
│   ├── __init__.pyc             # Tool router (dispatch)
│   ├── read.pyc                 # Query model state (incl. viewport screenshot)
│   ├── create.pyc               # Create simple features
│   ├── execute.pyc              # Execute Python scripts
│   ├── update.pyc               # Modify features
│   ├── delete.pyc               # Delete features
│   └── api_documentation.pyc    # API doc search
└── local_server/
    ├── server.py                # Flask backend (multi-provider routing, vision gating)
    ├── chat_ui.html             # Chat interface (loaded in Fusion palette)
    ├── config.json              # Local only — AI provider & API keys (git-ignored)
    ├── config.example.json      # Key-less configuration template
    ├── requirements.txt         # Python dependencies
    └── __init__.py              # Package init
```

## How It Works

1. Fusion loads `AIFusion.py` → ensures dependencies → starts a local Flask server on port 8765
2. A docked palette (Qt WebView) loads `chat_ui.html`
3. The user types a request → `POST /api/chat` → the server routes to the configured provider/model
4. The AI returns tool calls (e.g. `execute` with a Python script, or `read` with `queryType=screenshot`)
5. The JS bridge sends tool calls to Fusion via `adsk.fusionSendData`
6. `handlers/*.pyc` execute the Fusion API calls in-process
7. Results flow back to the AI for the next turn (up to 10 rounds)

## Visual Verification (multi-modal sanity check)

For vision-capable models, a design loop can end with a screenshot check:

```
execute(build) → read(screenshot) → Fusion captures viewport PNG (base64)
  → image is injected as a multimodal user message (image_url / image block)
  → model judges: OK → final summary; not OK → fixes with execute … (max 3 rounds)
```

- Screenshot support ships in the `read` tool (`queryType: "screenshot"`, returns `{type:"image", mimeType:"image/png", data:…}`)
- Enabled via the palette settings toggle *"设计完成后自动截图视觉校验"* (persisted in localStorage)
- Text-only models skip image injection automatically (DeepSeek official API, GLM-5.3)

## Attachments (reference images / PDFs)

Click **📎 附件** next to the input box to attach reference material:

- **Types:** PNG, JPG, WebP, GIF images; PDF
- **Limits:** ≤ 8 MB per file, max 4 files
- Attachments are base64-encoded and injected as multimodal user messages
- On text-only models the attachment is replaced by a textual notice (with a hint to switch to a vision model)
- Claude receives PDFs via Anthropic `document` blocks; other vision providers receive them as data URLs

## Switching Providers

1. Click the ⚙ badge in the palette header → Settings panel opens
2. Pick a provider (DeepSeek, Kimi, Zhipu GLM, OpenAI, Anthropic Claude, Google Gemini)
3. Pick a model from the dropdown
4. Paste the provider's API key and click **Save**

If exactly one provider has a key, it is auto-selected on startup. The model dropdown and the vision badge in the settings are populated live from `/api/models`.

## HTTP API

| Endpoint | Purpose |
|----------|---------|
| `GET /chat` | Serves the chat UI (palette) |
| `POST /api/chat` | Chat completion with tool calling; multi-provider routing; vision gating & image stripping |
| `GET /api/models` | Providers, model lists, vision capability (`vision_all` / `vision_models`), configured status |
| `POST/GET /api/config` | Read/update the local configuration |

## FAQ

### The plugin shows "No API key set"

Open Settings (⚙), select your provider, paste the API key, and click Save. The key is stored in the local `local_server\config.json`.

### I see `ModuleNotFoundError: No module named 'flask'`

The add-in auto-installs dependencies and injects the real Python's `site-packages` into Fusion's interpreter path. Run `install_deps.bat` as Administrator if that fails, then restart the add-in.

### The server shows "Cannot connect to …"

Check your internet connection, verify the API key (no extra spaces), confirm the provider subscription is active, or try another provider.

### I see `DeepSeek returned 400: unknown variant 'image_url'`

DeepSeek's official API is text-only. The current version detects this and automatically strips images (the error only occurs on versions before the gating fix). Switch to a vision model (Kimi K3 / GPT-5.6 / Claude / Gemini) to use image attachments.

### The chat seems stuck at "Thinking…"

The status bar now shows elapsed time and round progress — long multi-tool tasks can take several minutes (especially reasoning models). Restart the add-in and watch the counter; if it is frozen without counting, report the `aifusion_debug.log` excerpt.

### The model draws incorrectly or crashes

The system prompt contains verified Fusion API signatures. If you encounter an error:
1. Check `aifusion_debug.log` for the exact API error
2. The repair loop in the system prompt covers common failure patterns (stale body references, wrong property names, healthState != 0, …)
3. File an issue on GitHub with the log excerpt

## Privacy & Security

- **API keys:** Stored in `local_server\config.json` which is **git-ignored and never pushed**; use `config.example.json` as the template for the repository
- **Fusion files:** never uploaded; all Fusion API work happens in-process
- **Chat data & attachments:** only conversation text, tool results, and your attached reference images are sent to the chosen AI provider
- **Network:** only `localhost:8765` → your chosen AI provider's API endpoint

## Uninstall

1. Quit Fusion 360
2. Delete the folder:
   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\Fusion360-Add-Ins
   ```
3. Restart Fusion 360

## Contributing

Contributions welcome. The `.pyc` files in `bridge/` and `handlers/` implement the Fusion API bridge; the `local_server/` directory (server.py, chat_ui.html) and `AIFusion.py` are the open, main-editable parts.

## License

This project is licensed under the MIT License. See `LICENSE` for details.