"""分层诊断 Netlify Jev 代理，把"401"拆成 4 种不同原因。

    python diagnose_proxy.py                 # 用 config.env 里的站点和密钥
    python diagnose_proxy.py --url https://xxx.netlify.app

分层：
    第 1 层  静态托管      GET /                     应当 200 且是 index.html
    第 2 层  函数是否挂上  GET /v1/models（不带密钥） 应当 401 且是 JSON
    第 3 层  密钥是否匹配  GET /v1/models（带密钥）   应当 200 且列出模型
    第 4 层  上游是否通    POST /v1/systemone        应当 200 且返回 answers
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

URL_NAMES = ("JEV_PROXY_URL", "TYPESAFE_BASE_URL")
KEY_NAMES = ("JEV_PROXY_KEY", "TYPESAFE_API_KEY")


def config_file() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "jev-netlify-mcp" / "config.env"


def _file_values() -> dict[str, str]:
    path = config_file()
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return values


def _registry(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip() or None
    except Exception:  # noqa: BLE001
        return None


def lookup(names: tuple[str, ...]) -> str | None:
    for n in names:
        if (os.environ.get(n) or "").strip():
            return os.environ[n].strip()
    for n in names:
        if _registry(n):
            return _registry(n)
    values = _file_values()
    for n in names:
        if values.get(n):
            return values[n]
    return None


def probe(url: str, key: str | None) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "ctype": resp.headers.get("content-type", ""),
                    "body": resp.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "ctype": exc.headers.get("content-type", ""),
                "body": exc.read().decode("utf-8", "replace")}
    except urllib.error.URLError as exc:
        return {"status": 0, "ctype": "", "body": f"连接失败: {exc.reason}"}


def classify(res: dict, expect_401: bool = False) -> str:
    """把一次响应归到具体原因上。

    expect_401=True 用于"故意不带密钥"的探测：这时 JSON 的 401 恰恰是正确结果，
    说明函数在跑、鉴权生效，不能报成 key_mismatch。
    """
    status, ctype, body = res["status"], res["ctype"].lower(), res["body"]
    if status == 0:
        return "dns"
    if "edge-access" in body or "Login Redirect" in body:
        return "edge_access"
    if "text/plain" in ctype and "Request ID" in body:
        return "netlify_edge_error"
    if status == 200 and "text/html" in ctype:
        return "ok_static"
    if "json" in ctype:
        if status == 200:
            return "ok_json"
        if expect_401 and status == 401 and "authentication_error" in body:
            return "ok_json"
        if "authentication_error" in body:
            return "key_mismatch"
        if "configuration_error" in body:
            return "gateway_missing"
        return "json_error"
    if status == 404:
        return "not_found"
    return "other"


VERDICT = {
    "edge_access": (
        "【平台层】站点启用了 Netlify Edge Access / 访客登录保护，请求在到函数前就被 401 拦截。",
        [
            "Netlify 控制台 → 该站点 → Site configuration → Access & security，关闭 Password protection / Edge Access",
            "若是团队强制 SSO：Team settings → Security 里关闭，或改用个人 team 重新部署",
            "关闭后重跑本脚本，应当看到第 2 层返回 JSON 的 401",
        ],
    ),
    "netlify_edge_error": (
        "【平台层】Netlify 边缘返回纯文本 401/5xx，通常是函数未打包或路由未注册。",
        [
            "确认部署的是 dist/jev-proxy.zip（或 dist/site），不是仓库根目录或 proxy/ 目录",
            "确认 zip 根目录直接是 netlify.toml 和 netlify/functions/jev.mjs，没有多套一层文件夹",
            "Netlify 控制台 → Deploys → 该次部署 → 看 Functions 列表里有没有 jev",
        ],
    ),
    "key_mismatch": (
        "【应用层】已到达函数，但请求头里的密钥与函数内嵌的 SHA-256 不一致。",
        [
            "python scripts/setup.py show-key 确认本地密钥",
            "python scripts/setup.py rebuild 重建，再重新部署 dist/jev-proxy.zip",
            "确认 Netlify 环境变量里没有自己设的 JEV_PROXY_KEY_SHA256 覆盖了内嵌值",
        ],
    ),
    "gateway_missing": (
        "【上游层】函数在跑，但 Netlify AI Gateway 没注入 TYPESAFE_API_KEY。",
        [
            "确认站点是生产部署（production deploy），部署预览不带网关凭证",
            "删除自己设置的任何 TYPESAFE_* 环境变量（设了就不再自动注入）",
            "确认 Netlify 账号的 AI 功能已启用",
        ],
    ),
    "not_found": (
        "【平台层】路径没匹配到函数，通常是函数目录结构不对。",
        [
            "确认 zip 内是 netlify/functions/jev.mjs",
            "确认 netlify.toml 里 functions 目录配置正确",
        ],
    ),
    "ok_json": ("正常返回 JSON。", []),
    "ok_static": ("静态托管正常（返回 index.html）。", []),
    "json_error": ("函数返回了 JSON 错误，按 body 里的 message 处理。", []),
    "dns": ("域名解析失败，检查站点地址是否拼错或站点已删除。", []),
    "other": ("未识别的响应，见上方原始输出。", []),
}


def show(step: str, expect: str, res: dict, expect_401: bool = False) -> str:
    kind = classify(res, expect_401=expect_401)
    body = res["body"].strip().replace("\n", " ")[:160]
    print(f"\n{step}")
    print(f"  期望: {expect}")
    print(f"  实际: HTTP {res['status']}  [{res['ctype'] or 'no content-type'}]  -> {kind}")
    print(f"  body: {body}")
    return kind


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None)
    args = parser.parse_args()

    url = (args.url or lookup(URL_NAMES) or "").rstrip("/")
    key = lookup(KEY_NAMES)
    if not url:
        print("没有站点地址：先 python scripts/setup.py set-url https://xxx.netlify.app", file=sys.stderr)
        return 2
    if not key:
        print("没有密钥：先 python scripts/setup.py new-key", file=sys.stderr)
        return 2

    print(f"站点: {url}")
    print(f"密钥: {key[:5]}…（长度 {len(key)}）")

    kinds = [
        show("[第1层] 静态托管 GET /", "200 + index.html", probe(f"{url}/", None)),
        show("[第2层] 函数是否挂上 GET /v1/models（无密钥）", "401 且是 JSON", probe(f"{url}/v1/models", None), expect_401=True),
        show("[第3层] 密钥是否匹配 GET /v1/models（带密钥）", "200 且列出 models", probe(f"{url}/v1/models", key)),
    ]

    # 前一层没过就不用打第 4 层，避免无意义消耗上游额度
    blocked = [k for k in kinds if k in ("edge_access", "netlify_edge_error", "not_found", "dns", "other")]
    if blocked:
        print("\n[第4层] POST /v1/systemone —— 跳过（前序层已阻断）")
    else:
        payload = {
            "model": "jev-latest",
            "state": "测试：接口连通性检查。",
            "questions": {"ping": {"type": "noul", "instructions": "这是一个测试请求吗？"}},
        }
        req = urllib.request.Request(
            f"{url}/v1/systemone",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                res = {"status": resp.status, "ctype": resp.headers.get("content-type", ""),
                       "body": resp.read().decode("utf-8", "replace")}
        except urllib.error.HTTPError as exc:
            res = {"status": exc.code, "ctype": exc.headers.get("content-type", ""),
                   "body": exc.read().decode("utf-8", "replace")}
        except urllib.error.URLError as exc:
            res = {"status": 0, "ctype": "", "body": f"连接失败: {exc.reason}"}
        kinds.append(show("[第4层] 上游是否通 POST /v1/systemone", "200 且返回 answers", res))

    # 第 1 层就被 Edge Access 拦了，后面的纯文本 401 是同一个原因（带 Authorization 头时
    # Netlify 不跳登录页而是吐纯文本），别再报成"函数未打包"误导人。
    if kinds and kinds[0] == "edge_access":
        kinds = ["edge_access" if k == "netlify_edge_error" else k for k in kinds]

    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    seen = []
    for kind in kinds:
        if kind in seen or kind in ("ok_json", "ok_static"):
            continue
        seen.append(kind)
        title, hints = VERDICT[kind]
        print(f"\n{title}")
        for hint in hints:
            print(f"  - {hint}")
    if all(k in ("ok_json", "ok_static") for k in kinds):
        print("\n全部通过：代理已可用，直接用 POST /v1/systemone 调 Jev。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
