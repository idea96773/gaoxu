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
    if (Test-Path -LiteralPath $idFile) {
        $record=Get-Content -Raw -LiteralPath $idFile | ConvertFrom-Json
        $old=Get-Process -Id $record.id -ErrorAction SilentlyContinue
        if ($old -and $old.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.start) {return}
    }
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
    $stamp=$proc.StartTime.ToUniversalTime().Ticks.ToString()
    @{id=$proc.Id;start=$stamp} | ConvertTo-Json -Compress | Set-Content -LiteralPath $idFile -Encoding ascii
}
Start-Component 'distribution.api' @()
Start-Component 'distribution.engine' @('--concurrency',"$Concurrency")
$ready=$false
$probe=New-Object System.Net.WebClient
$probe.Proxy=$null
for ($i=0;$i -lt 20;$i++) {
    try {
        $status=$probe.DownloadString('http://127.0.0.1:5419/api/distribution/batches') | ConvertFrom-Json
        $age=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-$status.worker_seen
        if ($status.mode -eq 'dry-run' -and $status.worker_seen -and $age -lt 18) {$ready=$true;break}
    } catch {}
    Start-Sleep -Milliseconds 400
}
$probe.Dispose()
if (!$ready) {throw "服务或调度器未就绪，请检查 $state 中的 err.log"}
Write-Output 'http://127.0.0.1:5419/#/distribution'
