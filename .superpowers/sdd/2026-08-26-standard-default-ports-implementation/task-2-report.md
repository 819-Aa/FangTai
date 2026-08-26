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
