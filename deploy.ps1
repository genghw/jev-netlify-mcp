<#
.SYNOPSIS
    Jev Netlify 代理一键部署（幂等，可重复执行）

.DESCRIPTION
    依次完成：环境检查 -> 建 MCP 环境 -> 生成密钥 -> 校验部署包 -> 引导拖拽部署
    -> 回写站点地址 -> 四层诊断 -> 建无空格 Junction -> 注册 MCP 客户端

.PARAMETER SiteUrl
    已部署好的站点地址，例如 https://xxx.netlify.app。给了就不再交互提问。

.PARAMETER SkipClient
    只做服务端，不注册 MCP 客户端。

.PARAMETER Python313
    Python 3.13 解释器路径（MCP 依赖在 3.14 上装不了）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -SiteUrl https://my-site.netlify.app
#>
param(
    [string]$SiteUrl = "",
    [switch]$SkipClient,
    [string]$Python313 = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root

$JunctionPath = "D:\jev-mcp"
$Venv = Join-Path $Root ".venv313"
$Exe = Join-Path $Venv "Scripts\jev-netlify-mcp.exe"
$ConfigFile = Join-Path $env:APPDATA "jev-netlify-mcp\config.env"

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "    OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    !!  $text" -ForegroundColor Yellow }
function Die($text) { Write-Host "    XX  $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
Step 1 "环境检查"

# 找一个可用的 Python（优先 3.10+，MCP 需要 3.13）
$pyCandidates = @()
if ($Python313) { $pyCandidates += $Python313 }
$pyCandidates += @("D:\Python313\python.exe", "C:\Python313\python.exe")
$pyCandidates += @((Get-Command py -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }))

$Py313 = $null
foreach ($c in $pyCandidates) {
    if (-not $c) { continue }
    if ($c -like "*\py.exe") {
        $out = & $c -3.13 -c "import sys;print(sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py313 = "$c -3.13"; break }
    } elseif (Test-Path $c) {
        $v = & $c -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($v -eq "3.13") { $Py313 = $c; break }
    }
}
if (-not $Py313) {
    Warn "没找到 Python 3.13。MCP 依赖（rpds-py）在 3.14 上没有预编译包。"
    Warn "请装 Python 3.13 后用 -Python313 指定，或手动建 venv。"
} else {
    Ok "Python 3.13: $Py313"
}

$PyAny = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($PyAny) { Ok "默认 python: $PyAny（用于跑 setup 脚本，3.10+ 即可）" }
else { Die "PATH 里没有 python，无法运行 scripts\setup.py" }

# ---------------------------------------------------------------------------
Step 2 "MCP 运行环境"

if (Test-Path $Exe) {
    Ok "已存在：$Exe"
} elseif ($Py313) {
    if (-not (Test-Path $Venv)) {
        & cmd /c "`"$($Py313.Split(' ')[0])`" -m venv `"$Venv`"" 2>&1 | Out-Null
        Ok "已创建 venv：$Venv"
    }
    Write-Host "    安装 MCP 服务器（首次约 60 MB）..."
    & (Join-Path $Venv "Scripts\python.exe") -m pip install --no-cache-dir -q $Root
    if (Test-Path $Exe) { Ok "MCP 服务器已安装" } else { Warn "安装后仍未找到 exe，请手动检查" }
} else {
    Warn "跳过（缺 Python 3.13）"
}

# ---------------------------------------------------------------------------
Step 3 "生成密钥"

$key = $null
if (Test-Path $ConfigFile) {
    $line = Get-Content $ConfigFile | Where-Object { $_ -match '^JEV_PROXY_KEY=' }
    if ($line) { $key = ($line -split '=', 2)[1].Trim() }
}
if ($key) {
    Ok "密钥已存在（$($key.Substring(0,5))…，长度 $($key.Length)）；如需更换：python scripts\setup.py new-key --force"
} else {
    & $PyAny scripts\setup.py new-key
    Ok "已生成新密钥"
}

# ---------------------------------------------------------------------------
Step 4 "校验部署包"

& $PyAny verify_dist.py
if ($LASTEXITCODE -ne 0) { Die "部署包校验未通过，先别部署" }

# ---------------------------------------------------------------------------
Step 5 "部署到 Netlify"

if (-not $SiteUrl) {
    Write-Host "`n    接下来需要你手动操作：" -ForegroundColor Yellow
    Write-Host "    1. 打开 https://app.netlify.com/drop"
    Write-Host "    2. 把 $Root\dist\jev-proxy.zip 拖进去"
    Write-Host "    3. 站点创建后，点右下角 [Make public] 并确认（不点则全站 401）"
    $input = Read-Host "`n    完成后粘贴站点地址（留空跳过）"
    $SiteUrl = $input
}

if ($SiteUrl) {
    $SiteUrl = $SiteUrl.Trim().TrimEnd("/")
    & $PyAny scripts\setup.py set-url $SiteUrl
    Step 6 "四层诊断"
    & $PyAny diagnose_proxy.py
} else {
    Warn "未提供站点地址，跳过回写与诊断。之后执行：python scripts\setup.py set-url https://<站点>.netlify.app"
}

# ---------------------------------------------------------------------------
Step 7 "无空格路径入口（Windows 客户端需要）"

$hasSpace = $Root -match ' '
if (-not $hasSpace) {
    Ok "项目路径不含空格，无需 Junction"
} elseif (Test-Path $JunctionPath) {
    Ok "Junction 已存在：$JunctionPath"
} else {
    try {
        New-Item -ItemType Junction -Path $JunctionPath -Target $Root -ErrorAction Stop | Out-Null
        Ok "已创建 Junction：$JunctionPath -> $Root"
    } catch {
        Warn "创建失败（$($_.Exception.Message)），客户端请改用正斜杠路径"
    }
}
$McpRoot = if (Test-Path $JunctionPath) { "D:/jev-mcp" } else { $Root.Replace('\', '/') }

# ---------------------------------------------------------------------------
Step 8 "注册 MCP 客户端"

if ($SkipClient) {
    Warn "已跳过（-SkipClient）"
} else {
    $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
    if ($claude) {
        $list = & claude mcp list 2>&1 | Out-String
        if ($list -match '(^|\s)jev(\s|$)') {
            Ok "Claude Code 已注册 jev，跳过"
        } else {
            & claude mcp add --scope user jev -- (Join-Path $Venv "Scripts\jev-netlify-mcp.exe")
            Ok "已注册到 Claude Code（claude mcp list 可查看）"
        }
    } else {
        Warn "未安装 Claude Code CLI，跳过"
    }

    Write-Host "`n    CodeBuddy 手动配置（%USERPROFILE%\.codebuddy\mcp.json）：" -ForegroundColor Yellow
    Write-Host @"
{
  "mcpServers": {
    "jev": {
      "type": "stdio",
      "command": "$McpRoot/.venv313/Scripts/jev-netlify-mcp.exe",
      "args": [],
      "description": "TypeSafe Jev 决策模型（经 Netlify 代理）"
    }
  }
}
"@ -ForegroundColor Gray
    Write-Host "    注意：必须用正斜杠，反斜杠会被客户端二次反转义吃掉。" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
Write-Host "`n部署流程结束。" -ForegroundColor Cyan
Write-Host "日常命令：" -ForegroundColor Cyan
Write-Host "  python diagnose_proxy.py      # 分层体检"
Write-Host "  python jev_usage.py stats     # 用量统计"
Write-Host "  python jev_usage.py call      # 调一次并记录"
