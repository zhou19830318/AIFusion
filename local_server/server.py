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
import urllib.parse
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
_MAX_MESSAGES = 80          # 服务端最终窗口上限(超出自动截断,见 _trim_messages_to_window)
_MAX_MESSAGE_CHARS = 120_000
_MAX_SCRIPT_CHARS = 120_000
# Per-image base64 budget (~4.5 MB binary): screenshots / attachments are binary
# payloads, not model text context, so they must not consume the text budget.
# 120K chars of base64 ≈ a ~88 KB screenshot — counting it fully would reject
# any vision round.  Kept well below Anthropic's ~5 MB per-image limit.
_MAX_IMAGE_CHARS = 6_000_000
# PDF documents get a larger per-file cap: OpenAI reads them natively (file part,
# 100 MB/page limits) and Anthropic accepts base64 documents up to 32 MB — well
# above our 32 MB request-body cap, so 12M chars (~9 MB binary) is a safe bound.
_MAX_PDF_CHARS = 12_000_000
_CONNECT_TIMEOUT = 10
# 读超时 = "多久收不到任何字节"。非流式请求在整段生成完成前不会回任何数据,因此它
# 实际上等于"单次生成的等待上限"。180s 对思考型模型太紧:DeepSeek V4.1 Flash 默认
# 开启思考模式,推理与正文共享同一个输出上限(默认 64K),长推理很容易超过 180s ——
# 旧行为是一轮里连续 3 次超时(≈9 分钟)后才失败,用户只看到一个空洞的错误。
# 可用 config.json 的 "provider_read_timeout" 覆盖。
_READ_TIMEOUT = 420
_MIN_READ_TIMEOUT = 30
_MAX_READ_TIMEOUT = 3600
_MAX_PROVIDER_RETRIES = 2
_MAX_HISTORY_STATE_BYTES = 8 * 1024 * 1024
# 调试日志:与 AIFusion.py 的 log() 写同一个文件(插件根目录),让厂商调用也能被事后复盘
_DEBUG_LOG_PATH = _DIR.parent / "aifusion_debug.log"
_DEBUG_LOG_MAX_BYTES = 500 * 1024
_DEBUG_LOG_KEEP_BYTES = 200_000
# 思考强度档位(DeepSeek 官方取值;minimal/medium/xhigh 由厂商自行映射)
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
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
        # 2026-09-10 起官方 API 主打模型为 deepseek-flash (V4.1 Flash)，原生多模态：
        # https://api-docs.deepseek.com/zh-cn/news/news260910
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        # deepseek-flash 支持标准 OpenAI image_url 块（base64 data URL / 外部 URL，
        # JPEG/PNG/GIF/WebP，仅 user 消息）；deepseek-v4-pro 仍为纯文本。
        # 旧名 deepseek-v4-flash / deepseek-v4-flash-vision-exp 已下线，由 _MODEL_ALIASES
        # 映射到 deepseek-flash（与官方路由行为一致）。
        "vision_models": ["deepseek-flash"],
        # 思考强度可通过请求的 reasoning_effort 控制(none 关闭思考模式)。
        # 客户端在"推理吃满输出上限导致回复被截断"时会降档重试。
        "supports_reasoning_effort": True,
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
        # GLM-5.3-Flash（2026-08）是 GLM-5 系列首个原生多模态模型：
        # https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash
        # 图片参数：messages[].content[] 中添加 type: image_url 内容块，
        # image_url.url 传图片 URL（推荐）或 Base64 Data URL，可多图。
        # glm-5.3（文本旗舰）仍为纯文本。
        "models": ["glm-5.3-flash", "glm-5.3"],
        "vision_models": ["glm-5.3-flash"],
        # GLM-5.3-Flash 的 thinking.type 仅支持 enabled（思考不可关闭），
        # 客户端在截断恢复时会降档到 none —— 会 400，因此不声明支持。
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
    # ── Zhipu GLM ──
    "glm5.3-flash": "glm-5.3-flash",
    "glm5.3": "glm-5.3",
    # ── DeepSeek：旧模型名已下线，官方将其路由到 V4.1 Flash，这里直接映射到新名 ──
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "v4-flash": "deepseek-flash",
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
    "Exclude cooling/vent holes. "
    "## Port identification rule\n"
    "Never guess or infer port/feature names (USB, BOOT, RESET, mic, LED, ...) from position alone. "
    "Verify each opening against the actual geometry with `read` first; when a port's identity stays ambiguous, ask the user instead of assuming."
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
    # 采样温度:CAD 脚本生成需要稳定、可复现的输出,默认走低温;
    # 设为 "none" 或空则不发送 temperature,跟随提供商默认。
    "temperature": "0.2",
    # 单次厂商调用的读超时(秒):思考型模型长推理可能远超 180s。30–3600,超范围回退默认。
    "provider_read_timeout": _READ_TIMEOUT,
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


def _debug_log(message: str) -> None:
    """Append one line to aifusion_debug.log — best effort, never raises.

    厂商调用以前完全不入日志:一次失败轮次只留下启动/关闭行,事后无法回答"为什么报错"。
    这里把每次尝试、每个响应的 finish_reason/usage/截断标记写进同一个日志文件。
    轮转方式与 AIFusion.py 的 log() 一致:过大时保留尾部 ~200KB,而不是把整个文件清掉
    —— 否则用户失败后去查日志,看到的恰好是空文件。
    """
    try:
        size = _DEBUG_LOG_PATH.stat().st_size if _DEBUG_LOG_PATH.exists() else 0
        if size > _DEBUG_LOG_MAX_BYTES:
            # 二进制读取尾部:文本模式不允许从任意偏移 seek
            with _DEBUG_LOG_PATH.open("rb") as handle:
                handle.seek(-_DEBUG_LOG_KEEP_BYTES, os.SEEK_END)
                tail = handle.read().decode("utf-8", errors="replace")
            with _DEBUG_LOG_PATH.open("w", encoding="utf-8") as handle:
                handle.write(f"[ROTATED at {time.strftime('%Y-%m-%d %H:%M:%S')}] — trimmed to last ~200 KB\n")
                handle.write(tail)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] [provider] {message}\n")
    except OSError:
        pass


def _resolve_read_timeout(raw: Any) -> int:
    """config.json 的 provider_read_timeout → 秒;非法/越界值回退默认。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _READ_TIMEOUT
    return value if _MIN_READ_TIMEOUT <= value <= _MAX_READ_TIMEOUT else _READ_TIMEOUT


def _summarize_provider_response(status: int, data: Any) -> str:
    """Compact, log-safe one-liner about a provider response.

    重点是把"HTTP 200 但是空消息"这种致命情况显式标出来 —— 思考型模型推理吃满
    输出上限时正是这个形状(content="" / tool_calls=null / finish_reason=length),
    客户端旧代码只会报一句无信息的 "Empty response"。
    """
    if not isinstance(data, dict):
        return f"status={status} non_object_body={str(data)[:120]}"
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    reasoning = message.get("reasoning_content")
    flags = []
    if choice.get("finish_reason") == "length":
        flags.append("TRUNCATED")
    if not content and not tool_calls:
        flags.append("EMPTY_MESSAGE")
    if data.get("error"):
        flags.append(f"error={str(data['error'])[:160]}")
    return " ".join([
        f"status={status}",
        f"finish_reason={choice.get('finish_reason')}",
        f"content_chars={len(content) if isinstance(content, str) else 0}",
        f"tool_calls={len(tool_calls) if isinstance(tool_calls, list) else 0}",
        f"reasoning_chars={len(reasoning) if isinstance(reasoning, str) else 0}",
        f"prompt_tokens={usage.get('prompt_tokens')}",
        f"completion_tokens={usage.get('completion_tokens')}",
        f"reasoning_tokens={details.get('reasoning_tokens')}",
    ] + flags)


def _error_response(message: str, code: str, status: int, request_id: str, retryable: bool = False):
    return jsonify({
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
        }
    }), status


def _approx_message_chars(msg: dict[str, Any]) -> int:
    """Rough text-size estimate for window trimming (images are binary and
    counted nominally, mirroring _validate_chat_payload's accounting)."""
    content = msg.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = ((part.get("image_url") or {}).get("url")) or ""
                total += 512 if url.startswith("data:") else len(url)
            else:
                total += len(json.dumps(part, ensure_ascii=False))
        return total
    return 0


def _trim_messages_to_window(
    messages: list[dict[str, Any]], max_count: int, max_chars: int
) -> list[dict[str, Any]]:
    """Keep the newest window that fits both caps, never breaking tool pairing.

    Long design sessions append an assistant + tool message pair per tool
    round, so history only grows. Hard-rejecting an oversized list would
    permanently wedge the session (the client cannot shrink already-sent
    history), so instead drop the OLDEST complete groups and keep:
      - all system messages (merged CAD guidance must survive)
      - the original user request (the task statement)
      - the newest window of messages, kept in whole
        assistant.tool_calls + tool-responses groups, so vendors never receive
        orphan tool messages (which they reject with a 400).
    """
    if len(messages) <= max_count and sum(_approx_message_chars(m) for m in messages) <= max_chars:
        return messages

    owner_of_call: dict[str, int] = {}
    tool_of_call: dict[str, int] = {}
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                tc_id = str((tc or {}).get("id") or "")
                if tc_id:
                    owner_of_call[tc_id] = i
        elif role == "tool":
            tc_id = str(m.get("tool_call_id") or "")
            if tc_id:
                tool_of_call[tc_id] = i

    keep: set[int] = set()
    count = 0
    chars = 0
    for i in range(len(messages) - 1, -1, -1):
        if count >= max_count or chars >= max_chars:
            break
        m = messages[i]
        role = m.get("role")
        if role == "system":
            keep.add(i)  # system 提示随行,不占用窗口预算
            continue
        group = [i]
        if role == "tool":
            owner = owner_of_call.get(str(m.get("tool_call_id") or ""))
            if owner is not None:
                group.append(owner)
        elif role == "assistant" and isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                resp = tool_of_call.get(str((tc or {}).get("id") or ""))
                if resp is not None:
                    group.append(resp)
        fresh = [g for g in group if g not in keep]
        if count + len(fresh) > max_count:
            break  # 窗口对齐到完整的一组:宁可少带也不拆散 tool 配对
        group_chars = sum(_approx_message_chars(messages[g]) for g in fresh)
        if chars + group_chars > max_chars and count > 0:
            break
        for g in fresh:
            keep.add(g)
        count += len(fresh)
        chars += group_chars

    # 最初的用户请求必须保留,否则模型只剩工具片段,不知道任务是什么
    first_real = next((i for i, m in enumerate(messages) if m.get("role") != "system"), None)
    if first_real is not None and first_real not in keep and messages[first_real].get("role") == "user":
        keep.add(first_real)

    return [messages[i] for i in sorted(keep)]


def _validate_chat_payload(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    messages = payload.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    if len(messages) > _MAX_MESSAGES or sum(_approx_message_chars(m) for m in messages) > _MAX_MESSAGE_CHARS:
        # 长设计会话的每轮工具调用都会追加 assistant+tool 两条消息,历史只增不减,
        # 硬拒绝会让会话永久卡死。这里按 "保留 system + 最初用户请求 + 最新窗口"
        # 优雅截断,并保证 tool_call/tool 消息成对保留;单条超限仍报错(无法靠截断修复)。
        messages = _trim_messages_to_window(messages, _MAX_MESSAGES, _MAX_MESSAGE_CHARS)
    total_chars = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("each message must be an object with a valid role")
        if msg["role"] == "tool" and not str(msg.get("tool_call_id") or "").strip():
            # Anthropic rejects the whole request without it; failing early here
            # yields a specific, recoverable error instead of killing the repair loop.
            raise ValueError("tool messages must carry the tool_call_id of the assistant tool call they answer")
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = ((part.get("image_url") or {}).get("url")) or ""
                    if url.startswith("data:"):
                        # PDFs get a larger per-file cap than images: OpenAI reads
                        # them natively (≤100 pages/file) and Anthropic accepts
                        # base64 documents up to 32 MB; 12M chars ≈ 9 MB binary.
                        _is_pdf = url.startswith("data:application/pdf")
                        _limit = _MAX_PDF_CHARS if _is_pdf else _MAX_IMAGE_CHARS
                        if len(url) > _limit:
                            raise ValueError(
                                f"{'PDF' if _is_pdf else 'image'} attachment exceeds {_limit} characters; "
                                "attach a smaller file or split it"
                            )
                        # base64 image data is binary; charge a nominal text budget
                        total_chars += 512
                    else:
                        total_chars += len(url)
                else:
                    total_chars += len(json.dumps(part, ensure_ascii=False))
        elif content is not None:
            raise ValueError("message content must be text, multimodal parts, or null")
    if total_chars > _MAX_MESSAGE_CHARS:
        raise ValueError(f"message content exceeds {_MAX_MESSAGE_CHARS} characters")
    # Runaway-script guardrail (_MAX_SCRIPT_CHARS): an enormous `execute` script
    # argument would otherwise crowd out the system prompt's influence in later
    # repair rounds with no bound at all.
    for msg in messages:
        if msg.get("role") != "assistant" or not isinstance(msg.get("tool_calls"), list):
            continue
        for tc in msg["tool_calls"]:
            try:
                if tc.get("function", {}).get("name") != "execute":
                    continue
                args = json.loads(tc.get("function", {}).get("arguments") or "{}")
            except (json.JSONDecodeError, AttributeError):
                continue
            script = args.get("script") if isinstance(args, dict) else None
            if isinstance(script, str) and len(script) > _MAX_SCRIPT_CHARS:
                raise ValueError(
                    f"execute script exceeds {_MAX_SCRIPT_CHARS} characters; split the build into smaller steps"
                )
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
                          retry_statuses: set[int] | None = None, read_timeout: int | None = None,
                          label: str = "") -> tuple[int, dict[str, Any]]:
    retry_statuses = retry_statuses or {408, 429, 500, 502, 503, 504}
    timeout = read_timeout or _READ_TIMEOUT
    last_status = 502
    last_data: dict[str, Any] = {}
    for attempt in range(_MAX_PROVIDER_RETRIES + 1):
        started = time.time()
        try:
            resp = requests.post(
                endpoint, json=json_body, headers=headers,
                timeout=(_CONNECT_TIMEOUT, timeout),
            )
            last_status = resp.status_code
            last_data = _response_json(resp)
            _debug_log(f"{label} attempt {attempt + 1} -> {_summarize_provider_response(resp.status_code, last_data)} elapsed={time.time() - started:.1f}s")
            if resp.status_code not in retry_statuses or attempt >= _MAX_PROVIDER_RETRIES:
                return last_status, last_data
            time.sleep(min(2 ** attempt, 4))
        except requests.exceptions.Timeout:
            # 超时 = 这一次生成本身太慢(思考型模型常见)。重试同样的请求只会让用户再等
            # 一个完整超时,几乎不可能更快出结果,而旧行为会把它放大成 3×180s ≈ 9 分钟
            # 的"假死"。直接放弃,交由调用方给出可操作的错误。HTTP 429/5xx 仍然重试。
            _debug_log(f"{label} attempt {attempt + 1} -> TIMEOUT（{time.time() - started:.1f}s / 上限 {timeout}s，不再重试）")
            raise
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


def _host_of(url: str) -> str:
    """Return the lowercase hostname of a URL, or '' if unparsable."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_PDF_NO_INLINE_SUPPORT_NOTICE = (
    "[PDF attachment{name} was not sent: {label}'s API does not accept inline PDF "
    "documents. Convert it to images (PNG/JPEG) and attach those instead.]"
)


def _adapt_attachments_for_openai_provider(provider_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-provider adaptation of PDF attachments on OpenAI-format endpoints.

    OpenAI's /v1/chat/completions natively reads PDFs via a `file` content part
    (base64 data URL in file.file_data, ≤100 pages/file).  The other
    OpenAI-compatible endpoints we route to (Kimi, Zhipu GLM, Gemini,
    DeepSeek) document only `image_url` image parts — an inline PDF would
    either be rejected or silently ignored, so it is replaced by a notice.
    """
    def convert(provider_id: str, part: Any) -> Any:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            return part
        url = ((part.get("image_url") or {}).get("url")) or ""
        if not url.startswith("data:application/pdf"):
            return part
        if provider_id == "openai":
            file_part: dict[str, Any] = {"type": "file", "file": {"file_data": url}}
            filename = part.get("_filename")
            if filename:
                file_part["file"]["filename"] = str(filename)
            return file_part
        label = PROVIDERS[provider_id]["label"]
        detail = f" ({part.get('_filename')})" if part.get("_filename") else ""
        return {"type": "text", "text": _PDF_NO_INLINE_SUPPORT_NOTICE.format(label=label, name=detail)}

    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            out.append({**m, "content": [convert(provider_id, p) for p in m["content"]]})
        else:
            out.append(m)
    return out


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
                    parts.append({"type": "text", "text": "[图片附件已自动忽略:当前模型不支持图片输入。如需查看附件,请切换到支持视觉的模型(deepseek-flash / GLM-5.3-Flash / Kimi K3 / GPT-5.6 / Claude Sonnet 5 / Gemini 3.7 Flash)]"})
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

# Media types Anthropic accepts for base64 `image` blocks (per their vision docs:
# image/jpeg, image/png, image/gif, image/webp — ≤5 MB per image, ≤8000 px long edge).
_ANTHROPIC_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}
# Replaces attachments the target provider cannot ingest, instead of failing the
# whole request with a 400 and killing the repair loop.
_UNSUPPORTED_ATTACHMENT_NOTICE = (
    "[Attachment not sent: the current provider/model does not accept this file type "
    "({media_type}). Convert it to PNG/JPEG and attach it again.]"
)


def _anthropic_data_url_to_block(url: str) -> dict[str, Any]:
    """Convert a data: URL attachment into an Anthropic content block.

    - application/pdf → `document` block (Anthropic PDF support: ≤32 MB, ≤100 pages)
    - jpeg/png/gif/webp → `image` block, with the media_type normalized to the
      documented set (Anthropic rejects unknown types such as image/bmp or image/heic)
    - anything else → a text notice so the request still goes through
    """
    meta, _, b64data = url.partition(",")
    media_type = meta[5:].split(";")[0].strip().lower() or "image/png"
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64data}}
    if media_type in _ANTHROPIC_IMAGE_MEDIA_TYPES:
        if media_type == "image/jpg":  # canonical MIME is image/jpeg
            media_type = "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64data}}
    return {"type": "text", "text": _UNSUPPORTED_ATTACHMENT_NOTICE.format(media_type=media_type)}


def _as_anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize message content to Anthropic content blocks for merging."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return content
    return []


def _merge_adjacent_same_role(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role messages into one.

    The Anthropic Messages API requires alternating user/assistant roles.
    Multi-tool-call turns produce consecutive tool messages (each mapped to a
    user message), and the UI may also emit adjacent user turns — both are
    rejected by the API as "roles must alternate".
    """
    merged: list[dict[str, Any]] = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            prev = merged[-1]
            prev["content"] = _as_anthropic_blocks(prev["content"]) + _as_anthropic_blocks(m["content"])
        else:
            merged.append(dict(m))
    return merged


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
                            blocks.append(_anthropic_data_url_to_block(url))
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

    return _merge_adjacent_same_role(anthropic_msgs), system_content


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

    # Map Anthropic stop_reason → OpenAI finish_reason honestly so callers can
    # detect truncated responses ("length") instead of treating them as done.
    if tool_calls:
        finish_reason = "tool_calls"
    elif anthropic_resp.get("stop_reason") == "max_tokens":
        finish_reason = "length"
    else:
        finish_reason = "stop"
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
    temperature: float | None = 0.2,
    timeout: int = 180,
    reasoning_effort: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call an OpenAI-compatible /chat/completions endpoint."""
    body: dict[str, Any] = {"model": model, "messages": messages}
    # Reasoning-style models reject "temperature"; callers may pass None to
    # fall back to the provider's own default instead.
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    # 思考强度透传(DeepSeek: none 关闭思考模式)。只在调用方显式传入时发送,
    # 否则完全跟随厂商默认,避免给不认识该字段的厂商发多余参数。
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    endpoint = f"{base_url.rstrip('/')}/chat/completions"

    return _post_json_with_retry(endpoint, json_body=body, headers=headers,
                                 read_timeout=timeout, label=f"{model} openai")


def _anthropic_call(
    base_url: str,
    api_key: str,
    api_version: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float | None = 0.2,
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
    if temperature is not None:
        body["temperature"] = temperature
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

    status, data = _post_json_with_retry(endpoint, json_body=body, headers=headers,
                                         read_timeout=timeout, label=f"{model} anthropic")
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

    @app.route("/api/sessions/<session_id>/history", methods=["DELETE"])
    def api_clear_session_history(session_id: str):
        """Permanently delete this session's history (events + snapshot).

        Backs the palette's clear button: clearing only client state left the
        snapshot intact, so reopening the palette resurrected the conversation.
        """
        request_id = _request_id()
        try:
            removed = history.clear_session_history(session_id)
        except KeyError:
            return _error_response("session not found", "not_found", 404, request_id)
        return jsonify({"ok": True, "removed_events": removed})

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

        # Inject or merge the server-side CAD guidance.  It must reach the model
        # even when the client supplies its own system message (e.g. a UI
        # placeholder), so merge into it instead of gating on presence.
        _has_system = False
        for m in messages:
            if m.get("role") == "system":
                _has_system = True
                content = m.get("content", "")
                if isinstance(content, str) and "SketchTexts.createInput(text, position)" not in content:
                    m["content"] = content + "\n\n" + _CAD_TEXT_API_GUIDANCE
                break
        if not _has_system:
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

        # Security: never send the stored API key to an arbitrary host chosen by
        # a web page.  A per-request base_url is honored only when it points at a
        # loopback address (local gateways/proxies) or at the same host as the
        # provider's configured/default endpoint.
        if payload.get("base_url"):
            allowed_hosts = {_host_of(prov["base_url"]), _host_of(cfg.get(base_url_key) or "")}
            host = _host_of(payload["base_url"])
            if host and host not in allowed_hosts and host not in {"127.0.0.1", "localhost", "::1"}:
                return _error_response(
                    "base_url must point to the provider's configured endpoint or a loopback address",
                    "base_url_not_allowed", 400, request_id,
                )

        # Use payload tools or default Fusion tools
        effective_tools = tools_supplied if tools_supplied is not None else FUSION_TOOLS

        # 纵深防御:当前模型不支持视觉时,剥离 user 消息中的图片附件(防厂商 400)
        if not _model_has_vision(prov, model):
            messages, _stripped = _strip_unsupported_images(messages)

        # Per-provider attachment adaptation (researched against each vendor's
        # API docs): PDFs ride the native `file` part on OpenAI; other
        # OpenAI-compatible endpoints only accept image_url parts, so PDFs
        # become a notice there. Anthropic conversion handles its own rules.
        if api_format == "openai":
            messages = _adapt_attachments_for_openai_provider(provider_id, messages)

        # Sampling temperature: deterministic CAD script generation benefits from
        # low temperature.  Resolution: payload > config ("0.2" default);
        # "none"/empty/invalid falls back to the provider's own default.
        raw_temperature = payload.get("temperature", cfg.get("temperature", ""))
        try:
            temperature = float(raw_temperature)
            if not 0.0 <= temperature <= 2.0:
                raise ValueError
        except (TypeError, ValueError):
            temperature = None

        # 单次厂商调用的等待上限:思考型模型长推理会远超旧的 180s 固定值。
        read_timeout = _resolve_read_timeout(cfg.get("provider_read_timeout"))

        # 思考强度:仅在客户端显式传入、且厂商声明支持时透传(客户端在"回复被截断"
        # 后会降档重试)。不支持的厂商直接忽略该字段,而不是 400 —— 否则恢复路径会在
        # 其他提供商上反而把会话弄坏。
        reasoning_effort = payload.get("reasoning_effort")
        if reasoning_effort is not None:
            if not isinstance(reasoning_effort, str) or reasoning_effort not in _REASONING_EFFORTS:
                return _error_response(
                    f"reasoning_effort must be one of {sorted(_REASONING_EFFORTS)}",
                    "invalid_reasoning_effort", 400, request_id,
                )
            if not prov.get("supports_reasoning_effort"):
                reasoning_effort = None

        try:
            if api_format == "openai":
                status, data = _openai_compatible_call(
                    base_url=base_url, api_key=api_key, model=model,
                    messages=messages, tools=effective_tools,
                    tool_choice=tool_choice, temperature=temperature,
                    timeout=read_timeout, reasoning_effort=reasoning_effort,
                )
            elif api_format == "anthropic":
                status, data = _anthropic_call(
                    base_url=base_url, api_key=api_key,
                    api_version=prov.get("api_version", "2023-06-01"),
                    model=model, messages=messages, tools=effective_tools,
                    temperature=temperature, timeout=read_timeout,
                )
            else:
                return jsonify({"error": f"Unsupported API format: {api_format}"}), 500

        except requests.exceptions.Timeout:
            _debug_log(f"chat rid={request_id} provider={provider_id} model={model} TIMEOUT after {read_timeout}s")
            return _error_response(
                f"{prov['label']} 在 {read_timeout}s 内没有返回结果（读超时）。"
                "思考型模型（如 deepseek-flash）的长推理可能超过这个上限；"
                "可在 config.json 调大 provider_read_timeout，或把任务拆小、清空会话重开。",
                "provider_timeout", 504, request_id, True,
            )
        except requests.exceptions.ConnectionError:
            _debug_log(f"chat rid={request_id} provider={provider_id} CONNECTION_ERROR {base_url}")
            return _error_response(f"Cannot connect to {prov['label']} API ({base_url})", "provider_connection", 502, request_id, True)
        except ValueError as exc:
            _debug_log(f"chat rid={request_id} provider={provider_id} INVALID_RESPONSE {exc}")
            return _error_response(str(exc), "provider_invalid_response", 502, request_id, True)
        except Exception as exc:
            _debug_log(f"chat rid={request_id} provider={provider_id} ERROR {type(exc).__name__}: {exc}")
            return _error_response(f"Unexpected error calling {prov['label']}: {exc}", "provider_error", 500, request_id)

        _debug_log(
            f"chat rid={request_id} provider={provider_id} model={model} messages={len(messages)} "
            f"reasoning_effort={reasoning_effort or '-'} {_summarize_provider_response(status, data)}"
        )

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
                # 客户端据此决定能否在"回复被截断"后用 reasoning_effort 降档重试
                "supports_reasoning_effort": bool(prov.get("supports_reasoning_effort")),
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
            "provider_read_timeout",
            "provider", "model", "system_prompt", "temperature",
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
    # Only loopback origins may call the local API.  Notably "null" (file://
    # pages, sandboxed iframes) is rejected: a downloaded HTML file opened
    # locally would otherwise be able to spend the user's API keys.
    import re as _re
    _LOOPBACK_ORIGIN = _re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$")

    @app.after_request
    def add_cors_headers(resp):
        origin = request.headers.get("Origin", "")
        if origin and _LOOPBACK_ORIGIN.match(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-AIFusion-Token"
            # DELETE is required by the palette's clear button
            # (DELETE /api/sessions/<id>/history).
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
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
