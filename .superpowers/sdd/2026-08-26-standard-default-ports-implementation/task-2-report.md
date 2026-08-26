# Task 2 Report: Standard Service Ports

## Result

统一无环境覆盖时的运行时默认端口：

- API: `8000`
- MySQL: `3306`
- Qdrant REST/gRPC: `6333/6334`
- Redis: `6379`

已同步配置、Compose、H06 发布脚本、前端 Vite 代理、活动 E2E/性能脚本、活动模块文档和发布 runbook。T23 隔离端口 `38001/33307/36335/36336/36380` 未修改；未操作 Docker、数据或 `uv.lock`。

## Verification

- `uv run pytest tests/infrastructure/test_h06_publication_contract.py tests/test_config_defaults.py -q`
  - `12 passed`
- `uv run ruff check src/food_agent_v2/core/config.py tests/test_config_defaults.py tests/infrastructure/test_h06_publication_contract.py scripts/perf_harness.py scripts/run_e2e_report.py tests/e2e/test_full_chain_real.py tests/e2e/test_full_chain_failure_paths.py`
  - `All checks passed`
- 活动代码/文档检索确认没有遗留 `8001/8002/3307/3309/6335/6336/6339/6340/6380/6382`；仅隔离端口仍保留。

## Commit

提交：`ops: use standard service ports`（最终 SHA 由提交结果返回）

## Risk

现有运行中的旧服务仍可能绑定旧端口；本任务只更新默认和发布契约，实际 H06 容器重建、保留现有数据卷、API 切换和线上连续 E2E 属于后续运行时任务。运行时切换前必须确认标准端口空闲并核对三类 H06 数据卷未改变。

## API process ownership fix

真实切换发现 `Start-Process` 启动 `uv run uvicorn` 时返回的是 `uv` 启动器 PID；readiness 超时清理只能终止启动器，可能遗留实际 Python 服务器。发布脚本现直接启动工作树 `.venv/Scripts/python.exe -m uvicorn`，因此记录和清理的 PID 就是 API 服务器本身，并继续继承已导入的 `.env`。Python 可执行文件不存在时 fail-closed。

- RED：启动契约仍记录 `FilePath=uv`，不满足服务器 PID 所有权。
- GREEN：`uv run pytest tests/infrastructure/test_h06_publication_contract.py tests/test_config_defaults.py -q` → `13 passed`。
- Ruff：目标 Python 测试文件全部通过。

## Follow-up Review Round

- RED: 黑盒 fake `docker.cmd` 让 H05 `docker inspect` 输出 “No such object” 并返回 1；修复前 Preflight 失败。
- GREEN: 修复后 `uv run pytest tests/infrastructure/test_h06_publication_contract.py -q` → `12 passed`；新增回归覆盖真实目标端口被占用时核对标准 H06 映射。
- 真实只读 `Preflight -DryRun` → exit `0`；manifest build `fefd8bd7-dafa-4cc5-be0a-40ca22939392`；H05 mysql/qdrant/redis 均为 `missing/false`；未执行 Docker Compose。
- `uv run ruff check tests/infrastructure/test_h06_publication_contract.py` → `All checks passed`。
- `frontend/playwright.config.ts` 的活动说明已从旧默认 `8001` 改为 `8000`。
- `docs/modules/01-data-engineering.md` 的端口仍为标准 `3306/6333/6334/6379`；该文件的原有营养/时间语义改动在修复提交后重新作为未暂存工作树 diff 保留。
