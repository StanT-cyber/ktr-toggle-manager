$ErrorActionPreference = "Stop"

$PythonExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "未找到 .venv。请先按 README 创建虚拟环境并安装 requirements-dev.txt。"
}

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "Ktr启停管理工具" `
    --icon ".\assets\app_icon.ico" `
    --version-file ".\version_info.txt" `
    --add-data ".\assets\app_icon.ico;assets" `
    ".\ktr_hop_manager.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，退出代码：$LASTEXITCODE"
}

Write-Host "构建完成：dist\Ktr启停管理工具.exe"
