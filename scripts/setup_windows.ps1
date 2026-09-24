[CmdletBinding()]
param(
    [switch]$DemoOnly,
    [switch]$SkipBrowserInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$CommandArgs
    )
    & $Exe @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE: $Exe $($CommandArgs -join ' ')"
    }
}

function Get-CompatiblePython {
    $candidates = @()

    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($minor in 13, 12, 11) {
            $candidates += [pscustomobject]@{
                Exe = "py"
                Prefix = @("-3.$minor")
                Label = "Python Launcher 3.$minor"
            }
        }
    }

    if ($env:LOCALAPPDATA) {
        foreach ($minor in 13, 12, 11) {
            $path = Join-Path $env:LOCALAPPDATA "Programs\Python\Python3$minor\python.exe"
            if (Test-Path $path) {
                $candidates += [pscustomobject]@{ Exe = $path; Prefix = @(); Label = $path }
            }
        }
    }

    if ($env:ProgramFiles) {
        foreach ($minor in 13, 12, 11) {
            $path = Join-Path $env:ProgramFiles "Python3$minor\python.exe"
            if (Test-Path $path) {
                $candidates += [pscustomobject]@{ Exe = $path; Prefix = @(); Label = $path }
            }
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Exe = "python"; Prefix = @(); Label = "python from PATH" }
    }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Exe = "python3"; Prefix = @(); Label = "python3 from PATH" }
    }

    foreach ($candidate in $candidates) {
        try {
            $probeArgs = @($candidate.Prefix) + @(
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
            )
            $versionText = (& $candidate.Exe @probeArgs 2>$null | Select-Object -Last 1)
            if ($LASTEXITCODE -ne 0 -or -not $versionText) {
                continue
            }
            $version = [Version]$versionText.Trim()
            if ($version.Major -eq 3 -and $version.Minor -ge 11) {
                return [pscustomobject]@{
                    Exe = $candidate.Exe
                    Prefix = @($candidate.Prefix)
                    Label = $candidate.Label
                    Version = $version
                }
            }
        }
        catch {
            continue
        }
    }
    return $null
}

$Python = Get-CompatiblePython
if (-not $Python) {
    Write-Host ""
    Write-Host "No Python 3.11+ installation was found." -ForegroundColor Red
    Write-Host "Install Python 3.12, reopen the terminal, then run this script again:" -ForegroundColor Yellow
    Write-Host "  winget install -e --id Python.Python.3.12"
    Write-Host ""
    Write-Host "The Python Launcher (py.exe) is optional."
    exit 2
}

Write-Host "Using $($Python.Label): Python $($Python.Version)" -ForegroundColor Green
if ($Python.Version.Minor -ge 14) {
    Write-Warning "Python $($Python.Version) is not yet part of this project's CI matrix. If Laya/Torch installation fails, install Python 3.12 with: winget install -e --id Python.Python.3.12"
}

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $venvArgs = @($Python.Prefix) + @("-m", "venv", ".venv")
    Invoke-Checked -Exe $Python.Exe -CommandArgs $venvArgs
}
else {
    Write-Host "Reusing existing .venv" -ForegroundColor Cyan
}

Invoke-Checked -Exe $VenvPython -CommandArgs @("-m", "pip", "install", "--upgrade", "pip")

$extra = if ($DemoOnly) { ".[dev]" } else { ".[dev,laya]" }
try {
    Invoke-Checked -Exe $VenvPython -CommandArgs @("-m", "pip", "install", "-e", $extra)
}
catch {
    if ($Python.Version.Minor -ge 14 -and -not $DemoOnly) {
        Write-Host ""
        Write-Host "Laya/Torch installation failed under Python $($Python.Version)." -ForegroundColor Yellow
        Write-Host "Install Python 3.12 and rerun this script:" -ForegroundColor Yellow
        Write-Host "  winget install -e --id Python.Python.3.12"
    }
    throw
}

if (-not $SkipBrowserInstall) {
    Invoke-Checked -Exe $VenvPython -CommandArgs @("-m", "playwright", "install", "chromium")
}

Invoke-Checked -Exe $VenvPython -CommandArgs @("-m", "laya_runtime.cli", "doctor")

Write-Host ""
Write-Host "Windows setup completed." -ForegroundColor Green
if ($DemoOnly) {
    Write-Host "Start the demo chain with:"
    Write-Host "  .\.venv\Scripts\agentctl.exe serve --policy demo"
}
else {
    Write-Host "Start the Laya-first service with:"
    Write-Host "  .\.venv\Scripts\agentctl.exe serve"
}
Write-Host "No venv activation is required."
