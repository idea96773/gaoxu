param([ValidateRange(1,8)][int]$Concurrency=1,[string]$DatabaseUtf8)
$ErrorActionPreference='Stop'
$state=Join-Path (Split-Path $PSScriptRoot -Parent) 'data/distribution'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$owner=[IO.File]::Open((Join-Path $state 'supervisor.lock'),'OpenOrCreate','ReadWrite','None')
$log=Join-Path $state 'supervisor.log'
$delay=15
$previous=''
$policy=Join-Path $state 'supervision.policy'
if ($DatabaseUtf8) {
    $env:DIST_DB=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($DatabaseUtf8))
}
function Get-Enabled {
    return !(Test-Path -LiteralPath $policy) -or ([IO.File]::ReadAllText($policy).Trim() -ne 'disabled')
}
try {
    while (Get-Enabled) {
        try {
            $options=@{Concurrency=$Concurrency}
            if ($DatabaseUtf8) {$options.RequiredDatabase=$env:DIST_DB}
            & (Join-Path $PSScriptRoot 'start-distribution.ps1') @options | Out-Null
            $message='管理台与调度器在线'
            $delay=15
        } catch {
            $message=$_.Exception.Message
            $delay=[Math]::Min(300,$delay*2)
        }
        if ($message -ne $previous) {
            if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -ge 1MB) {
                Copy-Item -LiteralPath $log -Destination "$log.previous" -Force
                [IO.File]::WriteAllText($log,'')
            }
            Add-Content -LiteralPath $log -Value "$(Get-Date -Format o) $message" -Encoding UTF8
            $previous=$message
        }
        for ($elapsed=0;$elapsed -lt $delay -and (Get-Enabled);$elapsed+=5) {
            Start-Sleep -Seconds 5
        }
    }
} finally {$owner.Dispose()}
