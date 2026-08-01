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

function Stop-WithPrerequisiteMessage {
    param([string]$Message)

    Write-Host "`n$Message" -ForegroundColor Red
    Write-Host "Instale o programa, feche este PowerShell, abra outro e execute este arquivo novamente." -ForegroundColor Yellow
    exit 1
}

$projectRoot = $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$storageDirectory = Join-Path $projectRoot "storage"
$venvDirectory = Join-Path $backendDirectory ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$backendEnvironmentFile = Join-Path $backendDirectory ".env"

$pythonLauncher = Get-Executable -Names @("py.exe", "py")
if ($null -ne $pythonLauncher) {
    $pythonExecutable = $pythonLauncher
    $pythonVersionArgument = @("-3.12")
} else {
    $pythonExecutable = Get-Executable -Names @("python.exe", "python")
    $pythonVersionArgument = @()
}

if ($null -eq $pythonExecutable) {
    Stop-WithPrerequisiteMessage "Python 3.12 não foi encontrado. Instale-o em https://www.python.org/downloads/"
}

$nodeInstallDirectory = Join-Path $env:ProgramFiles "nodejs"
$corepackExecutable = Get-Executable -Names @("corepack.cmd", "corepack")
if ($null -eq $corepackExecutable -and (Test-Path (Join-Path $nodeInstallDirectory "corepack.cmd"))) {
    $corepackExecutable = Join-Path $nodeInstallDirectory "corepack.cmd"
}

if ($null -eq $corepackExecutable) {
    Stop-WithPrerequisiteMessage "Node.js 20 ou superior não foi encontrado. Instale-o em https://nodejs.org/"
}

$psqlExecutable = Get-Executable -Names @("psql.exe", "psql")
if ($null -eq $psqlExecutable -and (Test-Path "$env:ProgramFiles\PostgreSQL")) {
    $psqlExecutable = (
        Get-ChildItem "$env:ProgramFiles\PostgreSQL" -Filter "psql.exe" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        Select-Object -First 1 -ExpandProperty FullName
    )
}

if ($null -eq $psqlExecutable) {
    Stop-WithPrerequisiteMessage "PostgreSQL não foi encontrado. Instale-o em https://www.postgresql.org/download/windows/"
}

Write-Host "Preparando o ClipFinder. Isso é necessário apenas uma vez..." -ForegroundColor Cyan

if (-not (Test-Path $venvPython)) {
    & $pythonExecutable @pythonVersionArgument -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível criar o ambiente Python. Verifique se o Python 3.12 está instalado."
    }
}

& $venvPython -m pip install -r (Join-Path $backendDirectory "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Não foi possível instalar as dependências do backend."
}

& $venvPython -c "import pydantic_core"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Reparando o ambiente Python..." -ForegroundColor Yellow
    & $venvPython -m pip install --force-reinstall --no-cache-dir -r (Join-Path $backendDirectory "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível reparar as dependências do backend."
    }
}

Push-Location $frontendDirectory
try {
    $env:Path = "$nodeInstallDirectory;" + $env:Path
    & $corepackExecutable pnpm install
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível instalar as dependências do frontend."
    }
} finally {
    Pop-Location
}

$securePassword = Read-Host "Digite a senha criada na instalação do PostgreSQL para o usuário postgres" -AsSecureString
$postgresPassword = [System.Net.NetworkCredential]::new("", $securePassword).Password
if ([string]::IsNullOrWhiteSpace($postgresPassword)) {
    throw "A senha do PostgreSQL não pode ficar vazia."
}

$databaseName = "clipfinder"
$encodedPassword = [System.Uri]::EscapeDataString($postgresPassword)
$databaseUrl = "postgresql+psycopg://postgres:$encodedPassword@localhost:5432/$databaseName"
$env:PGPASSWORD = $postgresPassword
$youtubeApiKey = ""
if (Test-Path $backendEnvironmentFile) {
    $existingYoutubeSetting = Get-Content $backendEnvironmentFile | Where-Object { $_ -match "^YOUTUBE_API_KEY=" } | Select-Object -First 1
    if ($null -ne $existingYoutubeSetting) {
        $youtubeApiKey = $existingYoutubeSetting.Substring("YOUTUBE_API_KEY=".Length)
    }
}

$existingDatabase = & $psqlExecutable -h "localhost" -p "5432" -U "postgres" -d "postgres" -tAc "SELECT 1 FROM pg_database WHERE datname = '$databaseName'"
if ($LASTEXITCODE -ne 0) {
    throw "Não foi possível conectar ao PostgreSQL. Confirme a senha e se o serviço PostgreSQL está em execução."
}

if ((($existingDatabase -join "").Trim()) -ne "1") {
    & $psqlExecutable -h "localhost" -p "5432" -U "postgres" -d "postgres" -v "ON_ERROR_STOP=1" -c "CREATE DATABASE $databaseName"
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível criar o banco clipfinder."
    }
}

$backendEnvironmentContent = @(
    "DATABASE_URL=$databaseUrl"
    "STORAGE_PATH=$storageDirectory"
    "CORS_ORIGINS=http://localhost:3000"
    "YOUTUBE_API_KEY=$youtubeApiKey"
) -join [Environment]::NewLine

[System.IO.File]::WriteAllText(
    $backendEnvironmentFile,
    $backendEnvironmentContent,
    (New-Object System.Text.UTF8Encoding($false))
)

Push-Location $backendDirectory
try {
    & $venvPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível preparar o banco de dados."
    }
} finally {
    Pop-Location
}

Write-Host "Preparação concluída." -ForegroundColor Green
Write-Host "Nas próximas vezes, execute somente: .\start-local.ps1"
& (Join-Path $projectRoot "start-local.ps1")
