# T23 真实全链路验收（Gate S5/S6）—— H04 隔离固定数据环境
#
# 规则：
#   - 只允许操作获授权的隔离 Compose project（T23_PROJECT）与其专属容器/卷；
#     绝不启动/复用默认 food_agent_v2_* 旧资源。
#   - 前置检查：资源身份、目标为空、工作区 clean、服务真实 readiness。
#   - 不向非空 .staging/final 执行 data-rebuild；需要全新卷时返回 BLOCKED_T23_EMPTY_ENV。
#   - live 测试 0 skip：机器可解析报告，任何 skip/未执行/API key 缺失 → NOT_ACCEPTED。
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1

$ErrorActionPreference = "Stop"

# ---- 隔离资源身份 ----
$T23_PROJECT = "food_agent_v2_t23"
$T23_PREFIX = "food_agent_v2_t23"
$MYSQL_PORT = "33307"     # 隔离端口，避免与默认 3307 冲突
$QDRANT_REST_PORT = "36335"
$QDRANT_GRPC_PORT = "36336"
$REDIS_PORT = "36380"

$STAGING_DIR = ".staging/t23"      # 全新空 staging（绝不触碰现有 .staging/final）

function Assert-Precheck($name, $condition, $blockReason) {
    Write-Host "precheck: $name" -NoNewline
    if ($condition) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " BLOCKED" -ForegroundColor Red
        Write-Host "[BLOCKED_T23_EMPTY_ENV] $blockReason" -ForegroundColor Red
        exit 2
    }
}

function Assert-Step($name, $scriptBlock) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $scriptBlock
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $name (exit=$LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[PASS] $name" -ForegroundColor Green
}

# ---- 前置：工作区 clean（不得在脏工作区做验收） ----
$ws = git status --porcelain
Assert-Precheck "工作区 clean" `
    ([string]::IsNullOrWhiteSpace($ws)) `
    "工作区存在未提交改动，拒绝验收（$($ws.Trim())）"

# ---- 前置：资源身份（不得复用默认旧资源） ----
$running = docker ps --format "{{.Names}}" | Select-String "food_agent_v2_mysql|food_agent_v2_qdrant|food_agent_v2_redis"
Assert-Precheck "默认旧资源未运行" `
    ($null -eq $running) `
    "检测到默认 food_agent_v2_* 容器在运行（$running），拒绝使用旧资源"

# ---- 前置：全新空 staging（不得对现有 staging 执行 rebuild） ----
Assert-Precheck "全新空 staging 目录" `
    (-not (Test-Path $STAGING_DIR)) `
    "目标 staging 已存在（$STAGING_DIR）；拒绝覆盖。请指定新的空目录或等待授权"

# ---- 启动隔离 Compose project（专属容器/卷；不触碰默认项目） ----
Assert-Step "docker compose -p $T23_PROJECT up（隔离端口）" {
    $env:MYSQL_CONTAINER_NAME = "${T23_PREFIX}_mysql"
    $env:QDRANT_CONTAINER_NAME = "${T23_PREFIX}_qdrant"
    $env:REDIS_CONTAINER_NAME = "${T23_PREFIX}_redis"
    $env:MYSQL_PORT = "$MYSQL_PORT"
    $env:QDRANT_REST_PORT = "$QDRANT_REST_PORT"
    $env:QDRANT_GRPC_PORT = "$QDRANT_GRPC_PORT"
    $env:REDIS_PORT = "$REDIS_PORT"
    docker compose -p $T23_PROJECT up -d mysql qdrant redis
}

# ---- 前置：服务真实 readiness（健康检查） ----
Assert-Step "服务 readiness 健康检查" {
    $deadline = (Get-Date).AddSeconds(90)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        $m = docker exec "${T23_PREFIX}_mysql" mysqladmin ping -h localhost 2>$null
        $q = curl.exe -s "http://localhost:$QDRANT_REST_PORT/healthz"
        $r = docker exec "${T23_PREFIX}_redis" redis-cli ping 2>$null
        if ($m -match "alive" -and $q -match "healthz check passed" -and $r -eq "PONG") {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 5
    }
    if (-not $ready) {
        Write-Host "[BLOCKED_T23_EMPTY_ENV] 服务未就绪" -ForegroundColor Red
        exit 2
    }
}

# ---- 前置：目标为空（隔离卷必须是全新；已有数据 → BLOCKED） ----
$cfgCheck = docker exec "${T23_PREFIX}_mysql" mysql -ufoodagent -pfoodagent_v2 food_agent_v2 -e "SELECT COUNT(*) AS c FROM recipes" 2>$null
if ($cfgCheck -match "[1-9]") {
    Write-Host "[BLOCKED_T23_EMPTY_ENV] 隔离 MySQL 已有固定数据，拒绝重复初始化" -ForegroundColor Red
    exit 2
}

# ---- 固定数据：从全新空 staging 重建并初始化 ----
Assert-Step "data-rebuild --staging-dir $STAGING_DIR" {
    uv run food-agent-v2 data-rebuild --staging-dir $STAGING_DIR
}
Assert-Step "data-verify --manifest" {
    uv run food-agent-v2 data-verify --manifest "$STAGING_DIR/build_manifest.json"
}
Assert-Step "data-initialize --confirm-empty-v2" {
    uv run food-agent-v2 data-initialize --manifest "$STAGING_DIR/build_manifest.json" --confirm-empty-v2
}

# ---- live 0 skip：机器可解析报告 ----
Assert-Step "pytest tests/test_prompts_live.py + tests/e2e（23 live，0 skip）" {
    uv run pytest tests/test_prompts_live.py tests/e2e -q --junitxml="$PWD/.staging/t23_live_report.xml" 2>&1
    $live = uv run pytest tests/test_prompts_live.py tests/e2e --collect-only -q -m live 2>&1 | Select-String "tests collected"
    Write-Host "LIVE_COLLECT: $live"
    $xml = [xml](Get-Content "$PWD/.staging/t23_live_report.xml")
    $tests = [int]$xml.testsuite.tests
    $skipped = [int]$xml.testsuite.skipped
    $failed = [int]$xml.testsuite.failures
    $passed = $tests - $skipped - $failed
    Write-Host "LIVE_REPORT: collected=$tests passed=$passed skipped=$skipped failed=$failed"
    if ($skipped -gt 0 -or $failed -gt 0 -or $tests -lt 23) {
        Write-Host "[NOT_ACCEPTED] live 存在 skip($skipped)/failed($failed) 或未全部执行($tests)" -ForegroundColor Red
        exit 1
    }
}

# ---- 后端回归 + 静态 + 前端 ----
Assert-Step "pytest -m 'not live'" {
    uv run pytest -m "not live" -q
}
Assert-Step "ruff check src tests scripts" {
    uv run ruff check src tests scripts
}
Assert-Step "npm test -- --run" {
    Push-Location frontend
    npm test -- --run
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { exit $code }
}
Assert-Step "npm run build" {
    Push-Location frontend
    npm run build
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { exit $code }
}

Write-Host "`n[ACCEPTANCE] 全链路验收通过（Gate S5/S6）" -ForegroundColor Green
