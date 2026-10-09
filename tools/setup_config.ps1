param(
    [string]$DeployDir = ""
)

# ============================================================================
#  AI Fusion - API Key guided configuration
#  Usage: powershell -ExecutionPolicy Bypass -File setup_config.ps1 -DeployDir <dir>
#  - Detects an existing configured config.json and skips
#  - Otherwise interactively asks for the DeepSeek API key and writes config.json
#  - If skipped, the key can be entered later inside the Fusion palette settings
# ============================================================================
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($DeployDir)) {
    $DeployDir = Split-Path -Parent $PSScriptRoot
}
$cfgPath     = Join-Path $DeployDir "local_server\config.json"
$template    = Join-Path $DeployDir "local_server\config.example.json"
$targetIndex = "$DeployDir\index.html.txt"   # placeholder (unused)

# --- 1. Already configured? -------------------------------------------------
#注意:这里必须先把JSON 转成 hashtable,不能用 ConvertFrom-Json 得到的
# PSCustomObject直接 赋值。旧版 config.json(或用户手写的精简配置)可能根本没有
# provider/model 字段,而 PowerShell 5.1 不允许给 PSCustomObject 新增属性:
#   "设置"provider"时发生异常: 在此对象上找不到属性"provider"。
# 那会让整个安装脚本在第4 步崩掉,API Key 永远写不进 config.json。
$cfg = @{}
$cfgPathExists = Test-Path $cfgPath
if ($cfgPathExists) {
    try {
        $raw = Get-Content $cfgPath -Raw | ConvertFrom-Json
        if ($raw) { foreach ($p in $raw.PSObject.Properties) { $cfg[$p.Name] = $p.Value } }
    } catch {
        #坏 JSON:当成空配置继续,下面用模板补齐
        $cfg = @{}
    }
}

if ($cfg.ContainsKey("deepseek_api_key") -and $cfg["deepseek_api_key"]) {
    Write-Host "       已检测到 API Key,无需重复配置。"
    Write-Host "           (以后可在 Fusion 面板右上角齿轮设置里更换提供商/模型)"
    exit 0
}

# --- 2. Ask for the API key (Enter = skip) ---------------------------------
Write-Host ""
Write-Host "       尚未配置 API Key。"
$key = Read-Host "       粘贴你的 DeepSeek API Key(直接回车 = 跳过,稍后在 Fusion 里填)"
$key = ($key -replace '^\s+|\s+$', '') -replace '^"|"$', ''

if ($key) {
    # --- 3. 用模板补齐缺失字段 -------------------------------------------
    #现有 config.json 可能来自旧版本,缺 base_url / server_port 等键。
    # 以 example 为基底覆盖,保证字段齐全(值不同才覆盖,不碰用户已有配置)。
    if (Test-Path $template) {
        try {
            $tpl = Get-Content $template -Raw | ConvertFrom-Json
            if ($tpl) {
                foreach ($p in $tpl.PSObject.Properties) {
                    if (-not $cfg.ContainsKey($p.Name)) { $cfg[$p.Name] = $p.Value }
                }
            }
        } catch {
            Write-Host "       [警告] config.example.json 解析失败,仅写入必要字段。"
        }
    }

    $cfg["provider"]         = "deepseek"
    $cfg["model"]            = "deepseek-flash"
    $cfg["deepseek_api_key"] = $key

    $dir = Split-Path -Parent $cfgPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    # PowerShell 5.1 的 ConvertTo-Json 会把 hashtable 保持为对象(不会退化成
    # 数组),深度给 10 足够;转回 PSCustomObject 仅为保证输出是标准 JSON 对象。
    $json = [pscustomobject]$cfg | ConvertTo-Json -Depth 10
    # PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM,导致插件 JSON 解析失败;
    # 改用显式无 BOM 的 UTF-8 写入
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($cfgPath, $json, $utf8NoBom)
    Write-Host "       已保存: $cfgPath"
    Write-Host "       (也可在 Fusion 面板设置里修改提供商/模型)"
} else {
    Write-Host "       已跳过。稍后可在 Fusion 面板设置中添加:"
    Write-Host "           右上角齿轮 -> 选择 DeepSeek -> 粘贴 Key -> Save"
}
exit 0