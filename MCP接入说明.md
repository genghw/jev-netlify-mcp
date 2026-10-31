# 给 Claude Code 的执行说明：注册 jev MCP 服务器

> 本文档可直接交给 Claude Code 执行。你只需要按「第 2 节」跑命令，不要改动仓库代码。

## 1. 现状（已确认，无需再安装任何东西）

| 项目 | 状态 | 位置 |
| --- | --- | --- |
| Claude Code CLI | 已安装 v2.1.304 | `C:\Users\<你>\AppData\Roaming\npm\claude.ps1` |
| MCP Server 可执行文件 | 已安装 | `<项目目录>\.venv313\Scripts\jev-netlify-mcp.exe` |
| 运行环境 | 已就绪（Python 3.13 venv，含 `mcp` 依赖） | `<项目目录>\.venv313` |
| 代理密钥 | 已在 Windows 用户注册表 `HKCU\Environment\JEV_PROXY_KEY` | MCP Server 自动读取，无需显式配置 |
| 代理地址 | 已在 `%APPDATA%\jev-netlify-mcp\config.env` 的 `JEV_PROXY_URL` | 同上 |
| 代理连通性 | 已验证四层全通（含真实 Jev 调用 200） | 无需重复验证 |

**结论：不需要 pip install、不需要 npm install、不需要下载模型或权重。** 只需执行一条 `claude mcp add` 注册命令。

## 2. 需要执行的命令

在 PowerShell 里执行（路径含空格，**双引号必须保留**）：

```powershell
claude mcp add --scope user jev -- "<项目目录>\.venv313\Scripts\jev-netlify-mcp.exe"
```

然后验证：

```powershell
claude mcp list
```

预期输出中出现一行包含 `jev` 的条目，状态为 connected（首次可能显示 pending，重新执行一次 `claude mcp list` 即可）。

## 3. 注册后如何验证端到端可用

1. **必须新开一个 Claude Code 会话**（MCP 配置在进程启动时加载，旧会话不生效）。
2. 在新会话里发这句提示词：

```
用 jev 工具判断：用户反馈「点击支付后页面白屏，已经尝试了两个浏览器」。
问题：1) 是否为软件缺陷（noul）2) 该给 payments 还是 frontend（choice）3) 紧急度 1-3（score）
```

3. 预期 Claude Code 会调用 `jev_evaluate`，并返回类似：

```
是缺陷 0.92；frontend 0.74（payments 0.26）；紧急度 1.99
```

如果返回了概率数值，说明整条链路（Claude Code → MCP → Netlify 代理 → Jev）已通。

## 4. 失败时怎么处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `claude: 无法将"claude"项识别为 cmdlet` | 不在 PATH 或用了 cmd | 用完整路径 `& "C:\Users\<你>\AppData\Roaming\npm\claude.ps1" mcp add ...` |
| 提示找不到 exe | venv 被删或路径写错 | 检查 `.venv313\Scripts\jev-netlify-mcp.exe` 是否存在；不存在则执行 `D:\Python313\python.exe -m venv .venv313` 后 `\.venv313\Scripts\python.exe -m pip install .`（在仓库根目录） |
| `claude mcp list` 显示 jev 为 failed | Server 启动即退出 | 手动跑一次 exe 看报错：`& "<项目目录>\.venv313\Scripts\jev-netlify-mcp.exe"`；常见原因是密钥缺失 |
| 调用时返回 401 | 代理密钥与部署的哈希不一致 | 在仓库目录执行 `python scripts\setup.py show-key` 核对；必要时 `new-key --force` + `rebuild` 后重新部署 |
| 调用时返回 500 `configuration_error` | Netlify AI Gateway 未注入上游凭证 | 检查 Netlify 后台是否误设了 `TYPESAFE_*` 环境变量（设了就会停止注入，需删除） |
| 超过 60 秒无响应 | 跨境网络抖动 | 重试一次；持续失败则 `python diagnose_proxy.py` 逐层排查 |

## 5. 边界与注意事项

- **只允许做一件事**：执行第 2 节的 `claude mcp add` 命令并验证。**不要修改** `<项目目录>` 目录下的任何文件（`scripts/setup.py` 已含本地补丁，改动会导致 400 错误复现）。
- **作用范围**：`--scope user` 写入 `C:\Users\<你>\.claude.json`，对该用户所有项目生效；不影响仓库、不影响其他 MCP 服务器。
- **撤销方式**：`claude mcp remove jev -s user`，即可完全回滚。
- **成本**：每次 `jev_evaluate` 都是一次真实 Jev 调用，按输入 token 计费（实测单次约 300–500 token ≈ 0.002–0.004 credits），可忽略；可用 `python jev_usage.py stats` 查看累计（注意 MCP 调用不会自动进这个 CSV，只统计 `jev_usage.py call` 发起的调用）。
- **模型**：工具默认使用 `jev-latest`（当前解析为 `jev-1.14.0`）。

## 6. 可选：同时接入 Codex CLI

若需要给 Codex 也注册，编辑 `C:\Users\<你>\.codex\config.toml`，追加：

```toml
[mcp_servers.jev]
command = "D:\\python contents\\jev-netlify-proxy\\.venv313\\Scripts\\jev-netlify-mcp.exe"
```

（TOML 里反斜杠需要转义。此项非必须，本次任务可不做。）
