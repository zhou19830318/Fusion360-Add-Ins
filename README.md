# AI Fusion — AI Copilot for Autodesk Fusion 360

AI Fusion is a **native Fusion 360 add-in** that embeds a docked AI chat palette directly inside Fusion. Describe what you want to build in natural language — the AI drives the Fusion API to create sketches, extrusions, fillets, shells, holes, patterns, assemblies, and more.

## Demo Video
Click the link below to view the AI Fusion demo:
[c25926637e665985cc7f7c9c3a4c6ff0.mp4](https://github.com/zhou19830318/AIFusion/blob/main/c25926637e665985cc7f7c9c3a4c6ff0.mp4)

## Features

- **Docked chat palette** — AI copilot lives inside Fusion, no browser tab needed
- **6 AI providers** — DeepSeek, Kimi, Zhipu GLM, OpenAI, Anthropic Claude, Google Gemini
- **Automatic provider detection** — if exactly one API key is configured, it's selected automatically
- **Verified API knowledge base** — 15 Fusion API operations with exact signatures embedded in the system prompt, eliminating guesswork
- **Model aliases** — type `opus4.8`, `fable`, `gpt5.6` or `gemini3.6-flash` and the server maps them to real model IDs
- **Auto-dependency install** — missing Python packages (flask, requests) are installed automatically on first launch
- **All operations in-process** — Fusion API calls stay on your machine; only the chat protocol leaves your machine

## Supported AI Providers

| Provider | API Format | Key Models |
|----------|-----------|------------|
| **DeepSeek** | OpenAI-compatible | deepseek-v4-flash, v4-pro, chat, reasoner |
| **Kimi (Moonshot)** | OpenAI-compatible | moonshot-v1-auto, 8k, 32k, 128k |
| **Zhipu GLM** | OpenAI-compatible | glm-4-plus, glm-4-flash, glm-4, glm-4-air |
| **OpenAI** | OpenAI-compatible | gpt-4o, gpt-4-turbo, gpt-5, o3-mini |
| **Anthropic Claude** | Native Messages API | claude-opus-4, claude-sonnet-4, claude-3-5-haiku |
| **Google Gemini** | OpenAI-compatible | gemini-2.5-flash, gemini-2.5-pro |

## System Requirements

- **Windows** 10/11 (macOS supported by manifest but not yet tested)
- **Autodesk Fusion 360** (any recent version with Add-In support)
- **Python** — Fusion 360 bundles its own Python 3.14, no separate install needed
- **Internet connection** — for AI API calls

## Quick Install

### 1. Download

```powershell
# Clone from GitHub
git clone https://github.com/YOUR_USERNAME/AIFusion.git "$env:APPDATA\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion"
```

Or download the ZIP, extract to:
```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion\
```

### 2. Install Dependencies

**Run as Administrator:**

```batch
cd "%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion"
install_deps.bat
```

This script automatically:
1. Finds Fusion 360's bundled Python
2. Bootstraps pip if missing (Fusion's Python has no pip by default)
3. Installs `flask` and `requests`

> **Note:** The add-in will auto-install dependencies on first run if they're missing. Running `install_deps.bat` beforehand avoids the first-launch delay.

### 3. Configure an API Key

Open `local_server\config.json` and add at least one API key:

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "deepseek_api_key": "sk-your-deepseek-key-here",
  "openai_api_key": "",
  "anthropic_api_key": "",
  …
}
```

**Where to get keys:**
- DeepSeek: [platform.deepseek.com](https://platform.deepseek.com)
- Zhipu GLM: [open.bigmodel.cn](https://open.bigmodel.cn)
- OpenAI: [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Anthropic: [console.anthropic.com](https://console.anthropic.com)
- Google Gemini: [aistudio.google.com](https://aistudio.google.com) (free tier available)
- Kimi: [platform.moonshot.cn](https://platform.moonshot.cn)

### 4. Enable in Fusion 360

1. Fully quit Fusion 360 and restart
2. Press `Shift+S` → **Add-Ins** tab → find **AIFusion**
3. Click **Run**, then tick **Run on Startup**
4. The AI Fusion panel appears docked on the right side

## Project Structure

```
AIFusion/
├── AIFusion.py                 # Add-in entry point (loaded by Fusion)
├── AIFusion.manifest            # Fusion add-in manifest
├── install_deps.bat             # Dependency installer (run once)
├── key_vault.py                 # Machine-bound encrypted API key storage
├── resources/
│   └── palette.html             # Loading/fallback page
├── bridge/
│   ├── palette.pyc              # Message bridge (HTML ↔ Python)
│   └── selection_watcher.pyc    # Selection change monitor
├── handlers/
│   ├── __init__.pyc             # Tool router (dispatch)
│   ├── read.pyc                 # Query model state
│   ├── create.pyc               # Create simple features
│   ├── execute.pyc              # Execute Python scripts
│   ├── update.pyc               # Modify features
│   ├── delete.pyc               # Delete features
│   └── api_documentation.pyc    # API doc search
└── local_server/
    ├── server.py                # Flask backend (multi-provider routing)
    ├── chat_ui.html             # Chat interface (loaded in Fusion palette)
    ├── config.json              # AI provider & API key configuration
    ├── requirements.txt         # Python dependencies
    └── __init__.py              # Package init
```

## How It Works

1. Fusion loads `AIFusion.py` → starts a local Flask server on port 8765
2. A docked palette (Qt WebView) loads `chat_ui.html`
3. User types a request → POST `/api/chat` → Flask proxies to the configured AI provider
4. AI returns tool calls (e.g., `execute` with a Python script)
5. JS bridge sends tool calls to Fusion via `adsk.fusionSendData`
6. `handlers/*.pyc` execute the Fusion API calls in-process
7. Results flow back to the AI for the next turn (max 10 rounds)

## Switching Providers

After opening the AI Fusion panel:
1. Click the ⚙ badge in the header → Settings panel opens
2. Select a provider from the dropdown (DeepSeek, Kimi, Zhipu GLM, OpenAI, Anthropic Claude, Google Gemini)
3. Select a model from the model dropdown
4. Paste the provider's API key
5. Click **Save**

If exactly one provider has a key, it's auto-selected on startup.

## FAQ

### The plugin shows "No API key set"

Open Settings (⚙), select your provider, paste the API key, and click Save. The key is stored in `local_server\config.json`.

### I see `ModuleNotFoundError: No module named 'flask'`

Run `install_deps.bat` as Administrator. Fusion's Python is minimal and has no pip by default — the script handles both bootstrapping and installing.

### The server shows "Cannot connect to …"

- Check your internet connection
- Verify the API key is correct (no extra spaces)
- Some providers require an active subscription (check your billing dashboard)
- Try switching to a different provider

### I configured a different key but Fusion still uses the old one

There may be a `DEEPSEEK_API_KEY` (or similar) environment variable overriding the config file. Remove it from your system environment variables, or the new server code (v1.0+) prioritizes `config.json` over environment variables.

### The model draws incorrectly or crashes

AI Fusion's system prompt contains verified Fusion API signatures for 15 operations. If you encounter an error:
1. Check the `aifusion_debug.log` file for the exact API error
2. The repair loop in the system prompt covers 7 common failure patterns
3. File an issue on GitHub with the log excerpt

## Privacy & Security

- **API keys**: Stored in `local_server\config.json` locally. An optional `key_vault.py` module provides machine-bound encryption.
- **Fusion files**: Never uploaded. All Fusion API work is in-process.
- **Chat data**: Only the conversation text and tool call results are sent to your chosen AI provider.
- **Network**: Only `localhost:8765` → AI provider API endpoints.

## Uninstall

1. Quit Fusion 360
2. Delete the folder:
   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\AIFusion
   ```
3. Restart Fusion

## Contributing

Contributions welcome. The `.pyc` files in `bridge/` and `handlers/` are compiled from the original AI Fusion release and handle the Fusion API bridge. The `local_server/` directory (server.py, chat_ui.html, config.json) is the editable layer where most optimization work happens.

## License

This project is licensed under the MIT License. See `LICENSE` for details.

---

