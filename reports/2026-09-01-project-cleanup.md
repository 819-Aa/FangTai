# program_v2 项目清理记录

日期：2026-09-01

## 当前项目入口

- 分支：仅保留 `main`。
- 数据环境：H07 MySQL、Qdrant、Redis。
- 生成模型：DashScope `qwen3.8-max`。
- 嵌入与重排：SiliconFlow API，模型名为 `BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3`。
- 默认 API 端口：`8003`；前端开发端口：`5174`。
- 验收入口：`scripts/run_full_acceptance.ps1`。

## 已清理

- 删除 2 个已被 `main` 覆盖的 Git worktree 和对应临时分支，约 2.6 GiB。
- 删除退役 H06 的 3 个容器和 3 个数据卷；H07 容器与数据卷保持健康。
- 删除本地 BGE/Reranker 模型缓存，约 10.6 GiB。
- 从依赖中移除不再使用的 `sentence-transformers`、`torch` 及其传递依赖；`.venv` 从约 1.0 GiB 降至约 290 MiB。
- 删除 H06 发布脚本、H06 启动器、对应测试和运行手册。
- 删除旧 T23/H04 验收入口，`run_full_acceptance.ps1` 已改为 H07/千问预检与当前回归入口。
- 删除旧 E2E HTML、旧未跟踪实施草案、Python/pytest/Ruff 缓存和旧 pipeline 临时报告。
- 清除本地模型路径死配置、注释掉的本地加载代码以及活动提示词中的 DeepSeek 模型标记。
- 主目录 `.env` 已切换到经过验证的 H07 + Qwen 配置；密钥仍只保存在 Git 忽略文件中。

## 测试口径修正

- 过敏策略测试不再依赖已经不存在的 `before` 快照。
- R-002 临时约束测试读取当前 ready build，不再写死旧 H04 build_id。
- Redis 测试键和 Qdrant alias 测试读取当前配置，不再写死旧前缀/别名。
- 5 个依赖外部时间图批准缓存的完整 rebuild 测试标记为 `live`；普通回归不再假装能够无缓存重建。
- Windows PowerShell 与 PowerShell 7 均可运行 H07 预检。

## 最终验证

- H07 配置预检通过。
- `scripts/run_full_acceptance.ps1`：1509 passed，3 skipped，34 deselected，0 failed，0 errors。
- 3 个 skipped 均为 Windows 当前账户缺少符号链接权限。
- Ruff：全仓通过。
- 前端 Vitest：31 passed；Vite 生产构建通过。
- H07 Qdrant 真实集成：3 passed；MySQL、Qdrant、Redis 容器均为 healthy。
- Git 忽略文件外未检出凭证形态文本。
- 剩余一个第三方警告：FastAPI/Starlette TestClient 提示未来改用 `httpx2`，不影响当前功能。

## 有意保留与未执行

- 保留 `.venv`、前端 `node_modules`、正式数据/审计报告和 H07 数据卷。
- H07 API 与前端开发服务器当前未常驻启动；需要联调时再启动。
- `data/cache/recipe_time_graphs.jsonl` 未纳入仓库。若需要重新发布数据，必须先按离线流程生成并批准该缓存；现有 H07 已发布数据不受影响。
- 历史计划、ADR 和审计报告保留，用于追溯，不作为当前运行入口。
