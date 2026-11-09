param([Parameter(Mandatory=$true)][string]$ResultFile)
$ErrorActionPreference = 'Stop'
$deadline = [DateTime]::UtcNow.AddMinutes(30)
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath $ResultFile) {
        $resultLines = @(Get-Content -LiteralPath $ResultFile -Encoding UTF8)
        if ($resultLines.Count -gt 0 -and $resultLines[0] -eq '0') { exit 0 }
        Write-Output ($resultLines -join "`n")
        exit 1
    }
    Start-Sleep -Milliseconds 500
}
Write-Output 'Offline R installation did not finish within 30 minutes. Resources were preserved for retry.'
exit 2
