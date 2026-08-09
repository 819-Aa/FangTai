# Program V2 DeepSeek 交接基线

- 日期：2026-08-09
- 状态：`KNOWN_RED_BASELINE`
- 用途：记录 DeepSeek 开始 T01 前必须保留并逐步消除的真实初始问题

## 1. 基线不是验收通过

本报告证明仓库、固定数据、正式准则和执行计划已经可以交接；不表示当前业务实现已经符合 V2。下列失败是整改计划的输入，DeepSeek 不得删除测试、修改期望或把 skip 计为通过来消除红灯。

## 2. 2026-08-09 新鲜验证结果

| 检查 | 命令 | 结果 |
|---|---|---|
| 测试收集 | `uv run pytest --collect-only -q` | 退出码 0；68 collected |
| 当前非 live/环境测试 | `uv run pytest -m "not live" -q` | 退出码 1；63 passed、1 failed、4 skipped，83.58 秒 |
| 已知失败 | 同上 | `tests/test_invariants.py::TestC4Compression::test_restore_session`，恢复后只得到 1 个事件，期望至少 2 个 |
| Python 静态检查 | `uv run ruff check src tests` | 退出码 1；144 errors，其中包括 `api_app.py` 的 `last_event_id` 使用前未赋值 |
| 前端生产构建 | `frontend/` 下 `npm run build` | 退出码 0；1801 modules transformed |
| Markdown 本地链接 | 交接验证脚本 | 全部有效 |
| 固定菜品源 | 大小、SHA-256、GBK CSV 解析 | 1,138,083 bytes；2,000 行；批准表头和 SHA-256 完全一致 |
| 文档结构 | 交接验证脚本 | 14 个模块基线、24 条不变量、T00—T24 共 25 个任务 |
| diff 空白错误 | `git diff --check` | 退出码 0 |

## 3. 环境性未验收项

- 4 个 skip 不能记为通过，需在计划规定的真实 MySQL/Qdrant/Redis 环境重新运行。
- live LLM 用例必须在 T23 使用获授权的模型配置运行；没有凭据时状态是 `NOT_RUN`，不是 PASS。
- 前端当前只有构建证据，没有 Vitest 套件；T21 必须先增加并运行前端测试。

## 4. DeepSeek 的起点

DeepSeek 首先按仓库根目录 [DEEPSEEK_START_HERE.md](../DEEPSEEK_START_HERE.md) 执行 T00。T01 之后的每项验证必须与本报告区分：本报告是整改前红色基线，不能被覆盖为整改后证据。
