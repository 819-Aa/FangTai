# T23 真实全链路验收（Gate S5/S6）
# 前置：H04 空 V2 环境、固定源未改、DeepSeek API key 已配置、本地模型缓存存在。
# 从项目根目录执行：powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1

$ErrorActionPreference = "Stop"

function Assert-Step($name, $scriptBlock) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $scriptBlock
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[FAIL] $name (exit=$LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[PASS] $name" -ForegroundColor Green
}

# 0. 基础设施
Assert-Step "docker compose up -d mysql qdrant redis" {
    docker compose up -d mysql qdrant redis
    Start-Sleep -Seconds 8
}

# 1. 固定数据：重建 staging、校验 manifest、初始化空 V2
Assert-Step "data-rebuild --staging-dir .staging/final" {
    uv run food-agent-v2 data-rebuild --staging-dir .staging/final
}
Assert-Step "data-verify --manifest" {
    uv run food-agent-v2 data-verify --manifest .staging/final/build_manifest.json
}
Assert-Step "data-initialize --confirm-empty-v2" {
    uv run food-agent-v2 data-initialize --manifest .staging/final/build_manifest.json --confirm-empty-v2
}

# 2. 后端回归 + 真实全链路 e2e
Assert-Step "pytest tests/integration tests/e2e" {
    uv run pytest tests/integration tests/e2e -q
}
Assert-Step "pytest tests/test_prompts_live.py" {
    uv run pytest tests/test_prompts_live.py -q
}

# 3. 静态检查
Assert-Step "ruff check src tests scripts" {
    uv run ruff check src tests scripts
}

# 4. 前端
Assert-Step "npm test -- --run" {
    Push-Location frontend
    npm test -- --run
    $npmTest = $LASTEXITCODE
    Pop-Location
    exit $npmTest
}
Assert-Step "npm run build" {
    Push-Location frontend
    npm run build
    $npmBuild = $LASTEXITCODE
    Pop-Location
    exit $npmBuild
}

Write-Host "`n[ACCEPTANCE] 全链路验收通过（Gate S5/S6）" -ForegroundColor Green
