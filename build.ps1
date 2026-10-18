# 一键打包为 exe（GUI 版 + 命令行版）
# 用法：powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$py = "C:\Program Files\Python312\python.exe"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "assets\app.ico")) {
    & $py "_make_icon.py"
}

$common = @(
    "--noconfirm", "--onefile", "--icon", "assets\app.ico",
    "--add-data", "assets\app.ico;.",
    "--hidden-import", "gui",
    "--hidden-import", "pystray", "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL.Image", "--hidden-import", "PIL.ImageDraw",
    "--hidden-import", "PIL.ImageTk",
    "--hidden-import", "playwright", "--hidden-import", "playwright.sync_api",
    "--collect-all", "playwright"
)

Write-Host "== 1/2 打包图形界面版 ==" -ForegroundColor Cyan
& $py -m PyInstaller @common --clean --windowed --name "NjtechAutologin" "main.py"

Write-Host "== 2/2 打包命令行版 ==" -ForegroundColor Cyan
& $py -m PyInstaller @common --console --name "NjtechAutologin-CLI" "main.py"

Write-Host "`n完成，产物：" -ForegroundColor Green
Get-ChildItem "dist\*.exe" | Select-Object Name, @{n = "大小(MB)"; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table -AutoSize

# 把已有配置与说明一并放到 dist（exe 会读写同级目录的 config.json / logs）
if (Test-Path "config.json") { Copy-Item "config.json" "dist\config.json" -Force }
if (Test-Path "README.md") { Copy-Item "README.md" "dist\README.md" -Force }
Write-Host "已复制 config.json / README.md 到 dist\" -ForegroundColor Green
