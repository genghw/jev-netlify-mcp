"""校验 dist/jev-proxy.zip：哈希是否匹配本地密钥、包内是否泄漏明文密钥。

    python verify_dist.py

只用标准库，不联网，不上传任何东西。
"""

from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "dist" / "jev-proxy.zip"


def config_file() -> Path:
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "jev-netlify-mcp" / "config.env"


def read_config() -> dict[str, str]:
    path = config_file()
    if not path.is_file():
        raise SystemExit(f"配置文件不存在：{path}\n先执行 python scripts/setup.py new-key")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def main() -> int:
    values = read_config()
    key = values.get("JEV_PROXY_KEY")
    if not key:
        print("[FAIL] 配置里没有 JEV_PROXY_KEY", file=sys.stderr)
        return 1
    if not ARCHIVE.is_file():
        print(f"[FAIL] 部署包不存在：{ARCHIVE}", file=sys.stderr)
        return 1

    expected = hashlib.sha256(key.encode()).hexdigest()
    failed = 0

    with zipfile.ZipFile(ARCHIVE) as zf:
        names = zf.namelist()
        print(f"部署包：{ARCHIVE.name}（{len(names)} 个文件，{ARCHIVE.stat().st_size / 1024:.1f} KB）")
        for name in names:
            print(f"  - {name}")

        mjs_name = "netlify/functions/jev.mjs"
        if mjs_name not in names:
            print(f"[FAIL] 包内缺少 {mjs_name}", file=sys.stderr)
            return 1
        mjs = zf.read(mjs_name).decode("utf-8")

        embedded = expected in mjs
        print(f"[{'PASS' if embedded else 'FAIL'}] 函数内嵌的 SHA-256 与本地密钥一致")
        failed += 0 if embedded else 1

        placeholder_left = "__JEV_PROXY_KEY_SHA256__" in mjs
        print(f"[{'FAIL' if placeholder_left else 'PASS'}] 占位符已被替换")
        failed += 1 if placeholder_left else 0

        # 明文密钥（jevp_ 前缀开头）不应该出现在任何文件里
        leaks = []
        for name in names:
            try:
                text = zf.read(name).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
            if key in text or "jevp_" in text:
                leaks.append(name)
        print(f"[{'FAIL' if leaks else 'PASS'}] 包内无明文密钥{('：' + ', '.join(leaks)) if leaks else ''}")
        failed += 1 if leaks else 0

    print(f"\n密钥前缀 {key[:5]}…（长度 {len(key)}），完整明文只在本地配置里")
    print("可以部署：https://app.netlify.com/drop 拖入 dist/jev-proxy.zip")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
