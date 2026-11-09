param(
    [Parameter(Mandatory=$true)][ValidateSet('PrepareUpgrade','Protect','Restore','RemoveProgram')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$InstallDirectory,
    [string]$TargetDirectory
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
if ($root -eq [IO.Path]::GetPathRoot($root).TrimEnd('\')) { throw 'Refusing a drive root' }
$backup = $root + '.primigenius-preserved'
# Only these shipped entries belong to the uninstaller. Everything else is user data.
$programEntries = @('PrimiGenius.exe','resources','locales','swiftshader',
    'chrome_100_percent.pak','chrome_200_percent.pak','d3dcompiler_47.dll',
    'ffmpeg.dll','icudtl.dat','libEGL.dll','libGLESv2.dll','LICENSE.electron.txt',
    'LICENSES.chromium.html','resources.pak','snapshot_blob.bin','v8_context_snapshot.bin',
    'vk_swiftshader.dll','vk_swiftshader_icd.json','vulkan-1.dll',
    'Uninstall PrimiGenius.exe','uninstallerIcon.ico')
function Assert-Directory($path) {
    if ((Test-Path -LiteralPath $path) -and
        ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing linked directory: $path"
    }
}
Assert-Directory $root
Assert-Directory $backup
function Restore-Data {
    if (-not (Test-Path -LiteralPath $backup)) { return }
    $marker = Join-Path $backup 'owner.txt'
    if (-not (Test-Path -LiteralPath $marker) -or
        (Get-Content -LiteralPath $marker -Raw -Encoding UTF8).Trim() -ne $root) {
        throw "Unrecognized recovery directory: $backup"
    }
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $data = Join-Path $backup 'data'
    Assert-Directory $data
    if (Test-Path -LiteralPath $data) {
        foreach ($entry in @(Get-ChildItem -LiteralPath $data -Force)) {
            $destination = Join-Path $root $entry.Name
            if (Test-Path -LiteralPath $destination) {
                throw "Recovery conflict; original data retained at $($entry.FullName)"
            }
            Move-Item -LiteralPath $entry.FullName -Destination $destination
        }
        Remove-Item -LiteralPath $data
    }
    Remove-Item -LiteralPath $marker
    Remove-Item -LiteralPath $backup
}
if ($Mode -eq 'Restore') { Restore-Data; exit 0 }
if ($Mode -in @('PrepareUpgrade','Protect')) {
    if ($TargetDirectory -and
        [IO.Path]::GetFullPath($TargetDirectory).TrimEnd('\') -ne $root) {
        throw 'Upgrade must use the existing installation directory to preserve plugins and WSL storage paths.'
    }
    Restore-Data # Resume an interrupted upgrade before starting another.
    # In-place upgrades overwrite shipped files. Never move a live WSL disk,
    # package lock, plugin directory or log database merely to update the app.
    if ($Mode -eq 'PrepareUpgrade') { exit 0 }
    if (-not (Test-Path -LiteralPath $root)) { exit 0 }
    $entries = @(Get-ChildItem -LiteralPath $root -Force | Where-Object { $_.Name -notin $programEntries })
    if ($entries.Count -eq 0) { exit 0 }
    New-Item -ItemType Directory -Path $backup | Out-Null
    [IO.File]::WriteAllText((Join-Path $backup 'owner.txt'), $root)
    $data = Join-Path $backup 'data'
    New-Item -ItemType Directory -Path $data | Out-Null
    try {
        foreach ($entry in $entries) {
            # Same-volume rename; no copying of libraries or virtual disks.
            Move-Item -LiteralPath $entry.FullName -Destination (Join-Path $data $entry.Name)
        }
    } catch {
        Restore-Data
        throw
    }
    Write-Output "User data protected outside the old uninstaller scope: $backup"
    exit 0
}
foreach ($name in $programEntries) {
    $path = [IO.Path]::GetFullPath((Join-Path $root $name))
    if (-not $path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe removal path' }
    if (Test-Path -LiteralPath $path) {
        Assert-Directory $path
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
# Remove only an empty installation directory.
if ((Test-Path -LiteralPath $root) -and @(Get-ChildItem -LiteralPath $root -Force).Count -eq 0) {
    Remove-Item -LiteralPath $root
}
