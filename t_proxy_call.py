"""部署后验证与调用示例（纯标准库，不依赖 requests）。

    python t_proxy_call.py --check     # 不带密钥预期 401，带密钥预期 200
    python t_proxy_call.py --live      # 真实调一次 /v1/systemone（消耗极少量 credits）

地址与密钥的读取顺序和仓库一致：
    进程环境变量 JEV_PROXY_URL / JEV_PROXY_KEY
    -> Windows 用户注册表 HKCU\\Environment
    -> %APPDATA%\\jev-netlify-mcp\\config.env
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

SAMPLE_STATE = "用户反馈：点击支付后页面白屏，已经尝试了两个浏览器。"
SAMPLE_QUESTIONS = {
    "is_bug": {
        "type": "noul",
        "instructions": "这是否是一个软件缺陷报告？",
    },
    "team": {
        "type": "choice",
        "instructions": "应该由哪个团队处理？",
        "criteria": {
            "payments": "支付、账单、退款相关",
            "frontend": "页面渲染、浏览器兼容性问题",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "紧急程度",
        "criteria": ["下个版本再说", "本周内修", "正在阻塞收入"],
    },
}


def config_file() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "jev-netlify-mcp" / "config.env"


def _from_registry(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip() or None
    except OSError:
        return None


def _from_file(name: str) -> str | None:
    path = config_file()
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name and v.strip():
            return v.strip()
    return None


def lookup(names: tuple[str, ...]) -> str | None:
    for name in names:
        if (os.environ.get(name) or "").strip():
            return os.environ[name].strip()
    for name in names:
        value = _from_registry(name)
        if value:
            return value
    for name in names:
        value = _from_file(name)
        if value:
            return value
    return None


def request(url: str, key: str | None, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body[:300]}
    except urllib.error.URLError as exc:
        print(f"连接失败: {exc.reason}", file=sys.stderr)
        return 0, {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="验证密钥校验是否生效")
    parser.add_argument("--live", action="store_true", help="真实调用一次 /v1/systemone")
    parser.add_argument("--url", default=None, help="覆盖站点地址，如 https://xxx.netlify.app")
    args = parser.parse_args()

    url = (args.url or lookup(URL_NAMES) or "").rstrip("/")
    key = lookup(KEY_NAMES)
    if not url:
        print("没有站点地址。先部署，再执行：python scripts/setup.py set-url https://<site>.netlify.app", file=sys.stderr)
        return 2
    if not key:
        print("没有代理密钥。执行：python scripts/setup.py new-key", file=sys.stderr)
        return 2

    print(f"站点: {url}")
    print(f"密钥: {key[:5]}…（长度 {len(key)}）")

    failed = 0
    if args.check or not args.live:
        status_no_key, _ = request(f"{url}/v1/models", None)
        print(f"[{'PASS' if status_no_key == 401 else 'FAIL'}] 不带密钥 GET /v1/models -> {status_no_key}（预期 401）")
        failed += 0 if status_no_key == 401 else 1

        status, body = request(f"{url}/v1/models", key)
        names = [m.get("name") for m in body.get("models", [])]
        print(f"[{'PASS' if status == 200 else 'FAIL'}] 带密钥 GET /v1/models -> {status} {names}")
        failed += 0 if status == 200 else 1

        if status == 401:
            print("  密钥哈希不匹配：改过 key 的话需要 rebuild 后重新部署 dist/")

    if args.live:
        payload = {"model": "jev-latest", "state": SAMPLE_STATE, "questions": SAMPLE_QUESTIONS}
        status, body = request(f"{url}/v1/systemone", key, payload)
        print(f"\nPOST /v1/systemone -> {status}")
        print(json.dumps(body, ensure_ascii=False, indent=2)[:1500])
        if status != 200:
            failed += 1
        else:
            answers = body.get("answers") or {}
            for name, item in answers.items():
                print(f"  {name}: {json.dumps(item, ensure_ascii=False)}")
            print("\n[OK] 代理已打通，可以直接调用 Jev")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
