$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$EnvPrefix = if ($env:CONDA_ENV_PREFIX) { $env:CONDA_ENV_PREFIX } else { Join-Path $ProjectRoot ".conda/cad2bim_py311" }
$EnvFile = if ($env:CONDA_ENV_FILE) { $env:CONDA_ENV_FILE } else { Join-Path $ProjectRoot "environment.conda.yml" }

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "conda is not installed or not in PATH."
}

if (-not (Test-Path $EnvFile)) {
    throw "environment file not found: $EnvFile"
}

New-Item -ItemType Directory -Path (Join-Path $ProjectRoot ".conda") -Force | Out-Null

Write-Host "[1/5] Create/update conda env at: $EnvPrefix"
conda env update --prefix "$EnvPrefix" --file "$EnvFile" --prune

Write-Host "[2/5] Register src import path via .pth"
conda run --no-capture-output --prefix "$EnvPrefix" python "$ProjectRoot/scripts/setup/register_local_src_path.py" --project-root "$ProjectRoot"

Write-Host "[3/5] Verify local source package resolution"
$VerifyLocalSourceCode = @"
import importlib.util

modules = [
    "CAD2BIM_backend.application.data_runtime_service",
    "CAD2BIM_mcp.direct.bootstrap",
]
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"Local source package resolution failed: {missing}")

print("Local source package resolution: OK")
"@
conda run --no-capture-output --prefix "$EnvPrefix" python -c $VerifyLocalSourceCode

Write-Host "[4/5] Verify third-party dependency imports"
$VerifyDependencyImportsCode = @"
import fastmcp
import psycopg
import pyarrow

print("Third-party dependency import check: OK")
"@
conda run --no-capture-output --prefix "$EnvPrefix" python -c $VerifyDependencyImportsCode

Write-Host "[5/5] Completed"
Write-Host "Activate env: conda activate \"$EnvPrefix\""
Write-Host "Run current gate: conda run --no-capture-output --prefix \"$EnvPrefix\" python scripts/release/run_cad_data_gates.py"
Write-Host "Run Direct MCP only after configuring CAD2BIM_DIRECT_SUBJECT and CAD2BIM_DIRECT_APPROVED_ROOTS: cad2bim-mcp-direct"
