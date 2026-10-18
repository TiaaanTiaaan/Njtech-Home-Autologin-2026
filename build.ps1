# 一键打包：精简版 GUI / 完整版 GUI / 命令行版
# 用法：powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$py = "C:\Program Files\Python312\python.exe"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "assets\app.ico")) {
    & $py "_make_icon.py"
}

$base = @(
    "--noconfirm", "--onefile", "--icon", "assets\app.ico",
    "--add-data", "assets\app.ico;.",
    "--hidden-import", "gui",
    "--hidden-import", "pystray", "--hidden-import", "pystray._win32",
    "--hidden-import", "PIL.Image", "--hidden-import", "PIL.ImageDraw",
    "--hidden-import", "PIL.ImageTk"
)
$slim = $base + @("--exclude-module", "playwright")
$full = $base + @(
    "--hidden-import", "playwright", "--hidden-import", "playwright.sync_api",
    "--collect-all", "playwright"
)

Write-Host "== 1/3 精简版 GUI（不含 playwright，体积小）==" -ForegroundColor Cyan
& $py -m PyInstaller @slim --clean --windowed --name "NjtechAutologin" "main.py"

Write-Host "== 2/3 完整版 GUI（含浏览器兜底）==" -ForegroundColor Cyan
& $py -m PyInstaller @full --windowed --name "NjtechAutologin-Full" "main.py"

Write-Host "== 3/3 命令行版（精简）==" -ForegroundColor Cyan
& $py -m PyInstaller @slim --console --name "NjtechAutologin-CLI" "main.py"

Write-Host "`n完成，产物：" -ForegroundColor Green
Get-ChildItem "dist\*.exe" |
    Select-Object Name, @{n = "大小(MB)"; e = { [math]::Round($_.Length / 1MB, 1) } } |
    Format-Table -AutoSize

# 不再把 config.json 复制到 dist（含明文密码，且新版已改到 %APPDATA%\NjtechAutologin）
if (Test-Path "README.md") { Copy-Item "README.md" "dist\README.md" -Force }
Write-Host "提示：exe 的配置与日志保存在 %APPDATA%\NjtechAutologin，可用 --where 查看" -ForegroundColor Green
