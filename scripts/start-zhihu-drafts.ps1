param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-p]{32}$')][string]$ExtensionId,
    [ValidateRange(1024,65535)][int]$Port=9537
)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$python=Join-Path $root '.dist-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python)) {throw '请先按 DISTRIBUTION.md 安装稿序。'}
& $python -c 'from websockets.sync.server import serve' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw '草稿桥需要 websockets。请在项目根运行 uv pip install --python .dist-venv/Scripts/python.exe websockets==15.0.1'
}
Push-Location (Join-Path $root 'backend')
try {
    & $python -m distribution.zhihu_draft_worker --extension-id $ExtensionId --port $Port
    $result=$LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
