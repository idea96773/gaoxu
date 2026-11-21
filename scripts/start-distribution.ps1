param([ValidateRange(1,8)][int]$Concurrency=1)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$python=Join-Path $root '.dist-venv/Scripts/python.exe'
$backend=Join-Path $root 'backend'
$state=Join-Path $root 'data/distribution'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath (Join-Path $root 'frontend/dist/index.html'))) {
    throw '请按 DISTRIBUTION.md 安装隔离依赖并构建管理台。'
}
New-Item -ItemType Directory -Force -Path $state | Out-Null
function Start-Component([string]$Module,[string[]]$Extra) {
    $idFile=Join-Path $state "$Module.pid"
    $old=$null
    if (Test-Path -LiteralPath $idFile) {
        try {$record=Get-Content -Raw -LiteralPath $idFile | ConvertFrom-Json} catch {$record=$null}
        if ($record -and $record.id) {$old=Get-Process -Id $record.id -ErrorAction SilentlyContinue}
        if ($old -and $old.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.start) {
            if ($old.Path -ne $python) {throw "PID 指向其他程序，请检查 $idFile"}
            return $old.Id
        }
    }
    $moduleArg='(?:^|\s)-m\s+'+[regex]::Escape($Module)+'(?:\s|$)'
    $matchesFound=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.ExecutablePath -eq $python -and $_.CommandLine -match $moduleArg
    })
    if ($matchesFound.Count -gt 1) {throw "发现多个 $Module 进程，请核对当前实例。"}
    if ($matchesFound.Count -eq 1) {
        $proc=Get-Process -Id $matchesFound[0].ProcessId
    } else {
        if ($Module -eq 'distribution.api' -and (Get-NetTCPConnection -LocalPort 5419 -State Listen -ErrorAction SilentlyContinue)) {
            throw '5419 端口已被其他进程占用。'
        }
        $parameters=@{
            FilePath=$python; ArgumentList=(@('-m',$Module)+$Extra); WorkingDirectory=$backend
            WindowStyle='Hidden'; PassThru=$true
            RedirectStandardOutput=(Join-Path $state "$Module.out.log")
            RedirectStandardError=(Join-Path $state "$Module.err.log")
        }
        $proc=Start-Process @parameters
    }
    $stamp=$proc.StartTime.ToUniversalTime().Ticks.ToString()
    $tempFile="$idFile.new"
    [IO.File]::WriteAllText($tempFile,(@{id=$proc.Id;start=$stamp} | ConvertTo-Json -Compress))
    if ([IO.File]::Exists($idFile)) {
        [IO.File]::Replace($tempFile,$idFile,[System.Management.Automation.Language.NullString]::Value)
    } else {
        [IO.File]::Move($tempFile,$idFile)
    }
    return $proc.Id
}
$guard=[IO.File]::Open((Join-Path $state 'launcher.lock'),'OpenOrCreate','ReadWrite','None')
try {
    $apiId=Start-Component 'distribution.api' @()
    $workerId=Start-Component 'distribution.engine' @('--concurrency',"$Concurrency")
} finally {$guard.Dispose()}
$ready=$false
for ($i=0;$i -lt 12;$i++) {
    if (!(Get-Process -Id $apiId -ErrorAction SilentlyContinue) -or !(Get-Process -Id $workerId -ErrorAction SilentlyContinue)) {
        throw '服务进程已退出，请查看对应 err.log。'
    }
    try {
        $request=[Net.HttpWebRequest]::Create('http://127.0.0.1:5419/api/distribution/health')
        $request.Proxy=$null
        $request.Timeout=2500
        $request.ReadWriteTimeout=2500
        $response=$request.GetResponse()
        try {
            $reader=[IO.StreamReader]::new($response.GetResponseStream())
            try {$status=$reader.ReadToEnd() | ConvertFrom-Json} finally {$reader.Dispose()}
        } finally {$response.Dispose()}
        if ($status.application -eq 'gaoxu' -and $status.worker_online) {$ready=$true;break}
    } catch {}
    Start-Sleep -Milliseconds 400
}
if (!$ready) {throw "服务或调度器未就绪，请检查 $state 中的 err.log"}
Write-Output 'http://127.0.0.1:5419/#/distribution'
