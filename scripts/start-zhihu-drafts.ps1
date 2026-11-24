[CmdletBinding(DefaultParameterSetName='Worker')]
param(
    [Parameter(ParameterSetName='Worker')][ValidatePattern('^[a-p]{32}$')][string]$ExtensionId,
    [ValidateRange(1024,65535)][int]$Port=9537,
    [Parameter(ParameterSetName='Worker')][switch]$CheckOnly,
    [Parameter(ParameterSetName='Worker')][ValidateNotNullOrEmpty()][string]$Database,
    [Parameter(Mandatory,ParameterSetName='Inspect')][ValidateRange(1,9223372036854775807)][long]$InspectJob,
    [Parameter(ParameterSetName='Worker')][ValidateRange(1,9223372036854775807)][long]$JobId,
    [Parameter(Mandatory,ParameterSetName='Setup')][switch]$CheckSetup,
    [Parameter(Mandatory,ParameterSetName='Guided')][switch]$Guided
)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
function Read-ConsoleTarget([long]$number) {
    $endpoint=if ($number -gt 0) {"jobs/$number/platform-draft?launch=1"} else {'health'}
    $req=[Net.HttpWebRequest]::Create("http://127.0.0.1:5419/api/distribution/$endpoint")
    $req.Proxy=$null
    $req.AllowAutoRedirect=$false
    $req.Timeout=5000
    try {$response=$req.GetResponse()} catch [Net.WebException] {
        $response=$_.Exception.Response
        if (!$response) {throw '无法连接本机管理台，请先启动稿序。'}
    }
    try {
        $reader=[IO.StreamReader]::new($response.GetResponseStream(),[Text.Encoding]::UTF8)
        try {$info=$reader.ReadToEnd() | ConvertFrom-Json} finally {$reader.Dispose()}
    } finally {$response.Dispose()}
    if ($info.error) {throw [string]$info.error}
    if ($info.application -ne 'gaoxu' -or !$info.database -or !(Test-Path -LiteralPath $info.database -PathType Leaf)) {
        throw '管理台返回的任务库不可用，请核对本机稿序版本和磁盘。'
    }
    if ($number -gt 0) {
        if ($info.job -ne $number -or $info.sha -cnotmatch '^[a-f0-9]{64}$') {throw '所选任务与管理台返回的冻结稿不一致。'}
        $planned=[DateTimeOffset]::FromUnixTimeSeconds($info.slot).ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd HH:mm')
        Write-Host "任务 #$number　《$($info.title)》　知乎分稿账号 $($info.account)　模拟排期 $planned（北京时间）"
    }
    Write-Host "管理台任务库　$($info.database)"
    return $info
}
if ($InspectJob) {Read-ConsoleTarget $InspectJob | Out-Null;exit 0}
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
    $target=$null
    if ($Database) {
        $Database=(Get-Item -LiteralPath $Database -ErrorAction Stop).FullName
        if (!(Test-Path -LiteralPath $Database -PathType Leaf)) {throw '请选择已有任务库文件。'}
    } else {
        $target=Read-ConsoleTarget $JobId
        $Database=$target.database
    }
    if (!$ExtensionId) {
        $idText=Read-Host '请输入 Chrome 扩展程序管理页中「文章同步助手」的 32 位扩展 ID'
        if ($idText.Trim() -cnotmatch '^[a-p]{32}$') {throw '请输入由小写 a 至 p 组成的 32 位扩展 ID。'}
        $ExtensionId=$idText.Trim()
    }
    $workerArgs=@('-m','distribution.zhihu_draft_worker','--db',$Database,'--extension-id',$ExtensionId,'--port',"$Port")
    if ($CheckOnly) { $workerArgs+='--check-only' }
    if ($JobId -gt 0) { $workerArgs+=@('--job',"$JobId") }
    if ($target -and $target.sha) { $workerArgs+=@('--expected-sha',$target.sha) }
    & $python @workerArgs
    $result=$LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
