param(
    [switch]$RunSchemaValidation
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LocalCacheDir = Join-Path $ProjectRoot ".uv-cache"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Host "Project root: $ProjectRoot"
Write-Host "Local uv cache: $LocalCacheDir"

New-Item -ItemType Directory -Force -Path $LocalCacheDir | Out-Null
$env:UV_CACHE_DIR = $LocalCacheDir

if (-not (Test-Path (Join-Path $ProjectRoot ".venv"))) {
    Write-Host "Creating local virtual environment with uv..."
    uv venv --python python
}
else {
    Write-Host "Local virtual environment already exists."
}

$uvSyncSucceeded = $false
try {
    Write-Host "Attempting dependency sync with uv..."
    uv sync --python $PythonExe --default-index https://pypi.org/simple
    $uvSyncSucceeded = $true
}
catch {
    Write-Warning "uv sync failed. Falling back to pip install from the official PyPI index."
}

if (-not $uvSyncSucceeded) {
    Write-Host "Ensuring pip is available in the local virtual environment for fallback install..."
    & $PythonExe -m ensurepip --upgrade | Out-Host
    & $PythonExe -m pip install --index-url https://pypi.org/simple `
        duckdb numpy pyyaml scikit-learn | Out-Host
}

if ($RunSchemaValidation) {
    Write-Host "Running schema validation pipeline..."
    & $PythonExe "$ProjectRoot\scripts\run_schema_validation.py" | Out-Host
}

Write-Host "Environment bootstrap complete."
