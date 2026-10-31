"""Jev as MCP tools for Claude Code, Codex CLI, Claude Desktop and other MCP clients (stdio)."""
from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from .config import endpoint

# 不同 MCP 客户端在 initialize 时声明的名字，用来在用量里区分来源
CLIENT_LABELS = (
    ("codebuddy", "CodeBuddy"),
    ("claude", "Claude Code"),
    ("codex", "Codex"),
    ("cursor", "Cursor"),
)

# 用量记录：与仓库里的 jev_usage.py 共用同一份中文列定义，便于合并统计
USAGE_COLUMNS = [
    "时间", "来源", "模型", "预设", "状态码", "输入token", "输出token",
    "延迟毫秒", "问题数", "文本字符数", "请求ID", "备注",
]


def _usage_csv() -> Path:
    """MCP 调用单独记一个文件，避免和命令行调用抢写同一个 CSV。"""
    override = (os.environ.get("JEV_USAGE_CSV") or "").strip()
    if override:
        return Path(override)
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "jev-netlify-mcp" / "usage_mcp.csv"


def _client_label(ctx: Any) -> str:
    """从 MCP 会话的 initialize 参数里取客户端名字，映射成可读来源。"""
    name = ""
    try:
        params = ctx.request_context.session.client_params
        info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
        name = (getattr(info, "name", "") or "").strip()
    except Exception:  # noqa: BLE001 - 拿不到就退回 MCP
        pass
    low = name.lower()
    for key, label in CLIENT_LABELS:
        if key in low:
            return label
    return name or "MCP"


def _detect_lang(state: Any) -> str:
    """粗判 state 的语言，只用于记录，不改变调用行为。"""
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    cjk = kana = hangul = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
        elif 0x3040 <= o <= 0x30FF:
            kana += 1
        elif 0xAC00 <= o <= 0xD7AF:
            hangul += 1
    base = max(len(text.replace(" ", "").replace("\n", "")), 1)
    if kana / base > 0.05:
        return "ja"
    if hangul / base > 0.05:
        return "ko"
    ratio = (cjk + kana + hangul) / base
    if ratio > 0.5:
        return "zh"
    if ratio > 0.05:
        return "mixed"
    return "en"


def _log_usage(body: dict, status: int, latency_ms: float, request_id: str,
               source: str = "MCP") -> None:
    """尽力记录，失败绝不影响工具调用本身。"""
    if (os.environ.get("JEV_MCP_USAGE_LOG") or "1").strip() in ("0", "false", "False"):
        return
    try:
        path = _usage_csv()
        path.parent.mkdir(parents=True, exist_ok=True)
        usage = body.get("usage") or {}
        questions = body.get("_questions") or {}
        state = body.get("_state")
        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        row = [
            datetime.now().isoformat(timespec="seconds"),
            source,
            body.get("model") or "",
            "",
            status,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            round(latency_ms),
            len(questions) if isinstance(questions, dict) else 0,
            len(state_text or ""),
            request_id,
            f"lang={_detect_lang(state)}" if state is not None else "",
        ]
        new_file = not path.is_file()
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(USAGE_COLUMNS)
            writer.writerow(row)
    except Exception:  # noqa: BLE001 - 记录失败不能影响主流程
        pass

INSTRUCTIONS = """Jev (TypeSafe) is a System One decision model: it does NOT generate text. Give it a text
"state" plus typed questions and it returns calibrated probabilities. Use it for classification,
routing, scoring, yes/no checks and guardrails, especially over many items.
Rules:
- Jev works best in English. If the user writes in another language, translate the state faithfully
  to English (keep numbers, dates, names, urgency and threats), write the questions in English and
  report the answer back in the user's language. Show the English text you sent so the user can check it.
- Keep questions atomic: one idea per question. Split compound questions.
- noul: a yes/no statement; the answer is the probability that it is true.
- choice: pick one option; criteria is {option_name: description}; use meaningful snake_case names.
- score: ordered levels from low to high; criteria is a list of level descriptions (max 10).
- Put many questions about the same text into ONE call; call once per text when processing many texts.
- confidence < 0.5 (choice/score) or noul between 0.35 and 0.65 means the model is unsure: say so.
- State and questions share a budget of roughly 32k tokens per call."""

mcp = MCPServer(name="jev", instructions=INSTRUCTIONS)


def _request(path: str, body: dict | None = None, timeout: float = 120,
             log_state: Any = None, log_questions: Any = None,
             log_source: str = "MCP") -> dict:
    # ToolError messages reach the client; other exceptions are hidden by the MCP SDK.
    try:
        base, key = endpoint()
    except RuntimeError as e:
        raise ToolError(str(e)) from None
    req = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "jev-netlify-mcp",
        },
        method="GET" if body is None else "POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if body is not None:
                _log_usage(
                    {**data, "_state": log_state, "_questions": log_questions},
                    resp.status,
                    (time.perf_counter() - started) * 1000,
                    resp.headers.get("x-typesafe-request-id", "") or "",
                    source=log_source,
                )
            return data
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        if body is not None:
            _log_usage({}, e.code, (time.perf_counter() - started) * 1000, "", source=log_source)
        hint = {401: " (wrong key, or the proxy was deployed with a different key hash)",
                422: " (question format is invalid)",
                429: " (rate or credit limit reached, wait and retry)"}.get(e.code, "")
        raise ToolError(f"Jev API error {e.code}{hint}: {detail}") from None
    except urllib.error.URLError as e:
        if body is not None:
            _log_usage({}, 0, (time.perf_counter() - started) * 1000, "", source=log_source)
        raise ToolError(f"Cannot reach {base}: {e.reason}") from None


@mcp.tool(
    name="jev_evaluate",
    description=(
        "Evaluate one text (state) against typed questions with Jev and return calibrated answers. "
        "state: string or JSON object (English works best). "
        "questions: {name: {type: 'noul'|'choice'|'score', instructions: str, criteria?: ...}}. "
        "noul criteria optional {true, false}; choice criteria {option: description}; "
        "score criteria [level0, level1, ...] from low to high. model defaults to jev-latest."
    ),
)
def jev_evaluate(state: Any, questions: dict[str, Any], model: str = "jev-latest",
                 ctx: Context | None = None) -> dict:
    # ctx 由 MCP SDK 注入（不会出现在工具的入参 schema 里），用来识别是哪个客户端在调
    return _request(
        "/v1/systemone",
        {"state": state, "questions": questions, "model": model},
        log_state=state,
        log_questions=questions,
        log_source=_client_label(ctx) if ctx is not None else "命令行",
    )


@mcp.tool(name="jev_models", description="List the Jev models and aliases available on this endpoint.")
def jev_models() -> dict:
    return _request("/v1/models")


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
