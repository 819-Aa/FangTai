# LangGraph v2 acceptance entrypoint. It never initializes or mutates published stores.
[CmdletBinding()]
param(
    [switch]$PreflightOnly,
    [switch]$IncludeModelLive,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $PythonPath) {
    $PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("[H07_PYTHON_MISSING] $PythonPath")
    exit 2
}

$PreflightCode = @'
import json
import sys

from food_agent_v2.core.config import load_config

config = load_config()
summary = {
    "mysql": f"{config.mysql.host}:{config.mysql.port}/{config.mysql.database}",
    "qdrant": f"{config.qdrant.host}:{config.qdrant.rest_port}/{config.qdrant.collection}",
    "redis": f"{config.redis.host}:{config.redis.port}/{config.redis.key_prefix}",
    "reasoning_model": config.llm.model_reasoning,
    "answer_model": config.llm.model_answer,
    "llm_key_present": bool(config.llm.api_key),
}
expected = {
    "mysql": "127.0.0.1:3309/food_agent_v2_h07",
    "qdrant": "127.0.0.1:6339/recipe_retrieval_v2_h07",
    "redis": "127.0.0.1:6382/v2:h07",
}
if any(summary[key] != value for key, value in expected.items()):
    print("[H07_CONFIG_MISMATCH] active .env is not the accepted H07 store runtime", file=sys.stderr)
    raise SystemExit(2)
if not all((config.llm.api_key, config.llm.base_url, config.llm.model_reasoning,
            config.llm.model_for_role("answer_generation"))):
    print("[H07_LLM_NOT_CONFIGURED] missing provider/model configuration", file=sys.stderr)
    raise SystemExit(2)
print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
'@

Push-Location $RepoRoot
try {
    $PreflightCode | & $PythonPath -
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    if ($PreflightOnly) {
        exit 0
    }

    & $PythonPath -m pytest -m "not live" -q
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    if ($IncludeModelLive) {
        & $PythonPath scripts/verify_v2_live_structured.py
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }
}
finally {
    Pop-Location
}
