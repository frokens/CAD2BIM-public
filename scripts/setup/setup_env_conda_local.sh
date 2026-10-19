#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_PREFIX="${CONDA_ENV_PREFIX:-$PROJECT_ROOT/.conda/cad2bim_py311}"
ENV_FILE="${CONDA_ENV_FILE:-$PROJECT_ROOT/environment.conda.yml}"

if ! command -v conda >/dev/null 2>&1; then
  echo "[ERROR] conda is not installed or not in PATH."
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "[ERROR] environment file not found: $ENV_FILE"
  exit 1
fi

mkdir -p "$PROJECT_ROOT/.conda"

echo "[1/5] Create/update conda env at: $ENV_PREFIX"
conda env update --prefix "$ENV_PREFIX" --file "$ENV_FILE" --prune

echo "[2/5] Register src import path via .pth"
conda run --no-capture-output --prefix "$ENV_PREFIX" \
  python "$PROJECT_ROOT/scripts/setup/register_local_src_path.py" \
  --project-root "$PROJECT_ROOT"

echo "[3/5] Verify local source package resolution"
conda run --no-capture-output --prefix "$ENV_PREFIX" python - <<'PY'
import importlib.util

modules = [
    "CAD2BIM_backend.application.data_runtime_service",
    "CAD2BIM_mcp.direct.bootstrap",
]
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"Local source package resolution failed: {missing}")

print("Local source package resolution: OK")
PY

echo "[4/5] Verify third-party dependency imports"
conda run --no-capture-output --prefix "$ENV_PREFIX" python - <<'PY'
import fastmcp
import psycopg
import pyarrow

print("Third-party dependency import check: OK")
PY

echo "[5/5] Completed"
echo "Activate env: conda activate \"$ENV_PREFIX\""
echo "Run current gate: conda run --no-capture-output --prefix \"$ENV_PREFIX\" python scripts/release/run_cad_data_gates.py"
echo "Run Direct MCP only after configuring CAD2BIM_DIRECT_SUBJECT and CAD2BIM_DIRECT_APPROVED_ROOTS: cad2bim-mcp-direct"
