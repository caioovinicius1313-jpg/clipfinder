[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Get-Executable {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Names
    )

    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            return $command.Source
        }
    }

    return $null
}

$projectRoot = $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$venvPython = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$backendEnvironmentFile = Join-Path $backendDirectory ".env"

if (-not (Test-Path $venvPython) -or -not (Test-Path $backendEnvironmentFile)) {
    Write-Host "A preparação inicial ainda não foi concluída." -ForegroundColor Yellow
    Write-Host "Execute uma única vez: .\setup-local.ps1"
    exit 1
}

$corepackExecutable = Get-Executable -Names @("corepack.cmd", "corepack")
if ($null -eq $corepackExecutable) {
    $nodeInstallDirectory = Join-Path $env:ProgramFiles "nodejs"
    $corepackCandidate = Join-Path $nodeInstallDirectory "corepack.cmd"
    if (Test-Path $corepackCandidate) {
        $corepackExecutable = $corepackCandidate
    }
}

if ($null -eq $corepackExecutable) {
    throw "Node.js não foi encontrado. Execute .\setup-local.ps1 após instalar o Node.js LTS."
}

function Start-PowerShellWindow {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string]$Command
    )

    $encodedCommand = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($Command))
    Start-Process -FilePath "powershell.exe" -WorkingDirectory $WorkingDirectory -ArgumentList @("-NoExit", "-EncodedCommand", $encodedCommand)
}

$backendCommand = "& '$venvPython' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
$nodeDirectory = Split-Path -Parent $corepackExecutable
$frontendCommand = "`$env:Path = '$nodeDirectory;' + `$env:Path; & '$corepackExecutable' pnpm run dev"

Start-PowerShellWindow -WorkingDirectory $backendDirectory -Command $backendCommand
Start-PowerShellWindow -WorkingDirectory $frontendDirectory -Command $frontendCommand

Write-Host "ClipFinder iniciado sem reinstalar dependências." -ForegroundColor Green
Write-Host "Frontend: http://localhost:3000"
Write-Host "Swagger:  http://localhost:8000/docs"
Write-Host "Para encerrar, feche as duas janelas abertas."
