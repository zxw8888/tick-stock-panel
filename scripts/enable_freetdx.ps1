# 一键启用 freetdx 免费数据源路由
# 作用: 备份 preferences.json 后, 把 分钟K/全量分钟/五档盘口 三个数据集路由到 freetdx。
# 财务/实时/复权保持原配置不动(已有 fuyao)。
# 若 freetdx 插件不可用(服务器全挂), 程序启动时自动回退 tickflow, 无需手工还原。
#
# 用法: powershell -File scripts/enable_freetdx.ps1  (需在项目根目录运行)

$ErrorActionPreference = "Stop"
$prefPath = Join-Path $env:USERPROFILE "..\..\..\"  # fallback, 见下方实际定位
# 桌面版数据目录优先: D:\TSP\data; 源码运行: backend/data。两者都尝试。
$candidates = @(
    "D:\TSP\data\user_data\preferences.json",
    (Join-Path $PSScriptRoot "..\backend\data\user_data\preferences.json")
)
$prefPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $prefPath) { Write-Error "找不到 preferences.json"; exit 1 }

$backup = "$prefPath.bak_freetdx_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Copy-Item $prefPath $backup
Write-Host "已备份: $backup"

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
& $py -c @"
import json, sys
path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    prefs = json.load(f)
for key in ('minute_data_provider', 'full_minute_data_provider', 'depth5_data_provider'):
    prefs[key] = 'freetdx'
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, ensure_ascii=False, indent=2)
print('路由已切换: minute/full_minute/depth5 -> freetdx')
"@ $prefPath
