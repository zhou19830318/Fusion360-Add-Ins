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
$cfg = $null
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { $cfg = $null }
    if ($cfg -and $cfg.deepseek_api_key) {
        Write-Host "       已检测到 API Key,无需重复配置。"
        Write-Host "           (以后可在 Fusion 面板右上角齿轮设置里更换提供商/模型)"
        exit 0
    }
}

# --- 2. Ask for the API key (Enter = skip) ---------------------------------
Write-Host ""
Write-Host "       尚未配置 API Key。"
$key = Read-Host "       粘贴你的 DeepSeek API Key(直接回车 = 跳过,稍后在 Fusion 里填)"
$key = ($key -replace '^\s+|\s+$', '') -replace '^"|"$', ''

if ($key) {
    if ($null -eq $cfg) {
        $cfg = if (Test-Path $template) {
            Get-Content $template -Raw | ConvertFrom-Json
        } else {
            [pscustomobject]@{}
        }
    }
    $cfg.provider         = "deepseek"
    $cfg.model            = "deepseek-v4-pro"
    $cfg.deepseek_api_key = $key

    $dir = Split-Path -Parent $cfgPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $json = $cfg | ConvertTo-Json -Depth 10
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