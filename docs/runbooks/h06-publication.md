# H06 Publication Runbook

This runbook publishes the isolated H06 nutrition build without mutating H05.

## Inputs

- Manifest: `.staging/h06-nutrition-complete-v3/build_manifest.json`
- Environment: credentials and API keys loaded from the current worktree `.env` when present, then the main repository `.env`

## Actions

### 1. Preflight

```powershell
powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action Preflight -Manifest .staging/h06-nutrition-complete-v3/build_manifest.json -DryRun
```

Expected result:

- Manifest resolves inside `.staging/h06-nutrition-complete` or `.staging/h06-nutrition-complete-v3`
- Ports `3309/6339/6340/6382` are either free or already owned by the exact healthy H06 containers
- Port `8002` is free
- H05 container health is reported only for visibility

### 2. Start Stores

```powershell
powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action StartStores -Manifest .staging/h06-nutrition-complete-v3/build_manifest.json
```

This launches only `food_agent_v2_h06_mysql`, `food_agent_v2_h06_qdrant`, and `food_agent_v2_h06_redis` with Docker project `food_agent_v2_h06`.

The script executes Docker Compose from the repository root with the explicit file `docker-compose.yml`, then polls the exact H06 three-container set until all are `running=true` and `healthy`. If that condition is not reached before timeout, the command fails closed.

### 3. Initialize

```powershell
powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action Initialize -Manifest .staging/h06-nutrition-complete-v3/build_manifest.json
```

This runs:

```powershell
uv run food-agent-v2 data-initialize --manifest <absolute-manifest> --confirm-empty-v2
```

### 4. Start API

```powershell
powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action StartApi -Manifest .staging/h06-nutrition-complete-v3/build_manifest.json
```

The script starts the H06 API on `127.0.0.1:8002` with `Start-Process -WindowStyle Hidden` and writes logs to:

- `.staging/h06-api.log`
- `.staging/h06-api.err`

The H06 process is started with `RAG_WARMUP_ON_STARTUP=true` so readiness can eventually satisfy the SiliconFlow probe. The script then polls `/health` and `/ready` until both are live and `/ready.build_id` matches the manifest build ID, treating connection refusal and transient readiness failures as retryable cold-start states.

### 5. Verify

```powershell
powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action Verify -Manifest .staging/h06-nutrition-complete-v3/build_manifest.json
```

Expected result:

- `GET /health` returns `200` with `{"status":"ok"}`
- `GET /ready` returns `200` with `status=ready`
- `/ready.build_id` matches the manifest build ID
- `food_agent_v2_h06_mysql`, `food_agent_v2_h06_qdrant`, and `food_agent_v2_h06_redis` are all `running=true` and `healthy`

`Verify` also retries transient cold-start states until timeout instead of surfacing the first connection-refused or mismatched-build response.

## Notes

- `Preflight -DryRun` is the safe verification path for already-running environments.
- The script is fail-closed for unexpected listeners on H06 ports.
- The script does not contain secrets; it inherits them from `.env`.
