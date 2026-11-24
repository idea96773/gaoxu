param(
    [ValidateSet('Status','Enable','Disable')][string]$Action='Status',
    [ValidateSet('Logon','Startup')][string]$Trigger='Logon',
    [ValidateRange(1,8)][int]$Concurrency=1,
    [switch]$AsJson
)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$script=Join-Path $PSScriptRoot 'supervise-distribution.ps1'
$state=Join-Path $root 'data/distribution'
$policy=Join-Path $state 'supervision.policy'
$user=[Security.Principal.WindowsIdentity]::GetCurrent()
$hash=[Security.Cryptography.SHA256]::Create()
try {
    $bytes=$hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()+'|'+$user.User.Value))
} finally {$hash.Dispose()}
$taskName='Gaoxu-'+[BitConverter]::ToString($bytes,0,8).Replace('-','')
$description="稿序后台 | $script | $($user.User.Value)"
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$folder=$service.GetFolder('\')
try {
    $existing=$folder.GetTask($taskName)
} catch {
    if ($_.Exception.GetBaseException().HResult -ne -2147024894) {throw}
    $existing=$null
}
if ($existing -and $existing.Definition.RegistrationInfo.Description -ne $description) {
    throw "同名任务不属于此稿序目录，请先检查任务计划程序：$taskName"
}
if ($Action -eq 'Status') {
    if ($AsJson) {
        [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
        $report=[ordered]@{registered=[bool]$existing;enabled=$false;triggers=@();expected_database=$null}
        if ($existing) {
            $definition=$existing.Definition
            $report.enabled=$existing.Enabled -and (!(Test-Path -LiteralPath $policy) -or [IO.File]::ReadAllText($policy).Trim() -ne 'disabled')
            $report.triggers=@(foreach ($item in $definition.Triggers) {
                if ($item.Enabled) {switch ([int]$item.Type) {8 {'开机'} 9 {'用户登录'} default {'其他触发方式'}}}
            })
            $actionItem=$definition.Actions.Item(1)
            $expectedArgs='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+$script+'" -Concurrency '
            if ($definition.Actions.Count -eq 1 -and $actionItem.Arguments -match ('^'+[regex]::Escape($expectedArgs)+'[1-8]$') -and
                $actionItem.Path -and [IO.Path]::GetFullPath($actionItem.Path) -eq [IO.Path]::GetFullPath((Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe')) -and
                $definition.Principal.UserId -in ($user.Name,$user.User.Value)) {
                $database=[Environment]::GetEnvironmentVariable('DIST_DB','User')
                if (!$database) {$database=[Environment]::GetEnvironmentVariable('DIST_DB','Machine')}
                if (!$database) {$report.expected_database=Join-Path $state 'dry-run.sqlite'}
                elseif ($database -match '^[A-Za-z]:[\\/]') {$report.expected_database=[IO.Path]::GetFullPath($database)}
                elseif (![IO.Path]::IsPathRooted($database) -and !$database.Contains(':')) {
                    $report.expected_database=[IO.Path]::GetFullPath((Join-Path (Join-Path $root 'backend') $database))
                }
            }
        }
        $report | ConvertTo-Json -Compress
        return
    }
    if (!$existing) {Write-Output "尚未为当前用户和目录配置自启动。任务名 $taskName";return}
    Get-ScheduledTask -TaskName $taskName -TaskPath '\' | Select-Object TaskName,State,Triggers
    Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\' | Select-Object LastRunTime,LastTaskResult
    return
}
New-Item -ItemType Directory -Force -Path $state | Out-Null
if ($Action -eq 'Disable') {
    if ($existing) {Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null}
    [IO.File]::WriteAllText($policy,'disabled')
    Write-Output '已禁用自动启动，监督进程将在本轮检查后退出。'
    return
}
if (!(Test-Path -LiteralPath (Join-Path $root '.dist-venv/Scripts/python.exe')) -or
    !(Test-Path -LiteralPath (Join-Path $root 'frontend/dist/index.html'))) {
    throw '请先安装稿序依赖并构建管理台。'
}
$powershell=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$arguments='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+$script+'" -Concurrency '+$Concurrency
$run=New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $root
if ($Trigger -eq 'Startup') {
    $when=New-ScheduledTaskTrigger -AtStartup
    $who=New-ScheduledTaskPrincipal -UserId $user.Name -LogonType S4U -RunLevel Limited
} else {
    $when=New-ScheduledTaskTrigger -AtLogOn -User $user.Name
    $who=New-ScheduledTaskPrincipal -UserId $user.Name -LogonType Interactive -RunLevel Limited
}
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $run -Trigger $when -Principal $who -Settings $settings -Description $description -Force | Out-Null
[IO.File]::WriteAllText($policy,'enabled')
Start-ScheduledTask -TaskName $taskName -TaskPath '\'
Write-Output "已注册 $taskName，触发方式 $Trigger。监督日志位于 $state/supervisor.log"
