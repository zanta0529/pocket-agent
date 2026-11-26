# PocketAgent Windows Installer (PowerShell)
# Usage: irm https://raw.githubusercontent.com/zanta0529/pocket-agent/main/build_scripts/install.ps1 | iex
param(
    [switch]$Uninstall,
    [string]$Version = "",
    [string]$InstallDir = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = if ($env:POCKET_AGENT_REPO) { $env:POCKET_AGENT_REPO.Trim() } else { "zanta0529/pocket-agent" }
if ($Repo -notmatch "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") {
    throw "Invalid POCKET_AGENT_REPO value: '$Repo'"
}

if (-not $Version) {
    $Version = if ($env:POCKET_AGENT_VERSION) { $env:POCKET_AGENT_VERSION.Trim() } else { "latest" }
}
if ($Version -ne "latest" -and $Version -notmatch "^[A-Za-z0-9_.-]+$") {
    throw "Invalid POCKET_AGENT_VERSION value: '$Version'"
}

if (-not $InstallDir) {
    $InstallDir = if ($env:POCKET_AGENT_HOME) {
        $env:POCKET_AGENT_HOME.Trim()
    } else {
        Join-Path $env:LOCALAPPDATA "PocketAgent"
    }
}
$InstallDir = $InstallDir.TrimEnd("\")

$RequireChecksum = ($env:POCKET_AGENT_REQUIRE_CHECKSUM -eq "1")
$DoUninstall = $Uninstall.IsPresent -or ($env:POCKET_AGENT_UNINSTALL -eq "1")

function Test-IsPocketAgentDir {
    param([string]$DirPath)
    if (-not (Test-Path -LiteralPath $DirPath -PathType Container)) {
        return $false
    }
    $hasBat = Test-Path -LiteralPath (Join-Path $DirPath "pocket.bat") -PathType Leaf
    $hasPortable = Test-Path -LiteralPath (Join-Path $DirPath "app\PORTABLE") -PathType Leaf
    return ($hasBat -or $hasPortable)
}

function Normalize-PathEntry {
    param([string]$Entry)
    return $Entry.Trim().TrimEnd("\").ToLowerInvariant()
}

function Add-UserPathEntry {
    param([string]$BinDir)
    $normTarget = Normalize-PathEntry $BinDir
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @()
    if ($userPath) {
        $entries = @($userPath -split ";" | Where-Object { $_ -and $_.Trim() -ne "" })
    }
    $exists = $false
    foreach ($item in $entries) {
        if ((Normalize-PathEntry $item) -eq $normTarget) {
            $exists = $true
            break
        }
    }
    if (-not $exists) {
        $newEntries = $entries + @($BinDir)
        [Environment]::SetEnvironmentVariable("Path", ($newEntries -join ";"), "User")
        Write-Host "[INFO] Added '$BinDir' to User PATH."
    }
    $procEntries = @($env:Path -split ";" | Where-Object { $_ -and $_.Trim() -ne "" })
    $inProc = $false
    foreach ($item in $procEntries) {
        if ((Normalize-PathEntry $item) -eq $normTarget) {
            $inProc = $true
            break
        }
    }
    if (-not $inProc) {
        $env:Path = "$BinDir;$env:Path"
    }
}

function Remove-UserPathEntry {
    param([string]$BinDir)
    $normTarget = Normalize-PathEntry $BinDir
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) {
        return
    }
    $filtered = @(
        $userPath -split ";" |
        Where-Object { $_ -and $_.Trim() -ne "" -and (Normalize-PathEntry $_) -ne $normTarget }
    )
    [Environment]::SetEnvironmentVariable("Path", ($filtered -join ";"), "User")
    Write-Host "[INFO] Removed '$BinDir' from User PATH."
}

$BinDir = Join-Path $InstallDir "bin"

if ($DoUninstall) {
    Remove-UserPathEntry $BinDir
    if (Test-Path -LiteralPath $InstallDir) {
        if (-not (Test-IsPocketAgentDir $InstallDir)) {
            throw "Refusing to remove '$InstallDir': directory does not look like a PocketAgent installation."
        }
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
        Write-Host "[OK] Uninstalled PocketAgent from '$InstallDir'."
    } else {
        Write-Host "[INFO] PocketAgent is not installed at '$InstallDir'."
    }
    Write-Host "[INFO] User data in '$env:USERPROFILE\.pocket-agent' was preserved."
    return
}

if ($Version -eq "latest") {
    $BaseUrl = "https://github.com/$Repo/releases/latest/download"
} else {
    $BaseUrl = "https://github.com/$Repo/releases/download/$Version"
}

$AssetName = "PocketAgent-portable.zip"
$ZipUrl = "$BaseUrl/$AssetName"
$SumsUrl = "$BaseUrl/SHA256SUMS.txt"

$TempRoot = Join-Path $env:TEMP ("pocket-agent-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null

$ParentDir = Split-Path -Parent $InstallDir
if ($ParentDir -and -not (Test-Path -LiteralPath $ParentDir -PathType Container)) {
    New-Item -ItemType Directory -Path $ParentDir -Force | Out-Null
}
$StagingRoot = "$InstallDir.staging." + [Guid]::NewGuid().ToString("N").Substring(0, 8)
$PrevProgressPreference = $ProgressPreference

try {
    $ProgressPreference = "SilentlyContinue"
    $ZipFile = Join-Path $TempRoot $AssetName
    $SumsFile = Join-Path $TempRoot "SHA256SUMS.txt"
    $ExtractDir = $StagingRoot

    Write-Host "[INFO] Downloading $AssetName ($Version) from $Repo..."
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipFile -UseBasicParsing

    $HasChecksums = $false
    try {
        Invoke-WebRequest -Uri $SumsUrl -OutFile $SumsFile -UseBasicParsing
        $HasChecksums = Test-Path -LiteralPath $SumsFile -PathType Leaf
    } catch {
        if ($RequireChecksum) {
            throw "SHA256SUMS.txt is required (POCKET_AGENT_REQUIRE_CHECKSUM=1) but failed to download: $($_.Exception.Message)"
        }
        Write-Warning "SHA256SUMS.txt not found for this release; skipping checksum verification."
    }

    if ($HasChecksums) {
        $expectedHash = ""
        foreach ($line in (Get-Content -LiteralPath $SumsFile)) {
            if ($line -match "^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$") {
                if ($Matches[2].Trim() -eq $AssetName) {
                    $expectedHash = $Matches[1].ToLowerInvariant()
                    break
                }
            }
        }
        if (-not $expectedHash) {
            throw "SHA256SUMS.txt does not contain an entry for '$AssetName'."
        }
        $actualHash = (Get-FileHash -Path $ZipFile -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) {
            throw "SHA-256 verification failed! Expected $expectedHash, got $actualHash."
        }
        Write-Host "[INFO] SHA-256 verified ($actualHash)."
    }

    Write-Host "[INFO] Extracting archive..."
    Expand-Archive -LiteralPath $ZipFile -DestinationPath $ExtractDir -Force

    $PayloadDir = Join-Path $ExtractDir "PocketAgent"
    if (-not (Test-Path -LiteralPath (Join-Path $PayloadDir "pocket.bat") -PathType Leaf)) {
        $PayloadDir = $ExtractDir
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PayloadDir "pocket.bat") -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $PayloadDir "python\python.exe") -PathType Leaf)) {
        throw "Extracted archive does not contain a valid PocketAgent portable layout."
    }

    $BackupDir = "$InstallDir.bak." + [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $MovedBackup = $false
    if (Test-Path -LiteralPath $InstallDir) {
        $existingItems = @(Get-ChildItem -LiteralPath $InstallDir -Force)
        if ($existingItems.Count -gt 0 -and -not (Test-IsPocketAgentDir $InstallDir)) {
            throw "Target '$InstallDir' already exists and is not a PocketAgent directory. Set POCKET_AGENT_HOME to another path."
        }
        Move-Item -LiteralPath $InstallDir -Destination $BackupDir -Force
        $MovedBackup = $true
    }

    try {
        Move-Item -LiteralPath $PayloadDir -Destination $InstallDir -Force
    } catch {
        if ($MovedBackup -and (Test-Path -LiteralPath $BackupDir) -and -not (Test-Path -LiteralPath $InstallDir)) {
            Move-Item -LiteralPath $BackupDir -Destination $InstallDir -Force
        }
        throw
    }

    if ($MovedBackup -and (Test-Path -LiteralPath $BackupDir)) {
        Remove-Item -LiteralPath $BackupDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    $LauncherCmd = "@echo off`r`nsetlocal`r`nset `"PA_NO_PAUSE=1`"`r`ncall `"%~dp0..\pocket.bat`" %*`r`nendlocal & exit /b %ERRORLEVEL%`r`n"
    [System.IO.File]::WriteAllText((Join-Path $BinDir "pocket-agent.cmd"), $LauncherCmd, [System.Text.Encoding]::ASCII)

    Add-UserPathEntry $BinDir

    Write-Host ""
    Write-Host "[OK] PocketAgent installed to '$InstallDir'."
    Write-Host "Next steps:"
    Write-Host "  1. Run: pocket-agent setup"
    Write-Host "  2. Run: pocket-agent"
    Write-Host "To uninstall later: `$env:POCKET_AGENT_UNINSTALL=`"1`"; irm https://raw.githubusercontent.com/$Repo/main/build_scripts/install.ps1 | iex"
} finally {
    $ProgressPreference = $PrevProgressPreference
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
