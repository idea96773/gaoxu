# Interactive setup uses the same task registration and inspection as the supervisor CLI.
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$setup=Join-Path $PSScriptRoot 'windows-startup.ps1'
. (Join-Path $PSScriptRoot 'console-target.ps1')

function Get-Registration {
    return (& $setup -Action Status -AsJson | ConvertFrom-Json)
}

function Assert-SameRegistration {
    if (((Get-Registration) | ConvertTo-Json -Compress) -ne ($before | ConvertTo-Json -Compress)) {
        throw '自启动设置在确认期间发生了变化，请重新打开设置窗口核对。'
    }
}

$before=Get-Registration
Write-Host "稿序项目　$root"
if ($before.registered) {
    $enabled=if ($before.enabled) {'已启用'} else {'已禁用'}
    Write-Host "当前自启动　$enabled　$($before.triggers -join '、')"
    if ($before.expected_database) {Write-Host "配置的任务库　$($before.expected_database)"}
} else {
    Write-Host '当前 Windows 用户尚未为此目录配置自启动。'
}
Write-Host '1  登录当前 Windows 账号后恢复排期'
Write-Host '2  Windows 开机时恢复排期（需要任务注册权限）'
if ($before.registered) {Write-Host '3  禁用此目录的自启动'}
$choice=([string](Read-Host '请选择，直接回车退出')).Trim()
if (!$choice) {return}
if ($choice -notin '1','2','3' -or ($choice -eq '3' -and !$before.registered)) {throw '请选择上方列出的操作。'}
if ($choice -eq '3') {
    if (([string](Read-Host '禁用后，Windows 下次启动时此配置将不再恢复排期。输入 Y 确认')).Trim() -ne 'Y') {return}
    Assert-SameRegistration
    & $setup -Action Disable
    if ((Get-Registration).enabled) {throw '任务计划程序仍显示自启动已启用，请重新检查。'}
    Write-Host '已核对，自启动已禁用。'
    return
}

$live=Read-GaoxuConsole -ExpectedProject $root
$database=$live.database
$trigger=if ($choice -eq '1') {'Logon'} else {'Startup'}
$label=if ($choice -eq '1') {'用户登录'} else {'开机'}
$workers=if ($before.concurrency) {[int]$before.concurrency} else {1}
if ($before.registered -and !$before.concurrency) {
    throw '当前任务已有自定义启动命令，请在 Windows 任务计划程序中核对后调整。'
}
Write-Host "将绑定当前管理台任务库　$database"
Write-Host "Windows 触发方式　$label　　调度并发　$workers"
if ($before.registered) {Write-Host '确认后将更新上面显示的已有注册。'}
if (([string](Read-Host '输入 Y 启用并启动后台监督，直接回车退出')).Trim() -ne 'Y') {return}
$null=Read-GaoxuConsole -ExpectedProject $root -ExpectedDatabase $database
Assert-SameRegistration
& $setup -Action Enable -Trigger $trigger -Concurrency $workers -Database $database
$after=Get-Registration
if (!$after.enabled -or !$after.database_pinned -or $after.expected_database -ne $database -or
    $after.triggers.Count -ne 1 -or $after.triggers[0] -ne $label -or $after.concurrency -ne $workers) {
    throw '任务计划程序中读取的配置与所选设置不一致，请打开管理台重新检查。'
}
Write-Host "已从任务计划程序核对　$label · $database"
