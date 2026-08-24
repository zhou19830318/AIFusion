"""
AIFusion 多模型支持服务端 — Multi-Provider Chat Server

Supports 6 providers with automatic format adaptation:
  OpenAI-compatible (HTTP POST /chat/completions):
    - DeepSeek, Kimi (Moonshot), Zhipu GLM, OpenAI, Google Gemini (via OpenAI compat layer)

  Anthropic Messages API (HTTP POST /v1/messages):
    - Claude Sonnet 5 / Fable 5  — uses a built-in format adapter

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
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest
from .history_store import HistoryStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _DIR / "config.json"
_DEFAULT_PORT = 8765
_SERVICE_VERSION = "1.1.0"
_MAX_CONTENT_BYTES = 32 * 1024 * 1024
_MAX_MESSAGES = 80
_MAX_MESSAGE_CHARS = 120_000
_MAX_SCRIPT_CHARS = 120_000
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 180
_MAX_PROVIDER_RETRIES = 2
_MAX_HISTORY_STATE_BYTES = 8 * 1024 * 1024
_REDACTED_KEY_PREFIX = "***"

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
                    "operation": {
                        "type": "string",
                        "description": "Required when queryType=document.",
                        "enum": ["open", "close", "save", "active", "name", "path"],
                    },
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
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        # DeepSeek 官方 API (api.deepseek.com) 的 messages.content 仅接受 text，
        # 全系不支持图片输入（第三方托管平台另有多模态版本，与本配置无关）
        "vision_models": [],
        "config_key": "deepseek_api_key",
        "auth_style": "bearer",
    },
    "kimi": {
        "label": "Kimi (Moonshot)",
        "api_format": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k3"],
        "config_key": "kimi_api_key",
        "auth_style": "bearer",
    },
    "zhipu": {
        "label": "Zhipu GLM",
        "api_format": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.3"],
        # GLM-5.x 系列不支持图片输入（多模态仅 glm-5v-turbo）
        "vision_models": [],
        "config_key": "zhipu_api_key",
        "auth_style": "bearer",
    },
    "openai": {
        "label": "OpenAI",
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        "config_key": "openai_api_key",
        "auth_style": "bearer",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "api_format": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-sonnet-5", "claude-fable-5"],
        "config_key": "anthropic_api_key",
        "auth_style": "x-api-key",
        "api_version": "2023-06-01",  # Anthropic API version header
    },
    "google_gemini": {
        "label": "Google Gemini",
        "api_format": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-3.7-flash"],
        "config_key": "google_api_key",
        "auth_style": "bearer",
    },
}

# Aliases for model IDs that users might type (friendly names → real API IDs)
_MODEL_ALIASES: dict[str, str] = {
    # ── Anthropic Claude ──
    "sonnet5": "claude-sonnet-5",
    "sonnet-5": "claude-sonnet-5",
    "fable": "claude-fable-5",
    # ── OpenAI ──
    "gpt5.6": "gpt-5.6-sol",
    "gpt-5.6": "gpt-5.6-sol",
    "gpt-5.6-sol": "gpt-5.6-sol",
    # ── Google Gemini ──
    "gemini3.7": "gemini-3.7-flash",
    "gemini-3.7": "gemini-3.7-flash",
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
    "Print bounding boxes and volumes after every build.\n\n"
    "## Text labels on CAD faces\n"
    "When creating embossed port labels, do not guess the Fusion API signature. "
    "Fusion SketchTexts.createInput requires BOTH arguments: "
    "SketchTexts.createInput(text, position), where position is an adsk.core.Point3D. "
    "Typical pattern: txt = sketch.sketchTexts.createInput('USB', adsk.core.Point3D.create(x, y, 0)); "
    "then set txt.height/textAngle as needed and add it with sketch.sketchTexts.add(txt). "
    "Never call createInput(text) with one argument. Create one sketch/text feature per label or a controlled batch, "
    "validate the profile/text count, then extrude only the requested labels. "
    "For side openings, first identify the common side plane and map each opening center into that sketch coordinate system. "
    "Exclude cooling/vent holes. In the current enclosure task, the USB and button openings are on the same side and the opening nearest USB is BOOT; do not infer other port names without reading the geometry or asking the user."
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

_CAD_TEXT_API_GUIDANCE = (
    "\n\n## Mandatory Fusion text API reminder\n"
    "SketchTexts.createInput(text, position) requires two arguments; position must be adsk.core.Point3D. "
    "Do not call SketchTexts.createInput(text) with one argument. For side labels, map opening centers to one common side-plane sketch, "
    "exclude cooling/vent holes, and verify each label feature after creation."
)


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                disk = json.load(fh)
            cfg.update(disk)
    except (OSError, json.JSONDecodeError):
        # Keep the defaults usable if a previous process was interrupted while
        # writing config.json.  Do not overwrite the user's file here.
        pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically replace config.json so a crash cannot leave truncated JSON."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{_CONFIG_PATH.name}.", suffix=".tmp", dir=str(_CONFIG_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, _CONFIG_PATH)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _request_id() -> str:
    return uuid.uuid4().hex[:12]


def _error_response(message: str, code: str, status: int, request_id: str, retryable: bool = False):
    return jsonify({
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
        }
    }), status


def _validate_chat_payload(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or not messages or len(messages) > _MAX_MESSAGES:
        raise ValueError(f"messages must be a non-empty list of at most {_MAX_MESSAGES} items")
    total_chars = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("each message must be an object with a valid role")
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            total_chars += len(json.dumps(content, ensure_ascii=False))
        elif content is not None:
            raise ValueError("message content must be text, multimodal parts, or null")
    if total_chars > _MAX_MESSAGE_CHARS:
        raise ValueError(f"message content exceeds {_MAX_MESSAGE_CHARS} characters")
    tools = payload.get("tools")
    if tools is not None and (not isinstance(tools, list) or len(tools) > 32):
        raise ValueError("tools must be a list of at most 32 items")
    tool_choice = payload.get("tool_choice")
    if tool_choice is not None and not isinstance(tool_choice, (str, dict)):
        raise ValueError("tool_choice must be a string or object")
    return messages, tools, tool_choice


def _response_json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"provider returned non-JSON response (HTTP {resp.status_code})") from exc
    if not isinstance(data, dict):
        raise ValueError("provider returned a non-object JSON response")
    return data


def _post_json_with_retry(endpoint: str, *, json_body: dict[str, Any], headers: dict[str, str],
                          retry_statuses: set[int] | None = None) -> tuple[int, dict[str, Any]]:
    retry_statuses = retry_statuses or {408, 429, 500, 502, 503, 504}
    last_status = 502
    last_data: dict[str, Any] = {}
    for attempt in range(_MAX_PROVIDER_RETRIES + 1):
        try:
            resp = requests.post(
                endpoint, json=json_body, headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            last_status = resp.status_code
            last_data = _response_json(resp)
            if resp.status_code not in retry_statuses or attempt >= _MAX_PROVIDER_RETRIES:
                return last_status, last_data
            time.sleep(min(2 ** attempt, 4))
        except requests.exceptions.Timeout:
            if attempt >= _MAX_PROVIDER_RETRIES:
                raise
            time.sleep(min(2 ** attempt, 4))
    return last_status, last_data


def _redact_history_value(value: Any) -> Any:
    """Keep resumable text/tool state but never persist image base64 or secrets."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.lower() in {"api_key", "authorization", "dataurl", "data", "base64"}:
                out[key] = "[redacted for local history]"
            elif isinstance(item, str) and (item.startswith("data:") or (key.lower() == "url" and len(item) > 256)):
                out[key] = "[redacted for local history]"
            else:
                out[key] = _redact_history_value(item)
        return out
    if isinstance(value, list):
        return [_redact_history_value(item) for item in value]
    return value


def _resolve_model(provider_id: str, model_id: str) -> str:
    """Apply aliases so user-friendly model names map to real IDs."""
    key = model_id.lower().strip()
    if key in _MODEL_ALIASES:
        return _MODEL_ALIASES[key]
    return model_id


def _model_has_vision(prov: dict[str, Any], model: str) -> bool:
    """模型是否支持图片输入。vision_models 未声明 = 全部支持;
    空列表 = 全部不支持;有列表 = 仅列表内模型支持。"""
    vm = prov.get("vision_models")
    if vm is None:
        return True
    return model in set(vm)


def _strip_unsupported_images(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """纵深防御:把 user 消息中的图片附件替换为文本提示(供纯文本模型调用时防御 400)。"""
    changed = 0
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    changed += 1
                    parts.append({"type": "text", "text": "[图片附件已自动忽略:当前模型不支持图片输入。如需查看附件,请切换到支持视觉的模型(Kimi K3 / GPT-5.6 / Claude Sonnet 5 / Gemini 3.7 Flash)]"})
                else:
                    parts.append(p)
            out.append({**m, "content": parts})
        else:
            out.append(m)
    return out, changed


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
            content = msg.get("content", "")
            if isinstance(content, list):
                # OpenAI 多模态 user 消息(含 image_url) → Anthropic image blocks
                blocks: list[dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        blocks.append({"type": "text", "text": str(part)})
                        continue
                    if part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:"):
                            meta, _, b64data = url.partition(",")
                            media_type = meta[5:].split(";")[0] or "image/png"
                            if media_type == "application/pdf":
                                # PDF 附件 → Anthropic document block
                                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64data}})
                            else:
                                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}})
                        else:
                            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                    else:
                        blocks.append({"type": "text", "text": part.get("text", "")})
                anthropic_msgs.append({"role": "user", "content": blocks})
            else:
                anthropic_msgs.append({"role": "user", "content": content})

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

    return _post_json_with_retry(endpoint, json_body=body, headers=headers)


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

    status, data = _post_json_with_retry(endpoint, json_body=body, headers=headers)
    if status == 200:
        return 200, _anthropic_response_to_openai(data)
    return status, data


# ═══════════════════════════════════════════════════════════════════════════
# ── FLASK APPLICATION  ────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════

def create_app() -> Flask:
    import logging as _flog
    _flog.getLogger("werkzeug").setLevel(_flog.ERROR)

    app = Flask(__name__, static_folder=str(_DIR))
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_BYTES
    history = HistoryStore()

    @app.errorhandler(413)
    def request_too_large(_error):
        return _error_response("request body is too large", "payload_too_large", 413, _request_id())

    # ── durable project/session history ──
    @app.route("/api/projects", methods=["GET", "POST"])
    def api_projects():
        if request.method == "GET":
            return jsonify({"projects": history.list_projects()})
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or not isinstance(body.get("name", ""), str):
            return _error_response("project name is required", "invalid_request", 400, _request_id())
        return jsonify({"project": history.create_project(body["name"])})

    @app.route("/api/projects/<project_id>/sessions", methods=["GET", "POST"])
    def api_sessions(project_id: str):
        if request.method == "GET":
            return jsonify({"sessions": history.list_sessions(project_id)})
        body = request.get_json(silent=True) or {}
        title = body.get("title", "New design session") if isinstance(body, dict) else "New design session"
        if not isinstance(title, str):
            return _error_response("session title must be a string", "invalid_request", 400, _request_id())
        try:
            return jsonify({"session": history.create_session(project_id, title)})
        except KeyError:
            return _error_response("project not found", "not_found", 404, _request_id())

    @app.route("/api/sessions/<session_id>", methods=["GET"])
    def api_session(session_id: str):
        session = history.get_session(session_id)
        if not session:
            return _error_response("session not found", "not_found", 404, _request_id())
        return jsonify({"session": session})

    @app.route("/api/sessions/<session_id>/events", methods=["POST"])
    def api_session_event(session_id: str):
        request_id = _request_id()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("event_type"), str) or not isinstance(body.get("state"), dict):
            return _error_response("event_type and state are required", "invalid_request", 400, request_id)
        state = _redact_history_value(body["state"])
        if len(json.dumps(state, ensure_ascii=False)) > _MAX_HISTORY_STATE_BYTES:
            return _error_response("session state is too large", "history_too_large", 413, request_id)
        payload = body.get("payload", {})
        if not isinstance(payload, dict):
            return _error_response("event payload must be an object", "invalid_request", 400, request_id)
        try:
            saved = history.append_event(session_id, body["event_type"], _redact_history_value(payload), state)
        except KeyError:
            return _error_response("session not found", "not_found", 404, request_id)
        return jsonify({"ok": True, **saved})

    @app.route("/api/sessions/<session_id>/archive", methods=["POST"])
    def api_archive_session(session_id: str):
        history.archive_session(session_id)
        return jsonify({"ok": True})

    @app.route("/api/health")
    def api_health():
        cfg = load_config()
        configured = sum(
            bool(cfg.get(p["config_key"]) or os.environ.get(p["config_key"].upper()))
            for p in PROVIDERS.values()
        )
        return jsonify({
            "ok": True,
            "service": "aifusion-local-server",
            "version": _SERVICE_VERSION,
            "configured_count": configured,
        })

    # ── /chat ──
    @app.route("/chat")
    def chat_ui():
        return send_from_directory(str(_DIR), "chat_ui.html")

    # ── /api/chat (multi-provider routing) ──
    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        request_id = _request_id()
        cfg = load_config()
        try:
            payload = request.get_json(silent=False)
            messages, tools_supplied, tool_choice = _validate_chat_payload(payload)
        except (TypeError, ValueError, BadRequest) as exc:
            return _error_response(str(exc), "invalid_request", 400, request_id)

        # Inject system prompt if not present
        if not any(m.get("role") == "system" for m in messages):
            system_prompt = cfg["system_prompt"]
            if "SketchTexts.createInput(text, position)" not in system_prompt:
                system_prompt += _CAD_TEXT_API_GUIDANCE
            messages.insert(0, {"role": "system", "content": system_prompt})

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
                return _error_response(
                    f"Unknown provider: {provider_id}. Available: {list(PROVIDERS.keys())}",
                    "unknown_provider", 400, request_id,
                )

        prov = PROVIDERS[provider_id]
        api_format = prov["api_format"]

        # Resolve model: payload > config > provider's first model
        model = payload.get("model") or cfg.get("model") or prov["models"][0]
        model = _resolve_model(provider_id, model)

        # Get API key: payload > config (user's saved choice) > env var
        config_key = prov["config_key"]
        api_key = payload.get("api_key") or cfg.get(config_key, "") or os.environ.get(config_key.upper(), "")
        if not api_key:
            return _error_response(
                f"No API key configured for {prov['label']}. Set '{config_key}' in config.json or provide api_key in the request.",
                "missing_api_key", 400, request_id,
            )

        # Get base URL: payload > config > provider default
        base_url_key = f"{provider_id}_base_url"
        base_url = payload.get("base_url") or cfg.get(base_url_key) or prov["base_url"]

        # Use payload tools or default Fusion tools
        effective_tools = tools_supplied if tools_supplied is not None else FUSION_TOOLS

        # 纵深防御:当前模型不支持视觉时,剥离 user 消息中的图片附件(防厂商 400)
        if not _model_has_vision(prov, model):
            messages, _stripped = _strip_unsupported_images(messages)

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
            return _error_response(f"{prov['label']} request timed out.", "provider_timeout", 504, request_id, True)
        except requests.exceptions.ConnectionError:
            return _error_response(f"Cannot connect to {prov['label']} API ({base_url})", "provider_connection", 502, request_id, True)
        except ValueError as exc:
            return _error_response(str(exc), "provider_invalid_response", 502, request_id, True)
        except Exception as exc:
            return _error_response(f"Unexpected error calling {prov['label']}: {exc}", "provider_error", 500, request_id)

        if status == 200:
            return jsonify(data)
        else:
            detail = ""
            try:
                detail = json.dumps(data)[:2000]
            except Exception:
                detail = str(data)[:2000]
            return _error_response(
                f"{prov['label']} returned {status}: {detail}",
                "provider_http_error", 502, request_id,
                status in {408, 429, 500, 502, 503, 504},
            )

    # ── /api/models ──
    @app.route("/api/models")
    def api_models():
        cfg = load_config()

        providers_out: dict[str, Any] = {}
        for pid, prov in PROVIDERS.items():
            config_key = prov["config_key"]
            # 视觉能力:未声明 vision_models = 全部模型支持;
            # 空列表 = 全部不支持;有列表 = 仅列表内模型支持
            vision_models = prov.get("vision_models")
            providers_out[pid] = {
                "label": prov["label"],
                "api_format": prov["api_format"],
                "models": prov["models"],
                "base_url": cfg.get(f"{pid}_base_url") or prov["base_url"],
                "configured": bool(cfg.get(config_key) or os.environ.get(config_key.upper())),
                "config_key": config_key,
                "vision_all": vision_models is None,
                "vision_models": vision_models or [],
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

        request_id = _request_id()
        cfg = load_config()
        try:
            updates = request.get_json(silent=False)
        except BadRequest as exc:
            return _error_response(str(exc), "invalid_json", 400, request_id)
        if not isinstance(updates, dict):
            return _error_response("config update must be a JSON object", "invalid_request", 400, request_id)

        allowed_keys: set[str] = {
            "provider", "model", "system_prompt",
        }
        # Add per-provider API key and base_url keys
        for pid in PROVIDERS:
            allowed_keys.add(PROVIDERS[pid]["config_key"])
            allowed_keys.add(f"{pid}_base_url")

        for k, v in updates.items():
            if k in allowed_keys:
                if not isinstance(v, str):
                    return _error_response(f"config field '{k}' must be a string", "invalid_config", 400, request_id)
                if k.endswith("_base_url") and not (v.startswith("http://") or v.startswith("https://")):
                    return _error_response(f"config field '{k}' must be an http(s) URL", "invalid_config", 400, request_id)
                # GET /api/config intentionally returns API keys as ***last4.
                # If the UI saves settings after switching models, that masked
                # value must never replace the real key in config.json.
                if k.endswith("_api_key") and v.startswith(_REDACTED_KEY_PREFIX):
                    continue
                cfg[k] = v
        try:
            save_config(cfg)
        except OSError as exc:
            return _error_response(f"could not save configuration: {exc}", "config_write_failed", 500, request_id)
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
