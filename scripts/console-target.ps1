# Shared by the interactive launcher and the scheduled supervisor.
function Get-GaoxuDatabase([string]$ProjectRoot, [string]$Database) {
    if (!$Database) { $Database = 'data/distribution/dry-run.sqlite' }
    elseif (![IO.Path]::IsPathRooted($Database)) {
        # Both Python services run with backend as their working directory.
        $Database = Join-Path 'backend' $Database
    }
    return [IO.Path]::GetFullPath([IO.Path]::Combine($ProjectRoot, $Database))
}

function Read-GaoxuConsole([string]$ExpectedDatabase) {
    $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:5419/api/distribution/health')
    $request.Proxy = $null
    $request.Timeout = 2500
    $request.ReadWriteTimeout = 2500
    try {
        $response = $request.GetResponse()
        try {
            $reader = [IO.StreamReader]::new($response.GetResponseStream())
            try { $status = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
        } finally { $response.Dispose() }
    } catch {
        throw '无法核对 5419 端口上的管理台，请检查正在运行的程序后重试。'
    }
    if ($status.application -ne 'gaoxu' -or !$status.database) {
        throw '5419 端口上的服务未提供稿序任务库信息，请核对正在运行的程序。'
    }
    if ($status.database -ne $ExpectedDatabase) {
        throw "管理台正在使用其他任务库。当前运行 | $($status.database)；本次启动 | $ExpectedDatabase。请从原项目打开管理台，或在原实例正常退出后切换任务库。"
    }
    return $status
}
