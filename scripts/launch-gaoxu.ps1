[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$NoBrowser,
    [switch]$Rebuild
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$environmentPath = Join-Path $root '.dist-venv'
$python = Join-Path $environmentPath 'Scripts\python.exe'
$frontend = Join-Path $root 'frontend'
$dist = Join-Path $frontend 'dist'
$requirements = Join-Path $PSScriptRoot 'gaoxu-requirements.txt'

function Test-ConsoleBuild {
    $index = Join-Path $dist 'index.html'
    if (!(Test-Path -LiteralPath $index -PathType Leaf)) { return $false }
    $html = Get-Content -LiteralPath $index -Encoding UTF8 -Raw
    $entry = [regex]::Match($html, '<script\b[^>]*\bsrc="/assets/([\w.-]+\.js)"')
    if (!$entry.Success) { return $false }
    $bundle = Join-Path $dist ('assets\' + $entry.Groups[1].Value)
    if (!(Test-Path -LiteralPath $bundle -PathType Leaf)) { return $false }
    $route = [regex]::Match([IO.File]::ReadAllText($bundle), 'Distribution-[\w-]+\.js')
    return ($route.Success -and (Test-Path -LiteralPath (Join-Path $dist ('assets\' + $route.Value)) -PathType Leaf))
}

if ($CheckOnly -and $Rebuild) { throw '请选择只读检查或重新构建其中一个操作。' }
$consoleReady = Test-ConsoleBuild
if (!$consoleReady -or $Rebuild) {
    if ($CheckOnly) { throw '缺少稿序前端构建，请双击「启动稿序」。' }
    if (!(Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw '构建管理台需要 Node.js 和 npm。安装后重新打开「启动稿序」。' }
}
if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
    if ($CheckOnly) { throw '项目的 Python 环境尚未就绪。' }
    if (Test-Path -LiteralPath $environmentPath) { throw "已有环境缺少 Python，请检查 $environmentPath" }
    if (!(Get-Command uv -ErrorAction SilentlyContinue)) { throw '首次创建 Python 3.12 环境需要 uv，请安装后重新打开「启动稿序」。' }
    & uv venv --python 3.12 $environmentPath
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败，请查看上方 uv 输出。' }
}

Push-Location -LiteralPath (Join-Path $root 'backend')
try {
    # Match the same version pins used by the installer. Import the real API without starting it.
    $probe = @'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    package, expected = line.split('==')
    try:
        actual = version(package)
    except PackageNotFoundError:
        sys.exit(1)
    if actual != expected:
        sys.exit(1)
from distribution import api, engine
'@
    & $python -B -c $probe $requirements
    if ($LASTEXITCODE -ne 0) {
        if ($CheckOnly) { throw '项目 Python 依赖检查失败。' }
        if (!(Get-Command uv -ErrorAction SilentlyContinue)) { throw '修复项目依赖需要 uv，请安装后重试。' }
        & uv pip install --python $python -r $requirements
        if ($LASTEXITCODE -ne 0) { throw '安装管理台依赖失败，请查看上方 uv 输出。' }
        & $python -B -c $probe $requirements
        if ($LASTEXITCODE -ne 0) { throw '依赖安装后仍无法加载程序，请查看上方错误。' }
    }
    $checkLibrary = @'
import json
import os
from pathlib import Path
from distribution.library import library_settings
from distribution.store import DEFAULT_DB

root = Path(library_settings(os.environ.get('DIST_DB') or DEFAULT_DB, os.environ.get('DIST_LIBRARY'))['root'])
print(json.dumps({'path': str(root)}, ensure_ascii=True))
try:
    with os.scandir(root) as entries:
        next(entries, None)
except OSError:
    raise SystemExit(2)
'@
    $libraryInfo = & $python -B -c $checkLibrary
    $libraryResult = $LASTEXITCODE
    if ($libraryResult -notin 0,2) { throw '无法读取文章目录配置。' }
    $libraryPath = ($libraryInfo | ConvertFrom-Json).path
    if ($libraryResult -eq 0) {
        Write-Host "文章目录可读取 | $libraryPath"
    } else {
        Write-Warning "文章目录不可读取 | $libraryPath"
        if ($CheckOnly) { throw '请核对 Easy WeChat 保存位置、DIST_LIBRARY 和目录的读取权限。' }
        Write-Warning '可打开管理台，在「批量转载 → 保存目录设置」中指定 Easy WeChat 保存位置。'
    }
} finally { Pop-Location }

if (!$consoleReady -or $Rebuild) {
    Push-Location -LiteralPath $frontend
    try {
        & npm.cmd ci --include=dev --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw '还原前端依赖失败，请查看上方 npm 输出。' }
        & npm.cmd run build -- --mode distribution
        if ($LASTEXITCODE -ne 0 -or !(Test-ConsoleBuild)) { throw '稿序管理台构建失败。' }
    } finally { Pop-Location }
}
if ($CheckOnly) { Write-Host '管理台构建及 Python 依赖检查通过。'; return }
& (Join-Path $PSScriptRoot 'start-distribution.ps1')
if (!$NoBrowser) { Start-Process 'http://127.0.0.1:5419/#/distribution' }
