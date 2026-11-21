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
$requirements = Join-Path $PSScriptRoot 'gaoxu-requirements.txt'

$releaseScript = Join-Path $PSScriptRoot 'gaoxu_release.py'
$starter = Join-Path $PSScriptRoot 'start-distribution.ps1'

function Invoke-GaoxuLaunch {
    if ($CheckOnly -and $Rebuild) { throw '请选择只读检查或重新构建其中一个操作。' }
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        if ($CheckOnly) { throw '项目的 Python 环境尚未就绪。' }
        if (Test-Path -LiteralPath $environmentPath) { throw "已有环境缺少 Python，请检查 $environmentPath" }
        if (!(Get-Command uv -ErrorAction SilentlyContinue)) { throw '首次创建 Python 3.12 环境需要 uv，请安装后重新打开「启动稿序」。' }
        & uv venv --python 3.12 $environmentPath
        if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败，请查看上方 uv 输出。' }
    }

    [string[]]$buildArgs=if ($CheckOnly) {@('check')} elseif ($Rebuild) {@('prepare','--force')} else {@('prepare')}
    & $python -B $releaseScript @buildArgs
    if ($LASTEXITCODE -ne 0) {throw '管理台构建或版本核对失败，请查看上方提示。'}
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
            & $starter -RetireOnly -LauncherLock $launchLock
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

    if ($CheckOnly) {
        if (Get-NetTCPConnection -LocalPort 5419 -State Listen -ErrorAction SilentlyContinue) {
            $client=New-Object Net.WebClient
            try {$client.Proxy=$null;$health=$client.DownloadString('http://127.0.0.1:5419/api/distribution/health') | ConvertFrom-Json}
            finally {$client.Dispose()}
            $build=& $python -B $releaseScript backend-id
            if ($health.application -ne 'gaoxu' -or !$health.worker_online -or
                $health.api_build -ne $build -or $health.worker_build -ne $build) {
                throw '运行中的管理台或调度器尚未同步到当前源码，请双击「启动稿序」。'
            }
        }
        Write-Host '管理台构建及 Python 依赖检查通过。'
        return
    }
    & $starter -RefreshCode -LauncherLock $launchLock
    & $python -B $releaseScript activate
    if ($LASTEXITCODE -ne 0) {throw '新版网页尚未启用，请查看上方提示后重新启动。'}
    if (!$NoBrowser) { Start-Process 'http://127.0.0.1:5419/#/distribution' }
}

$launchLock=$null
try {
    if (!$CheckOnly) {
        $state=Join-Path $root 'data/distribution'
        New-Item -ItemType Directory -Force -Path $state | Out-Null
        $announced=$false
        while (!$launchLock) {
            try {$launchLock=[IO.File]::Open((Join-Path $state 'launcher.lock'),'OpenOrCreate','ReadWrite','None')}
            catch [IO.IOException] {
                if (($_.Exception.GetBaseException().HResult -band 65535) -notin (32,33)) {throw}
                if (!$announced) {Write-Host '正在等待另一项启动或更新操作完成。';$announced=$true}
                Start-Sleep -Milliseconds 400
            }
        }
    }
    Invoke-GaoxuLaunch
} finally {if ($launchLock) {$launchLock.Dispose()}}
