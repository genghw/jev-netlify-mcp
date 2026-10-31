#!/usr/bin/env python3
"""Set up the key-locked Jev proxy. Standard library only.

  python scripts/setup.py new-key            create a key and build dist/ (+ dist/jev-proxy.zip)
  python scripts/setup.py set-url URL        remember your Netlify site address
  python scripts/setup.py check [--live]     test the proxy (--live also asks Jev one question)
  python scripts/setup.py show-key           print the key (only when you need it elsewhere)

The key is stored in your user config file (and on Windows also in your user environment
variables). Only its SHA-256 hash goes into the Netlify function. The key is never printed
unless you run show-key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from jev_netlify_mcp.config import config_file, registry_enabled  # noqa: E402

PROXY = ROOT / "proxy"
DIST = ROOT / "dist"
PLACEHOLDER = "__JEV_PROXY_KEY_SHA256__"


def read_config() -> dict[str, str]:
    path = config_file()
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def write_config(values: dict[str, str]) -> None:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# jev-netlify-mcp settings. Keep this file private.\n"
    body += "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_text(body, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    if registry_enabled():
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            for name, value in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def build_dist(key_hash: str) -> Path:
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(PROXY, DIST / "site")
    fn = DIST / "site" / "netlify" / "functions" / "jev.mjs"
    text = fn.read_text(encoding="utf-8")
    if PLACEHOLDER not in text:
        raise SystemExit("placeholder not found in proxy/netlify/functions/jev.mjs")
    fn.write_text(text.replace(PLACEHOLDER, key_hash), encoding="utf-8")
    archive = DIST / "jev-proxy.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((DIST / "site").rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(DIST / "site").as_posix())
    return archive


def cmd_new_key(args: argparse.Namespace) -> None:
    values = read_config()
    if values.get("JEV_PROXY_KEY") and not args.force:
        print("A key already exists. Use --force to replace it (then redeploy the proxy).")
        print("To rebuild dist/ with the existing key, run: python scripts/setup.py rebuild")
        return
    key = "jevp_" + secrets.token_hex(32)
    values["JEV_PROXY_KEY"] = key
    write_config(values)
    archive = build_dist(hashlib.sha256(key.encode()).hexdigest())
    print("Key created and saved (not shown). Config file:", config_file())
    print("Deploy this to Netlify:", archive)
    print("  Netlify Drop: https://app.netlify.com/drop  (drag the zip or the dist/site folder)")
    print("Then run: python scripts/setup.py set-url https://<your-site>.netlify.app")


def cmd_rebuild(_: argparse.Namespace) -> None:
    key = read_config().get("JEV_PROXY_KEY")
    if not key:
        raise SystemExit("No key yet. Run: python scripts/setup.py new-key")
    print("Rebuilt:", build_dist(hashlib.sha256(key.encode()).hexdigest()))


def cmd_set_url(args: argparse.Namespace) -> None:
    url = args.url.strip().rstrip("/")
    if not url.startswith("https://"):
        raise SystemExit("The address must start with https://")
    values = read_config()
    values["JEV_PROXY_URL"] = url
    write_config(values)
    print("Saved:", url)
    print("Restart your MCP clients (Claude Code, Codex, Claude Desktop) so they pick it up.")


def _call(url: str, key: str | None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "User-Agent": "jev-netlify-mcp-setup"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def cmd_check(args: argparse.Namespace) -> None:
    values = read_config()
    url, key = values.get("JEV_PROXY_URL"), values.get("JEV_PROXY_KEY")
    if not (url and key):
        raise SystemExit("Run new-key and set-url first.")
    status, _ = _call(url + "/v1/models", None)
    print(f"without key -> {status} ({'OK, locked' if status == 401 else 'UNEXPECTED: the proxy is not locked'})")
    status, body = _call(url + "/v1/models", key)
    print(f"with key    -> {status}", [m.get("name") for m in body.get("models", [])])
    if status == 401:
        print("The deployed proxy has a different key hash. Run `rebuild` and deploy dist/ again.")
    if args.live and status == 200:
        # model 是上游必填字段；缺了会被 Jev 以 400 invalid_req_data 拒绝。
        status, body = _call(url + "/v1/systemone", key, {
            "model": "jev-latest",
            "state": "The package arrived two weeks late and the box was crushed.",
            "questions": {"complaint": {"type": "noul", "instructions": "The customer is complaining"}},
        })
        print(f"live Jev    -> {status}", json.dumps(body.get("answers", body))[:300])


def cmd_show_key(_: argparse.Namespace) -> None:
    key = read_config().get("JEV_PROXY_KEY")
    print(key or "No key yet.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new-key"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_new_key)
    sub.add_parser("rebuild").set_defaults(fn=cmd_rebuild)
    s = sub.add_parser("set-url"); s.add_argument("url"); s.set_defaults(fn=cmd_set_url)
    s = sub.add_parser("check"); s.add_argument("--live", action="store_true"); s.set_defaults(fn=cmd_check)
    sub.add_parser("show-key").set_defaults(fn=cmd_show_key)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
