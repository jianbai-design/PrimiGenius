param([Parameter(Mandatory=$true)][string]$InstallDirectory, [int]$InstallerProcessId = 0)
$ErrorActionPreference = 'Stop'
try {
    $installRoot = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\') + '\'
    $owned = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $InstallerProcessId -and $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($owned.Count -eq 0) { exit 0 }
    # Request the app's normal close flow; it decides how to drain active jobs.
    # Never kill children or unrelated Python/Node processes, even during updates.
    foreach ($entry in $owned) {
        if ([IO.Path]::GetFileName($entry.ExecutablePath) -ieq 'PrimiGenius.exe') {
            $process = Get-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue
            if ($process) { $null = $process.CloseMainWindow() }
        }
    }
    Write-Output 'Processes from the target installation are still running.'
    exit 2
} catch {
    Write-Output "Cannot verify installation processes: $($_.Exception.Message)"
    exit 3
}
