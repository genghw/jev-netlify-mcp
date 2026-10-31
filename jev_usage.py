"""Jev 代理调用量的记录与统计。

    python jev_usage.py call                      # 调一次并记录
    python jev_usage.py call --preset ticket      # 用不同的问题集
    python jev_usage.py stats                     # 看累计统计
    python jev_usage.py stats --since 2026-10-31  # 只看某天起
    python jev_usage.py stats --deployments 2     # 顺带把每次 15 credits 的部署成本算进来
    python jev_usage.py log --last 10             # 看最近 10 条明细

数据落在脚本同目录的 data/usage.csv，一行一次调用，可以直接丢 Excel。
地址与密钥读取顺序：环境变量 -> Windows 注册表 -> %APPDATA%\\jev-netlify-mcp\\config.env
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "usage.csv"

# 列定义：程序内部用英文键，CSV 表头用中文，方便直接看
COLUMNS = [
    ("timestamp", "时间"),
    ("source", "来源"),
    ("model", "模型"),
    ("preset", "预设"),
    ("status", "状态码"),
    ("input_tokens", "输入token"),
    ("output_tokens", "输出token"),
    ("latency_ms", "延迟毫秒"),
    ("questions", "问题数"),
    ("state_chars", "文本字符数"),
    ("request_id", "请求ID"),
    ("note", "备注"),
]
EN2CN = {en: cn for en, cn in COLUMNS}
CN2EN = {cn: en for en, cn in COLUMNS}
FIELDS = [en for en, _ in COLUMNS]

# 预设名也翻成中文，CSV 里更直观
PRESET_LABELS = {"short": "简短", "ticket": "工单", "long": "长文"}

URL_NAMES = ("JEV_PROXY_URL", "TYPESAFE_BASE_URL")
KEY_NAMES = ("JEV_PROXY_KEY", "TYPESAFE_API_KEY")

# 文档给的换算：18,000 input tokens ≈ 0.11 credits，输出不计费
TOKENS_PER_CREDIT = 18_000 / 0.11
DEPLOY_CREDITS = 15

PRESETS = {
    "ticket": {
        "state": "用户反馈：点击支付后页面白屏，已经尝试了两个浏览器。",
        "questions": {
            "is_bug": {"type": "noul", "instructions": "这是否是一个软件缺陷报告？"},
            "team": {
                "type": "choice",
                "instructions": "应该由哪个团队处理？",
                "criteria": {"payments": "支付、账单、退款相关", "frontend": "页面渲染、浏览器兼容性问题"},
            },
            "urgency": {
                "type": "score",
                "instructions": "紧急程度",
                "criteria": ["下个版本再说", "本周内修", "正在阻塞收入"],
            },
        },
    },
    "short": {
        "state": "账单金额翻倍了，请退款。",
        "questions": {"refund": {"type": "noul", "instructions": "用户是否要求退款？"}},
    },
    "long": {
        "state": (
            "上周三开始，我们企业账号下的三个子账号在批量导出报表时都会卡在 90% 然后报错 "
            "ERR_EXPORT_TIMEOUT。运维让我们清缓存、换浏览器、换网络都试过了，问题依旧。"
            "今早财务催月度报表，已经影响到结账流程。我们买的是企业版，合同里写了 4 小时响应，"
            "现在过去两天了，希望给个明确的时间点，以及是否需要我们提供日志包。"
        ),
        "questions": {
            "is_bug": {"type": "noul", "instructions": "用户反馈的是不是产品缺陷？"},
            "urgency": {
                "type": "score",
                "instructions": "紧急程度",
                "criteria": ["可以等", "本周内", "阻塞业务，立即处理"],
            },
        },
    },
}


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
def _config_file() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "jev-netlify-mcp" / "config.env"


def _from_registry(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip() or None
    except Exception:  # noqa: BLE001
        return None


def _from_file(name: str) -> str | None:
    path = _config_file()
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip()
    return None


def lookup(names: tuple[str, ...]) -> str | None:
    for n in names:
        if (os.environ.get(n) or "").strip():
            return os.environ[n].strip()
    for n in names:
        if _from_registry(n):
            return _from_registry(n)
    for n in names:
        if _from_file(n):
            return _from_file(n)
    return None


# --------------------------------------------------------------------------- #
# 调用与记录
# --------------------------------------------------------------------------- #
def call_proxy(url: str, key: str, payload: dict) -> tuple[int, dict, float, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/v1/systemone",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, data, (time.perf_counter() - started) * 1000, resp.headers.get("x-typesafe-request-id", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw[:300]}
        return exc.code, data, (time.perf_counter() - started) * 1000, ""
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}, (time.perf_counter() - started) * 1000, ""


def mcp_usage_csv() -> Path:
    """MCP Server 记录的用量文件（由 server.py 写入）。"""
    override = (os.environ.get("JEV_USAGE_CSV") or "").strip()
    if override:
        return Path(override)
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "jev-netlify-mcp" / "usage_mcp.csv"


def _normalize(path: Path, default_source: str) -> None:
    """统一表头为中文、补齐缺失列（如新增的「来源」）、翻译历史预设名。"""
    if not path.is_file():
        return
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return

    wanted = [cn for _, cn in COLUMNS]
    old_header = [c.strip() for c in rows[0]]
    new_header = [EN2CN.get(h, h) for h in old_header]
    changed = new_header != old_header

    rebuilt = []
    for raw in rows[1:]:
        record = {name: (raw[i] if i < len(raw) else "") for i, name in enumerate(new_header)}
        if record.get(EN2CN["preset"]) in PRESET_LABELS:
            record[EN2CN["preset"]] = PRESET_LABELS[record[EN2CN["preset"]]]
            changed = True
        if not record.get(EN2CN["source"]):
            record[EN2CN["source"]] = default_source
            changed = True
        rebuilt.append([record.get(col, "") for col in wanted])

    if new_header == wanted and not changed:
        return
    try:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(wanted)
            writer.writerows(rebuilt)
    except PermissionError:
        # 文件被 Excel/编辑器占用时跳过迁移，不影响读取
        pass


def append_row(row: dict) -> None:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    if DATA.is_file():
        _normalize(DATA, "命令行")
    else:
        with DATA.open("w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([cn for _, cn in COLUMNS])
    row = {"source": "命令行", **row}
    try:
        with DATA.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([row.get(en, "") for en, _ in COLUMNS])
    except PermissionError:
        print(f"[警告] {DATA.name} 正被其他程序占用（Excel/编辑器？），本条用量未写入。", file=sys.stderr)


def cmd_call(args: argparse.Namespace) -> int:
    url = (args.url or lookup(URL_NAMES) or "").rstrip("/")
    key = lookup(KEY_NAMES)
    if not url or not key:
        print("缺少站点地址或密钥，先跑 python scripts/setup.py set-url / new-key", file=sys.stderr)
        return 2

    preset = PRESETS.get(args.preset)
    if preset is None:
        print(f"未知 preset：{args.preset}，可选：{', '.join(PRESETS)}", file=sys.stderr)
        return 2

    payload = {"model": args.model, "state": preset["state"], "questions": preset["questions"]}
    status, body, latency_ms, request_id = call_proxy(url, key, payload)
    usage = body.get("usage") or {}

    print(f"POST {url}/v1/systemone  ->  HTTP {status}  ({latency_ms:.0f} ms)  preset={args.preset}")
    if status == 200:
        print(f"model: {body.get('model')}")
        for name, item in (body.get("answers") or {}).items():
            print(f"  {name}: {json.dumps(item, ensure_ascii=False)}")
    else:
        print(json.dumps(body, ensure_ascii=False)[:400])

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "model": body.get("model") or args.model,
        "preset": PRESET_LABELS.get(args.preset, args.preset),
        "status": status,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": round(latency_ms),
        "questions": len(preset["questions"]),
        "state_chars": len(preset["state"]),
        "request_id": request_id,
        "note": args.note or "",
    }
    append_row(row)
    print(f"\n已记录到 {DATA}")
    print(f"  输入 {row['input_tokens']} tokens / 输出 {row['output_tokens']} tokens"
          f" ≈ {row['input_tokens'] / TOKENS_PER_CREDIT:.5f} credits")
    return 0


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def load_rows(since: str | None) -> list[dict]:
    """合并两个来源：命令行的 data/usage.csv + MCP 的 usage_mcp.csv。"""
    rows: list[dict] = []
    sources = [(DATA, "命令行")]
    mcp = mcp_usage_csv()
    if mcp.resolve() != DATA.resolve():
        sources.append((mcp, "MCP"))

    for path, default_source in sources:
        if not path.is_file():
            continue
        _normalize(path, default_source)
        with path.open(newline="", encoding="utf-8-sig") as f:
            for raw in csv.DictReader(f):
                # 表头可能是中文（新）也可能是英文（旧），统一映射回内部键
                row: dict = {}
                for k, v in raw.items():
                    key = CN2EN.get((k or "").strip(), (k or "").strip())
                    row[key] = v
                if since and (row.get("timestamp") or "") < since:
                    continue
                for k in ("status", "input_tokens", "output_tokens", "latency_ms", "questions", "state_chars"):
                    try:
                        row[k] = int(float(row.get(k) or 0))
                    except ValueError:
                        row[k] = 0
                row["source"] = row.get("source") or default_source
                rows.append(row)

    rows.sort(key=lambda r: r.get("timestamp") or "")
    return rows


def pct(values: list[float], q: float) -> float:
    """线性插值分位数，q 取 0~1。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    if low == high:
        return float(ordered[int(pos)])
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def bar(value: float, total: float, width: int = 24) -> str:
    if total <= 0:
        return ""
    return "█" * max(1, round(value / total * width))


def cmd_stats(args: argparse.Namespace) -> int:
    rows = load_rows(args.since)
    if not rows:
        print(f"还没有记录（{DATA}）。先跑 python jev_usage.py call")
        return 1

    ok = [r for r in rows if r["status"] == 200]
    fails = [r for r in rows if r["status"] != 200]
    in_tok = sum(r["input_tokens"] for r in ok)
    out_tok = sum(r["output_tokens"] for r in ok)
    lat = [r["latency_ms"] for r in ok]
    call_credits = in_tok / TOKENS_PER_CREDIT if args.tokens_per_credit is None else in_tok / args.tokens_per_credit
    deploy_credits = args.deployments * DEPLOY_CREDITS

    print("=" * 62)
    print("Jev 代理调用统计")
    print("=" * 62)
    print(f"数据文件    : {DATA}")
    mcp = mcp_usage_csv()
    print(f"MCP 日志    : {mcp}{'' if mcp.is_file() else '（暂无，MCP 调用过才会生成）'}")
    print(f"时间范围    : {rows[0]['timestamp']} ~ {rows[-1]['timestamp']}")
    print(f"调用次数    : {len(rows)}  成功 {len(ok)}  失败 {len(fails)}"
          f"  成功率 {len(ok) / len(rows) * 100:.1f}%")

    print("\n--- token 用量 ---")
    print(f"输入 token 合计 : {in_tok:,}")
    print(f"输出 token 合计 : {out_tok:,}   （Jev 输出不计费）")
    if ok:
        per_call = [r["input_tokens"] for r in ok]
        total_q = max(sum(r["questions"] for r in ok), 1)
        print(f"单次输入 token  : 平均 {statistics.mean(per_call):.0f} / 中位 {statistics.median(per_call):.0f}"
              f" / P95 {pct(per_call, 0.95):.0f} / 最大 {max(per_call)}")
        print(f"每题开销        : 平均 {in_tok / total_q:.0f} token"
              f"（共 {total_q} 题，含 instructions/criteria 的固定开销）")

    print("\n--- 延迟 ---")
    if lat:
        print(f"平均 {statistics.mean(lat):.0f} ms / 中位 {statistics.median(lat):.0f} ms"
              f" / P95 {pct(lat, 0.95):.0f} ms / 最大 {max(lat)} ms")

    print("\n--- 按预设分组 ---")
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        if r.get("source") == "MCP":
            continue  # MCP 调用没有预设概念，单独在「按来源」里体现
        groups[r.get("preset") or "-"].append(r)
    total_in = max(in_tok, 1)
    print(f"{'预设':<8}{'次数':>6}{'输入token':>12}{'占比':>9}{'平均延迟':>10}  ")
    for name, items in sorted(groups.items(), key=lambda kv: -sum(i["input_tokens"] for i in kv[1])):
        sub_in = sum(i["input_tokens"] for i in items)
        avg_lat = statistics.mean([i["latency_ms"] for i in items])
        print(f"{name:<10}{len(items):>6}{sub_in:>12,}{sub_in / total_in * 100:>8.1f}%{avg_lat:>9.0f}ms  {bar(sub_in, total_in)}")

    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_day[(r["timestamp"] or "")[:10]].append(r)
    if len(by_day) > 1:
        print("\n--- 按日分组 ---")
        for day in sorted(by_day):
            items = by_day[day]
            print(f"{day}  调用 {len(items):>3}  输入 {sum(i['input_tokens'] for i in items):>7,} tokens")

    print("\n--- 按来源分组 ---")
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_source[r.get("source") or "未知"].append(r)
    for name, items in sorted(by_source.items()):
        sub_in = sum(i["input_tokens"] for i in items)
        print(f"{name:<8} 调用 {len(items):>3} 次   输入 {sub_in:>7,} token"
              f"   占 {sub_in / total_in * 100:>5.1f}%   {bar(sub_in, total_in)}")

    langs: Counter[str] = Counter()
    lang_tokens: dict[str, int] = defaultdict(int)
    for r in ok:
        m = re.search(r"lang=(\w+)", r.get("note") or "")
        if m:
            langs[m.group(1)] += 1
            lang_tokens[m.group(1)] += r["input_tokens"]
    if langs:
        print("\n--- 按语言（MCP 自动打的 lang 标记）---")
        for name, n in langs.most_common():
            print(f"{name:<8} {n:>3} 次   输入 {lang_tokens[name]:>7,} token")
        non_en = sum(v for k, v in langs.items() if k != "en")
        if non_en:
            print(f"  其中非英文 {non_en} 次 —— Jev 英文更准，必要时在调用方先翻译再提交")

    models = Counter(r.get("model") or "-" for r in ok)
    if models:
        print("\n--- 模型分布 ---")
        for name, n in models.most_common():
            print(f"{name:<16} {n:>4} 次")

    print("\n--- 成本估算 ---")
    print(f"Jev 调用        : {call_credits:.4f} credits"
          f"   （{in_tok:,} token ÷ {args.tokens_per_credit or TOKENS_PER_CREDIT:,.0f} token/credit）")
    if args.deployments:
        print(f"生产部署        : {deploy_credits} credits   （{args.deployments} 次 × {DEPLOY_CREDITS}）")
        print(f"合计            : {call_credits + deploy_credits:.4f} credits")
    print(f"反推真实单价    : 若账单里 AI 部分显示消耗 C credits，则每 token ≈ C ÷ {in_tok:,} credits")

    if fails:
        print("\n--- 失败记录 ---")
        for r in fails[-5:]:
            print(f"{r['timestamp']}  HTTP {r['status']}  preset={r.get('preset')}  note={r.get('note')}")
    print()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """把命令行 + MCP 两份记录合并导出成一个总表。"""
    rows = load_rows(args.since)
    if not rows:
        print("没有可导出的记录。")
        return 1
    out = Path(args.output) if args.output else ROOT / "data" / "usage_all.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([cn for _, cn in COLUMNS])
        for r in rows:
            writer.writerow([r.get(en, "") for en, _ in COLUMNS])
    print(f"已合并 {len(rows)} 条记录 -> {out}")
    print("（这是只读汇总，不会影响两份原始文件；下次要最新数据重新跑一次 export）")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    rows = load_rows(None)
    if not rows:
        print("还没有记录。先跑 python jev_usage.py call")
        return 1
    shown = rows[-args.last:]
    print(f"{'时间':<21}{'预设':<8}{'状态':>5}{'输入token':>10}{'输出token':>10}{'延迟ms':>8}")
    for r in shown:
        print(f"{r['timestamp']:<21}{str(r.get('preset')):<9}{r['status']:>5}"
              f"{r['input_tokens']:>9}{r['output_tokens']:>9}{r['latency_ms']:>8}")
    print(f"\n共 {len(rows)} 条，显示最近 {len(shown)} 条；明细文件：{DATA}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.required = True

    p = sub.add_parser("call", help="调用一次并记录用量")
    p.add_argument("--preset", default="ticket", choices=sorted(PRESETS))
    p.add_argument("--model", default="jev-latest")
    p.add_argument("--url", default=None)
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_call)

    p = sub.add_parser("stats", help="累计统计")
    p.add_argument("--since", default=None, help="只看该时间之后，如 2026-10-31")
    p.add_argument("--deployments", type=int, default=0, help="把 N 次生产部署（每次 15 credits）计入")
    p.add_argument("--tokens-per-credit", type=float, default=None, help="自定义换算，默认按文档 18000 token≈0.11 credit")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("log", help="看最近明细")
    p.add_argument("--last", type=int, default=10)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("export", help="合并两份记录导出总表 data/usage_all.csv")
    p.add_argument("--output", default=None, help="自定义输出路径")
    p.add_argument("--since", default=None, help="只看该时间之后")
    p.set_defaults(fn=cmd_export)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
