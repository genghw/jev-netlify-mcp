# Jev Netlify 代理（中文增强版）

把 [TypeSafe Jev](https://typesafe.ai)（System 1 决策模型）变成一个**受密钥保护、走 Netlify AI Gateway 免费额度**的私有端点，并接入 Claude Code / CodeBuddy 等 MCP 客户端。

本项目 fork 自 [NighCross/jev-netlify-mcp](https://github.com/NighCross/jev-netlify-mcp)（MIT），在保持原有能力的前提下补了**修 bug、加可观测性、加一键部署和中文文档**。

---

## 一、和上游有什么不同

### 1.1 修复（上游 bug）

| 文件 | 问题 | 修复 |
| --- | --- | --- |
| `scripts/setup.py` | `check --live` 的请求体漏了 `model` 字段，上游会返回 `400 invalid_req_data`，极易误判为部署失败 | 请求体补 `"model": "jev-latest"` |

### 1.2 增强（新增能力）

| 文件 | 能力 |
| --- | --- |
| `src/jev_netlify_mcp/server.py` | **MCP 调用也记用量**：每次 `jev_evaluate` 写一行 CSV（时间/来源/模型/状态/输入输出 token/延迟/问题数/请求ID），失败也记；**自动识别调用方**（`CodeBuddy` / `Claude Code` / `Codex` / `Cursor`，读 MCP 握手的 `clientInfo`）；**自动标注语言**（`lang=zh/en/ja/ko/mixed`） |
| `jev_usage.py` | 用量工具：`call` 调一次并记录、`stats` 出报表（token / 延迟 / 分组 / 成本估算）、`log` 看明细、`export` 合并两份记录 |
| `diagnose_proxy.py` | **四层分层诊断**：静态托管 → 函数挂载 → 密钥校验 → 上游网关，一眼看出卡在哪层，不会像上游 `check` 那样把所有非 200 都误报成「密钥不匹配」 |
| `verify_dist.py` | 部署包安全校验：哈希是否与本地密钥一致、占位符是否替换、**是否泄漏明文密钥** |
| `t_proxy_call.py` | 独立调用示例（纯标准库，不依赖仓库代码）：`--check` 验鉴权、`--live` 真调一次 |
| `deploy.ps1` | **一键部署**：环境检查 → 建 venv → 生成密钥 → 校验包体 → 引导部署 → 回写地址 → 建无空格入口 → 注册客户端 |
| `部署记录.md` / `MCP接入说明.md` | 中文全流程复现文档 + 给 AI 直接执行的接入指令 |

### 1.3 实测沉淀的坑（上游文档未提）

- **新站点默认是 Private**：所有请求（含首页）返回 401 跳登录页，必须点 **Make public**，否则会被误判成密钥错误
- **Python 3.14 装不了 MCP**：`rpds-py` 无 cp314 wheel，必须用 3.13
- **Windows 路径空格**：客户端会把 `D:\python contents\...` 截断成 `D:\python`，需用 Junction 或正向斜杠
- **CodeBuddy 会二次反转义**：`command` 里的 `\\` 会被吃掉，必须写 `D:/...` 正斜杠
- `jev-latest` 当前解析为 `jev-1.14.0`

---

## 二、快速开始

### 2.1 三步跑通（最简）

```powershell
git clone --depth 1 https://github.com/NighCross/jev-netlify-mcp.git jev-netlify-proxy
cd jev-netlify-proxy

python scripts\setup.py new-key      # 生成密钥 + dist\jev-proxy.zip
python verify_dist.py                # 校验包里没有明文密钥

# 手动把 dist\jev-proxy.zip 拖到 https://app.netlify.com/drop
# 站点创建后务必点右下角 "Make public"

python scripts\setup.py set-url https://<你的站点>.netlify.app
python diagnose_proxy.py             # 四层体检
```

> 生成部署包**不需要** `pip install`，`scripts/setup.py` 全是标准库。

### 2.2 一键部署（推荐）

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```

脚本会依次做：检查 Python 版本 → 建 `.venv313` → 安装 MCP 服务器 → 生成密钥 → 校验包体 → 提示你去拖拽部署 → 回写地址 → 建无空格 Junction → 注册 Claude Code → 打印 CodeBuddy 配置。

参数：

| 参数 | 作用 |
| --- | --- |
| `-SiteUrl https://xxx.netlify.app` | 跳过交互，直接回写站点地址 |
| `-SkipClient` | 只做服务端，不注册 MCP 客户端 |
| `-Python313 D:\Python313\python.exe` | 指定 Python 3.13 解释器路径 |

---

## 三、接入 MCP 客户端

### Claude Code

```powershell
claude mcp add --scope user jev -- "<项目绝对路径>\.venv313\Scripts\jev-netlify-mcp.exe"
claude mcp list
```

### CodeBuddy

`C:\Users\<你>\.codebuddy\mcp.json`：

```json
{
  "mcpServers": {
    "jev": {
      "type": "stdio",
      "command": "D:/jev-netlify-proxy/.venv313/Scripts/jev-netlify-mcp.exe",
      "args": [],
      "description": "TypeSafe Jev 决策模型（经 Netlify 代理）"
    }
  }
}
```

**路径含空格时必须换成正斜杠**，否则反斜杠会被二次反转义吃掉。

接好后在客户端里直接说需求即可，例如：

> 判断这条工单：点击支付后白屏，换过两个浏览器 —— 是不是 bug？该给哪个团队？多急？

客户端会自动调用 `jev_evaluate` 并把概率结论翻译回中文。

---

## 四、安全须知（发布/开源前必读）

### 4.1 绝对不能提交的东西

| 内容 | 为什么 |
| --- | --- |
| `%APPDATA%\jev-netlify-mcp\config.env` | **明文代理密钥**，拿到即可消耗你的 Netlify credits |
| `dist/` | 构建产物，内含部署包（虽然只有 SHA-256，也没必要提交） |
| `data/`、`*.csv` | 调用明细，含 `request_id` 与业务文本 |
| `.venv*` | 本地环境，数十 MB |
| 任何含真实密钥、站点地址、绝对路径的本地文档 | 用 `.local.md` 后缀并加入 `.gitignore` |

本仓库的 `.gitignore` 已覆盖以上全部。提交前自查：

```powershell
git status --porcelain                                  # 看哪些文件会进版本库
git grep -nI "jevp_" $(git rev-list --all) 2>$null       # 全历史扫描明文密钥
```

### 4.2 密钥的三层保护

1. **部署包里只有 SHA-256 哈希**，无法反推明文（`jev.mjs` 用 `timingSafeEqual` 比对）
2. **站点必须设为 Public 才能被外部调用**，但接口本身仍要求 Bearer 密钥，未授权请求 401 且不打到上游、**不消耗 credits**
3. 密钥只存在你本机：`config.env` + 注册表 `HKCU\Environment`

### 4.3 额度提醒

- 免费计划 **300 credits/月**
- **每次生产部署固定 15 credits** —— 部署成功后别反复拖 zip
- Jev 调用本身极便宜：实测单次约 350 输入 token ≈ 0.0027 credits

---

## 五、日常运维

```powershell
python diagnose_proxy.py            # 接口分层体检
python jev_usage.py stats           # 用量统计（按来源/语言/预设分组）
python jev_usage.py call            # 命令行调一次并记录
python jev_usage.py export          # 合并两份记录 → data/usage_all.csv
python scripts\setup.py show-key    # 查看密钥
python scripts\setup.py rebuild     # 换包内容但保留密钥
```

---

## 六、目录结构

```
proxy/                     部署包源文件（netlify.toml、functions、public）
src/jev_netlify_mcp/       MCP Server（config.py、server.py[已增强]）
scripts/setup.py           配置脚本（new-key/rebuild/set-url/check/show-key）[已修 bug]
deploy.ps1                 一键部署
diagnose_proxy.py          四层诊断
jev_usage.py               用量记录与统计
verify_dist.py             部署包校验
t_proxy_call.py            独立调用示例
部署记录.md                完整复现手册
MCP接入说明.md             给 AI 执行的接入指令
```

---

## 七、许可

上游为 MIT，本项目沿用 MIT，保留原始 `LICENSE` 与版权声明。二次分发请注明来源。
