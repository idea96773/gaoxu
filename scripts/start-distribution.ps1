param([ValidateRange(1,8)][int]$Concurrency=1,[ValidateRange(1,2)][int]$PreparationConcurrency=1,[switch]$RefreshCode,[switch]$RetireOnly,[IO.FileStream]$LauncherLock,[string]$RequiredDatabase)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$python=Join-Path $root '.dist-venv/Scripts/python.exe'
$backend=Join-Path $root 'backend'
$state=Join-Path $root 'data/distribution'
if (!(Test-Path -LiteralPath $python) -or (!$RetireOnly -and !(Test-Path -LiteralPath (Join-Path $root 'frontend/dist/index.html')))) {
    throw '请按 DISTRIBUTION.md 安装隔离依赖并构建管理台。'
}
New-Item -ItemType Directory -Force -Path $state | Out-Null
$releaseScript=Join-Path $PSScriptRoot 'gaoxu_release.py'
$expectedBuild=$null
function Get-BackendBuild {
    $value=& $python -B $releaseScript backend-id
    if ($LASTEXITCODE -ne 0 -or $value -notmatch '^[a-f0-9]{64}$') {throw '无法核对后台源码版本。'}
    return $value
}
if ($RefreshCode) {$expectedBuild=Get-BackendBuild}
function Start-Component([string]$Module,[string[]]$Extra,[switch]$Inspect) {
    $idFile=Join-Path $state "$Module.pid"
    $old=$null
    if (Test-Path -LiteralPath $idFile) {
        try {$record=Get-Content -Raw -LiteralPath $idFile | ConvertFrom-Json} catch {$record=$null}
        if ($record -and $record.id) {$old=Get-Process -Id $record.id -ErrorAction SilentlyContinue}
        if ($old -and $old.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.start) {
            if ($old.Path -ne $python) {throw "PID 指向其他程序，请检查 $idFile"}
            if (!$Inspect) {return $old.Id}
            if ($record.instance -notmatch '^[a-f0-9]{32}$') {
                throw "正在运行的旧版 $Module 缺少升级标识。完成当前操作后重启 Windows，再双击「启动稿序」。"
            }
            $marker=Join-Path $state ("reload-"+$record.instance)
            if (!$RetireOnly -and $record.build -eq $expectedBuild -and !(Test-Path -LiteralPath $marker)) {return}
            return [pscustomobject]@{Module=$Module;Marker=$marker;Process=$old}
        }
        if ($RefreshCode -and $Module -eq 'distribution.engine' -and
            $record.concurrency -in (1..8) -and $record.preparation -in (1..2)) {
            $Concurrency=[int]$record.concurrency
            $PreparationConcurrency=[int]$record.preparation
            $Extra=@('--concurrency',"$Concurrency",'--preparation-concurrency',"$PreparationConcurrency")
        }
    }
    $moduleArg='(?:^|\s)-m\s+'+[regex]::Escape($Module)+'(?:\s|$)'
    $matchesFound=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.ExecutablePath -eq $python -and $_.CommandLine -match $moduleArg
    })
    if ($matchesFound.Count -gt 1) {throw "发现多个 $Module 进程，请核对当前实例。"}
    if ($matchesFound.Count -eq 1) {
        if ($Inspect -or $RefreshCode) {throw "发现另行启动的 $Module，请在其完成当前工作并退出后再启动新版。"}
        $proc=Get-Process -Id $matchesFound[0].ProcessId
        $instance=$null
        $build=$null
    } else {
        if ($Inspect) {return}
        if ($Module -eq 'distribution.api' -and (Get-NetTCPConnection -LocalPort 5419 -State Listen -ErrorAction SilentlyContinue)) {
            throw '5419 端口已被其他进程占用。'
        }
        $parameters=@{
            FilePath=$python; ArgumentList=(@('-m',$Module)+$Extra); WorkingDirectory=$backend
            WindowStyle='Hidden'; PassThru=$true
            RedirectStandardOutput=(Join-Path $state "$Module.out.log")
            RedirectStandardError=(Join-Path $state "$Module.err.log")
        }
        $build=if ($expectedBuild) {$expectedBuild} else {Get-BackendBuild}
        $instance=[Guid]::NewGuid().ToString('N')
        $previousMarker=$env:GAOXU_RELOAD_FILE
        $previousBuild=$env:GAOXU_BUILD
        try {
            $env:GAOXU_RELOAD_FILE=Join-Path $state ("reload-"+$instance)
            $env:GAOXU_BUILD=$build
            $proc=Start-Process @parameters
        } finally {
            $env:GAOXU_RELOAD_FILE=$previousMarker
            $env:GAOXU_BUILD=$previousBuild
        }
    }
    $stamp=$proc.StartTime.ToUniversalTime().Ticks.ToString()
    $tempFile="$idFile.new"
    $identity=@{id=$proc.Id;start=$stamp;build=$build;instance=$instance}
    if ($Module -eq 'distribution.engine') {
        $identity.concurrency=$Concurrency
        $identity.preparation=$PreparationConcurrency
    }
    [IO.File]::WriteAllText($tempFile,($identity | ConvertTo-Json -Compress))
    if ([IO.File]::Exists($idFile)) {
        [IO.File]::Replace($tempFile,$idFile,[System.Management.Automation.Language.NullString]::Value)
    } else {
        [IO.File]::Move($tempFile,$idFile)
    }
    return $proc.Id
}
$guard=$null
if (!$LauncherLock) {
    $guard=[IO.File]::Open((Join-Path $state 'launcher.lock'),'OpenOrCreate','ReadWrite','None')
}
try {
    if ($RequiredDatabase) {
        if (!(Test-Path -LiteralPath $RequiredDatabase -PathType Leaf) -or !(Test-Path -LiteralPath ($RequiredDatabase+'.simulator') -PathType Leaf)) {
            throw '绑定的任务库或回执库不可读取，请检查原磁盘和文件。'
        }
        $env:DIST_DB=$RequiredDatabase
        if (Get-NetTCPConnection -LocalPort 5419 -State Listen -ErrorAction SilentlyContinue) {
            $probe=New-Object Net.WebClient
            try {
                $probe.Proxy=$null
                $live=$probe.DownloadString('http://127.0.0.1:5419/api/distribution/health') | ConvertFrom-Json
            } finally {$probe.Dispose()}
            if ($live.application -ne 'gaoxu' -or $live.database -ne $RequiredDatabase) {
                throw '正在运行的管理台使用其他任务库或版本过旧，请核对后再启动绑定的任务库。'
            }
        }
    }
    if ($RefreshCode -or $RetireOnly) {
        $prior=@(foreach ($component in 'distribution.api','distribution.engine') {
            Start-Component -Module $component -Inspect
        })
        foreach ($entry in $prior) {[IO.File]::WriteAllText($entry.Marker,'reload')}
        foreach ($entry in $prior) {
            if (!$entry.Process.WaitForExit(3000)) {
                Write-Host "$($entry.Module) 正在完成已领取的工作，完成后将自动启动新版。"
                while (!$entry.Process.WaitForExit(1000)) { }
            }
        }
        if ($RetireOnly) {return}
    }
    $apiId=Start-Component 'distribution.api' @()
    $workerId=Start-Component 'distribution.engine' @('--concurrency',"$Concurrency",'--preparation-concurrency',"$PreparationConcurrency")
} finally {if ($guard) {$guard.Dispose()}}
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
        $versionsMatch=!$RefreshCode -or ($status.api_build -eq $expectedBuild -and $status.worker_build -eq $expectedBuild)
        if ($status.application -eq 'gaoxu' -and $status.worker_online -and $versionsMatch -and
            (!$RequiredDatabase -or $status.database -eq $RequiredDatabase)) {$ready=$true;break}
    } catch {}
    Start-Sleep -Milliseconds 400
}
if (!$ready) {throw "服务或调度器未就绪，请检查 $state 中的 err.log"}
if ($RefreshCode -and (Get-BackendBuild) -ne $expectedBuild) {throw '启动期间后台源码发生变化，请再次启动稿序。'}
Write-Output 'http://127.0.0.1:5419/#/distribution'
