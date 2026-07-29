"""
AIFusion 多模型支持服务端 — Multi-Provider Chat Server

Supports 6 providers with automatic format adaptation:
  OpenAI-compatible (HTTP POST /chat/completions):
    - DeepSeek, Kimi (Moonshot), Zhipu GLM, OpenAI, Google Gemini (via OpenAI compat layer)

  Anthropic Messages API (HTTP POST /v1/messages):
    - Claude Opus 4.8, Opus 5.0, Fable  — uses a built-in format adapter

Architecture:
  chat_ui.html  →  POST /api/chat  {messages, tools, model, provider}
                 →  provider_router  →  openai_compatible_call()  or  anthropic_call()
                 →  return unified OpenAI-formatted response to UI

The Fusion add-in palette loads the chat UI; tool calls are dispatched to Fusion
via the existing adsk.fusionSendData bridge (unchanged).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests
from flask import Flask, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _DIR / "config.json"
_DEFAULT_PORT = 8765

# ---------------------------------------------------------------------------
# ── Fusion tool definitions (shared across all providers) ──────────────
# ---------------------------------------------------------------------------
FUSION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Query information from the active Fusion 360 design. "
                "Use this BEFORE making any changes — understand the current "
                "model state thoroughly. Supports listing bodies, measuring "
                "dimensions, searching API documentation, browsing material "
                "libraries, inspecting the timeline, and more.\n\n"
                "Common workflow: list bodies → analyze bbox/zones → "
                "identify components → check API docs → build geometry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queryType": {
                        "type": "string",
                        "description": "What kind of information to read.",
                        "enum": [
                            "bodies", "faces", "edges", "features",
                            "sketches", "sketchProfiles",
                            "parameters", "userParameters", "modelParameters",
                            "materialLibraries", "materialAppearance",
                            "selection", "selectionSets",
                            "timeline", "timelineStatus",
                            "document", "projects",
                            "volume", "area", "length", "centroid",
                            "similar", "edgesByType",
                            "apiDocumentation", "screenshot",
                        ],
                    },
                    "entityToken": {"type": "string"},
                    "featureType": {"type": "string"},
                    "apiCategory": {"type": "string", "enum": ["function", "member", "class", "property", "enum"]},
                    "searchPattern": {"type": "string"},
                    "userDescription": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer"},
                    "sortBy": {"type": "string"},
                },
                "required": ["queryType"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute",
            "description": (
                "Execute Python scripts directly against the Fusion 360 API. "
                "THIS IS YOUR PRIMARY TOOL for building complex geometry.\n\n"
                "REQUIRED WORKFLOW:\n"
                "1. CAD Brief — plan dimensions, features, origin, validation before code\n"
                "2. Build — one comprehensive script using verified API patterns\n"
                "3. Verify — print bbox, volume, body count, healthState after every feature\n"
                "4. Repair — adjust params and retry on failure; never give up\n\n"
                "Script must define `def run(_context):`. Use `MM = 0.1` for cm→mm conversion.\n"
                "Always print bounding boxes and volumes for verification.\n\n"
                "**CRITICAL: RevolveFeatures.createInput(profiles, axis, operation) needs ALL 3 ARGS.**\n"
                "**CRITICAL: Bodies are accessed via root.bRepBodies — NOT root.bodies or component.bodies.**\n"
                "**CRITICAL: Always check sk.profiles.count > 0 before ExtrudeInput or RevolveInput.**"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "featureType": {"type": "string", "enum": ["script", "document", "object"]},
                    "script": {"type": "string"},
                    "action": {"type": "string", "enum": ["open", "close", "save"]},
                    "fileId": {"type": "string"},
                },
                "required": ["featureType"],
            },
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# ── PROVIDER REGISTRY  ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════
# Each provider entry defines:
#   api_format    "openai" or "anthropic"
#   base_url      API endpoint base (without /chat/completions or /v1/messages)
#   auth_header   "Bearer {key}" or "x-api-key: {key}"
#   models        list of available model IDs
#   config_key    key in config.json holding the API key
#   env_var       environment variable override for the API key

PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "api_format": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "config_key": "deepseek_api_key",
        "auth_style": "bearer",
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "api_format": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-auto", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "config_key": "kimi_api_key",
        "auth_style": "bearer",
    },
    "zhipu": {
        "label": "Zhipu GLM",
        "api_format": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash", "glm-4", "glm-4-air", "glm-4-long"],
        "config_key": "zhipu_api_key",
        "auth_style": "bearer",
    },
    "openai": {
        "label": "OpenAI",
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4.1", "gpt-5", "gpt-5-mini", "gpt-5-nano", "o3-mini", "o4-mini"],
        "config_key": "openai_api_key",
        "auth_style": "bearer",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "api_format": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-3-5-haiku-latest",
            "claude-3-5-sonnet-latest",
        ],
        "config_key": "anthropic_api_key",
        "auth_style": "x-api-key",
        "api_version": "2023-06-01",  # Anthropic API version header
    },
    "google_gemini": {
        "label": "Google Gemini",
        "api_format": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ],
        "config_key": "google_api_key",
        "auth_style": "bearer",
    },
}

# Aliases for model IDs that users might type (e.g., "opus4.8" → "claude-opus-4-20250514")
_MODEL_ALIASES: dict[str, str] = {
    "opus4.8": "claude-opus-4-20250514",
    "opus-4.8": "claude-opus-4-20250514",
    "opus5.0": "claude-sonnet-4-20250514",
    "opus-5.0": "claude-sonnet-4-20250514",
    "fable": "claude-3-5-haiku-latest",
    "glm-5.2": "glm-4-plus",
    "gpt5.6": "gpt-5",
    "gpt-5.6": "gpt-5",
    "gemini3.6-flash": "gemini-2.5-flash",
    "gemini-3.6-flash": "gemini-2.5-flash",
}


# ═══════════════════════════════════════════════════════════════════════════
# ── PROVIDER AUTO-DETECTION  ──────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

def _auto_detect_provider(cfg: dict[str, Any]) -> str | None:
    """Return the sole provider that has an API key configured.

    Scans all registered providers. If exactly one has a non-empty API key
    (checked in config.json first, then environment variables), returns its id.
    If zero or multiple have keys, returns None — the caller should fall back
    to the stored config['provider'] or a hard-coded default.
    """
    configured: list[str] = []
    for pid, prov in PROVIDERS.items():
        cfg_key = prov["config_key"]
        key = cfg.get(cfg_key, "") or os.environ.get(cfg_key.upper(), "")
        if key.strip():
            configured.append(pid)
    if len(configured) == 1:
        return configured[0]
    return None


# ═══════════════════════════════════════════════════════════════════════════
# ── CONFIG MANAGEMENT  ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_SYSTEM_PROMPT = (
    "You are AI Fusion, an AI copilot for Autodesk Fusion 360. "
    "You help users create, modify, and explore 3D CAD models through "
    "natural language conversation.\n\n"
    "## Tools at your disposal\n"
    "- **read**: query the model (bodies, features, sketches, etc.)\n"
    "- **execute**: run Python scripts against the Fusion API — THIS IS YOUR PRIMARY TOOL\n\n"
    "## Workflow: Analyze → Plan → Build → Verify → Refine\n"
    "## Critical: Fusion uses cm internally. Always use MM=0.1 constant. "
    "Print bounding boxes and volumes after every build."
)

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "",  # auto-detected from configured API keys at startup
    "model": "",     # auto-resolved per provider
    # ── API keys per provider ──
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "kimi_api_key": "",
    "kimi_base_url": "https://api.moonshot.cn/v1",
    "zhipu_api_key": "",
    "zhipu_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "anthropic_api_key": "",
    "anthropic_base_url": "https://api.anthropic.com",
    "google_api_key": "",
    "google_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    # ── general ──
    "server_port": _DEFAULT_PORT,
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            cfg.update(disk)
    except Exception:
        pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)


def _resolve_model(provider_id: str, model_id: str) -> str:
    """Apply aliases so user-friendly model names map to real IDs."""
    key = model_id.lower().strip()
    if key in _MODEL_ALIASES:
        return _MODEL_ALIASES[key]
    return model_id


# ═══════════════════════════════════════════════════════════════════════════
# ── ANTHROPIC FORMAT ADAPTER  ─────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════
# Converts OpenAI-format messages+tools → Anthropic Messages API format,
# and converts the response back to OpenAI format so the chat UI
# does not need to know about provider-specific formats.

def _anthropic_convert_messages(openai_messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """
    Convert OpenAI-format messages → Anthropic Messages API format.

    OpenAI roles:  system, user, assistant, tool
    Anthropic roles: user, assistant

    Returns (anthropic_messages, system_prompt)
    """
    system_content = None
    anthropic_msgs: list[dict[str, Any]] = []

    for msg in openai_messages:
        role = msg.get("role", "")

        if role == "system":
            system_content = msg.get("content", "")
            continue

        if role == "user":
            anthropic_msgs.append({"role": "user", "content": msg.get("content", "")})

        elif role == "assistant":
            if msg.get("tool_calls"):
                # Convert tool_calls → Anthropic tool_use blocks
                content_blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    try:
                        tool_input = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        tool_input = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": tool_input,
                    })
                anthropic_msgs.append({"role": "assistant", "content": content_blocks})
            else:
                anthropic_msgs.append({"role": "assistant", "content": msg.get("content", "")})

        elif role == "tool":
            # Anthropic requires tool results in a USER message
            anthropic_msgs.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }],
            })

    return anthropic_msgs, system_content


def _anthropic_convert_tools(openai_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI tool definitions → Anthropic tool format."""
    anthropic_tools: list[dict[str, Any]] = []
    for t in openai_tools:
        func = t.get("function", {})
        anthropic_tools.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
        })
    return anthropic_tools


def _anthropic_response_to_openai(anthropic_resp: dict[str, Any]) -> dict[str, Any]:
    """
    Convert Anthropic Messages API response → OpenAI-compatible format.

    Anthropic response structure:
    {
      "id": "...",
      "type": "message",
      "role": "assistant",
      "content": [{"type": "text", "text": "..."}, {"type": "tool_use", ...}],
      "model": "...",
      "stop_reason": "end_turn" | "tool_use",
      "usage": {"input_tokens": ..., "output_tokens": ...}
    }

    OpenAI response structure:
    {
      "id": "...",
      "object": "chat.completion",
      "model": "...",
      "choices": [{"index": 0, "message": {"role": "assistant", "content": ..., "tool_calls": [...]}, "finish_reason": "..."}],
      "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}
    }
    """
    content_blocks = anthropic_resp.get("content", [])
    text_content = ""
    tool_calls: list[dict[str, Any]] = []

    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "text":
            text_content += block.get("text", "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{len(tool_calls)}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    finish_reason = "tool_calls" if tool_calls else ("stop" if anthropic_resp.get("stop_reason") == "end_turn" else "stop")
    usage = anthropic_resp.get("usage", {})

    return {
        "id": anthropic_resp.get("id", ""),
        "object": "chat.completion",
        "model": anthropic_resp.get("model", ""),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text_content or None,
                "tool_calls": tool_calls if tool_calls else None,
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# ── API CALL DISPATCH  ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

def _openai_compatible_call(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | None,
    timeout: int = 180,
) -> tuple[int, dict[str, Any]]:
    """Call an OpenAI-compatible /chat/completions endpoint."""
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    resp = requests.post(endpoint, json=body, headers=headers, timeout=timeout)
    return resp.status_code, resp.json()


def _anthropic_call(
    base_url: str,
    api_key: str,
    api_version: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout: int = 180,
) -> tuple[int, dict[str, Any]]:
    """Call the Anthropic Messages API with format conversion."""
    anthropic_msgs, system = _anthropic_convert_messages(messages)
    anthropic_tools = _anthropic_convert_tools(tools) if tools else None

    body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_msgs,
        "max_tokens": 8192,
    }
    if system:
        body["system"] = system
    if anthropic_tools:
        body["tools"] = anthropic_tools

    headers = {
        "x-api-key": api_key,
        "anthropic-version": api_version,
        "Content-Type": "application/json",
    }
    endpoint = f"{base_url.rstrip('/')}/v1/messages"

    resp = requests.post(endpoint, json=body, headers=headers, timeout=timeout)

    if resp.status_code == 200:
        return 200, _anthropic_response_to_openai(resp.json())
    return resp.status_code, resp.json()


# ═══════════════════════════════════════════════════════════════════════════
# ── FLASK APPLICATION  ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

def create_app() -> Flask:
    import logging as _flog
    _flog.getLogger("werkzeug").setLevel(_flog.ERROR)

    app = Flask(__name__, static_folder=str(_DIR))

    # ── /chat ──
    @app.route("/chat")
    def chat_ui():
        return send_from_directory(str(_DIR), "chat_ui.html")

    # ── /api/chat (multi-provider routing) ──
    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        cfg = load_config()
        payload = request.get_json(force=True)

        messages: list[dict[str, Any]] = payload.get("messages", [])
        tools_supplied: list[dict[str, Any]] | None = payload.get("tools")
        tool_choice: str | None = payload.get("tool_choice")

        # Inject system prompt if not present
        if not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": cfg["system_prompt"]})

        # Resolve provider: payload > config > auto-detect (sole configured) > hard-coded
        provider_id = payload.get("provider") or cfg.get("provider") or _auto_detect_provider(cfg) or "deepseek"
        if provider_id not in PROVIDERS:
            # Try case-insensitive match
            found = False
            for pid in PROVIDERS:
                if pid.lower() == provider_id.lower():
                    provider_id = pid
                    found = True
                    break
            if not found:
                return jsonify({"error": f"Unknown provider: {provider_id}. Available: {list(PROVIDERS.keys())}"}), 400

        prov = PROVIDERS[provider_id]
        api_format = prov["api_format"]

        # Resolve model: payload > config > provider's first model
        model = payload.get("model") or cfg.get("model") or prov["models"][0]
        model = _resolve_model(provider_id, model)

        # Get API key: payload > config (user's saved choice) > env var
        config_key = prov["config_key"]
        api_key = payload.get("api_key") or cfg.get(config_key, "") or os.environ.get(config_key.upper(), "")
        if not api_key:
            return jsonify({
                "error": (
                    f"No API key configured for {prov['label']}. "
                    f"Set '{config_key}' in config.json or provide api_key in the request."
                )
            }), 400

        # Get base URL: payload > config > provider default
        base_url_key = f"{provider_id}_base_url"
        base_url = payload.get("base_url") or cfg.get(base_url_key) or prov["base_url"]

        # Use payload tools or default Fusion tools
        effective_tools = tools_supplied if tools_supplied is not None else FUSION_TOOLS

        try:
            if api_format == "openai":
                status, data = _openai_compatible_call(
                    base_url=base_url, api_key=api_key, model=model,
                    messages=messages, tools=effective_tools,
                    tool_choice=tool_choice,
                )
            elif api_format == "anthropic":
                status, data = _anthropic_call(
                    base_url=base_url, api_key=api_key,
                    api_version=prov.get("api_version", "2023-06-01"),
                    model=model, messages=messages, tools=effective_tools,
                )
            else:
                return jsonify({"error": f"Unsupported API format: {api_format}"}), 500

        except requests.exceptions.Timeout:
            return jsonify({"error": f"{prov['label']} request timed out (180s)."}), 504
        except requests.exceptions.ConnectionError:
            return jsonify({"error": f"Cannot connect to {prov['label']} API ({base_url})"}), 502
        except Exception as exc:
            return jsonify({"error": f"Unexpected error calling {prov['label']}: {exc}"}), 500

        if status == 200:
            return jsonify(data)
        else:
            detail = ""
            try:
                detail = json.dumps(data)[:2000]
            except Exception:
                detail = str(data)[:2000]
            return jsonify({
                "error": f"{prov['label']} returned {status}: {detail}",
            }), 502

    # ── /api/models ──
    @app.route("/api/models")
    def api_models():
        cfg = load_config()

        providers_out: dict[str, Any] = {}
        for pid, prov in PROVIDERS.items():
            config_key = prov["config_key"]
            providers_out[pid] = {
                "label": prov["label"],
                "api_format": prov["api_format"],
                "models": prov["models"],
                "base_url": cfg.get(f"{pid}_base_url") or prov["base_url"],
                "configured": bool(cfg.get(config_key) or os.environ.get(config_key.upper())),
                "config_key": config_key,
            }

        configured_count = sum(1 for p in providers_out.values() if p["configured"])
        return jsonify({
            "current_provider": cfg.get("provider", ""),
            "current_model": cfg.get("model", ""),
            "default_provider": cfg.get("provider") or _auto_detect_provider(cfg),
            "configured_count": configured_count,
            "providers": providers_out,
            "aliases": _MODEL_ALIASES,
        })

    # ── /api/config ──
    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if request.method == "GET":
            cfg = load_config()
            safe = dict(cfg)
            # Redact every API key field
            for key in list(safe.keys()):
                if key.endswith("_api_key") and len(safe.get(key, "")) > 4:
                    safe[key] = "***" + safe[key][-4:]
            return jsonify(safe)

        cfg = load_config()
        updates = request.get_json(force=True)

        allowed_keys: set[str] = {
            "provider", "model", "system_prompt",
        }
        # Add per-provider API key and base_url keys
        for pid in PROVIDERS:
            allowed_keys.add(PROVIDERS[pid]["config_key"])
            allowed_keys.add(f"{pid}_base_url")

        for k, v in updates.items():
            if k in allowed_keys:
                cfg[k] = v
        save_config(cfg)
        return jsonify({"status": "ok", "updated": list(updates.keys())})

    # ── CORS ──
    _LOCAL_ORIGINS = frozenset({
        "http://127.0.0.1:8765",
        f"http://127.0.0.1:{_DEFAULT_PORT}",
        "null",
    })

    @app.after_request
    def add_cors_headers(resp):
        origin = request.headers.get("Origin", "")
        if origin in _LOCAL_ORIGINS or not origin:
            resp.headers["Access-Control-Allow-Origin"] = origin or "http://127.0.0.1:8765"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-AIFusion-Token"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    return app


# ═══════════════════════════════════════════════════════════════════════════
# ── LAUNCH HELPERS  ───────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

def start_in_thread(port: int | None = None):
    import threading
    port = port or int(os.environ.get("AIFUSION_PORT", _DEFAULT_PORT))
    if not _CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)

    _app = create_app()

    def _run():
        try:
            _app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
        except Exception as exc:
            print(f"[aifusion] Flask thread error: {exc}")

    t = threading.Thread(target=_run, daemon=True, name="AIFusionServer")
    t.start()
    return t


def wait_until_ready(host: str = "127.0.0.1", port: int = _DEFAULT_PORT,
                     timeout: float = 6.0) -> bool:
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"http://{host}:{port}/api/models", method="GET"),
                timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> None:
    port = int(os.environ.get("AIFUSION_PORT", _DEFAULT_PORT))
    if not _CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        print(f"[aifusion] Created default config at {_CONFIG_PATH}")
        print("[aifusion] Add your API keys to config.json before using.")

    app = create_app()
    print(f"[aifusion] Multi-provider server on http://127.0.0.1:{port}")
    print(f"[aifusion] Providers: {', '.join(p['label'] for p in PROVIDERS.values())}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
