[CmdletBinding(DefaultParameterSetName='Worker')]
param(
    [Parameter(ParameterSetName='Worker')][ValidatePattern('^[a-p]{32}$')][string]$ExtensionId,
    [ValidateRange(1024,65535)][int]$Port=9537,
    [Parameter(ParameterSetName='Worker')][switch]$CheckOnly,
    [Parameter(ParameterSetName='Worker')][ValidateRange(1,9223372036854775807)][long]$JobId,
    [Parameter(Mandatory,ParameterSetName='Setup')][switch]$CheckSetup,
    [Parameter(Mandatory,ParameterSetName='Guided')][switch]$Guided
)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$python=Join-Path $root '.dist-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) {throw '请先双击「启动稿序.bat」准备项目运行环境。'}
Push-Location -LiteralPath (Join-Path $root 'backend')
try {
    $probe='from distribution.draft_bridge import local_component_ready; import sys; sys.exit(0 if local_component_ready() else 2)'
    & $python -B -c $probe
    if ($LASTEXITCODE -eq 2) {
        if (!$Guided) {throw '草稿桥组件未就绪，请双击「启动知乎草稿助手.bat」按提示安装。'}
        if ((Read-Host '需要为稿序安装 websockets 15.0.1。输入 Y 安装，按回车退出') -ne 'Y') {exit 0}
        if (!(Get-Command uv -ErrorAction SilentlyContinue)) {throw '安装项目组件需要 uv，请先安装 uv 后重开助手。'}
        & uv pip install --python $python 'websockets==15.0.1'
        if ($LASTEXITCODE -ne 0) {throw '草稿桥组件安装失败，请检查上方的 uv 提示。'}
        & $python -B -c $probe
    }
    if ($LASTEXITCODE -ne 0) {throw '项目环境检查失败，请查看上方错误并重新启动稿序。'}
    Write-Host '本机草稿桥组件已就绪。'
    if ($CheckSetup) {exit 0}

    if ($Guided) {
        Write-Host '1  仅核对扩展连接和知乎账号（默认）'
        Write-Host '2  仅处理指定任务已确认的草稿'
        $choice=Read-Host '请选择 1 或 2，直接回车选择 1'
        switch ($choice.Trim()) {
            ''  { $CheckOnly=$true }
            '1' { $CheckOnly=$true }
            '2' {
                $number=Read-Host '请输入管理台「知乎草稿箱」显示的交付任务编号'
                if ($number -notmatch '^[1-9][0-9]*$' -or ![long]::TryParse($number,[ref]$JobId)) {
                    throw '任务编号必须是有效的正整数。'
                }
            }
            default {throw '请选择 1 或 2。'}
        }
    }
    if (!$ExtensionId) {
        $idText=Read-Host '请输入 Chrome 扩展程序管理页中「文章同步助手」的 32 位扩展 ID'
        if ($idText.Trim() -cnotmatch '^[a-p]{32}$') {throw '请输入由小写 a 至 p 组成的 32 位扩展 ID。'}
        $ExtensionId=$idText.Trim()
    }
    $workerArgs=@('-m','distribution.zhihu_draft_worker','--extension-id',$ExtensionId,'--port',"$Port")
    if ($CheckOnly) { $workerArgs+='--check-only' }
    if ($JobId -gt 0) { $workerArgs+=@('--job',"$JobId") }
    & $python @workerArgs
    $result=$LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
