# V2 Final Delivery Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有已授权 T23 固定数据环境上打通已提交结果、readiness、前端恢复与真实验收，形成可运行、可演示、可复验的竞赛交付。

**Architecture:** 保持 V2 模块化单体。Application/MySQL 是成功事实源，D1/Redis 保存公开运行投影，C4 暴露会话菜单，Qdrant 提供检索；前端只消费公开投影。验收脚本显式区分首次初始化和既有环境复验，本次禁止重新初始化。

**Tech Stack:** Python 3.12/3.13、FastAPI、PyMySQL、Redis、Qdrant、pytest、Vue 3、Pinia、Vitest、Playwright、PowerShell、Docker Compose。

## Global Constraints

- 固定菜品数据不会新增或减少；禁止修改 `data/raw/**`、已签审核 CSV 与批准 manifest。
- 当前批准 build 固定为 `8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f`。
- 复验现有 T23 时禁止 `data-rebuild`、`data-initialize`、`docker compose down -v` 和删除卷。
- 菜单规范身份固定为 `plan_id + menu_hash + recipe_ids`；菜名只从同一 ready build 固定视图派生。
- 真实 live 或浏览器测试未执行/被 skip 时不得报告全链路通过。

---

### Task 1: MC-03 已提交结果投影

**Files:**
- Modify: `src/food_agent_v2/c3/runner.py`
- Create: `tests/c3/test_mc03_committed_projection.py`
- Modify: `tests/integration/test_api_application.py`

**Interfaces:**
- Consumes: `commit_request_result(...) -> dict`、`FinalValidationArtifact`、`AnswerArtifact`。
- Produces: `WorkflowRunner._committed_result_summary(fva, ans) -> dict`，供 D1 状态轮询和前端恢复。

- [ ] **Step 1: 写 dispatcher 失败与 commit 失败的对照测试**
- [ ] **Step 2: 运行测试，确认当前 dispatcher 失败会错误反写 failed**
- [ ] **Step 3: 将原子提交与即时 outbox 投递拆为两个错误边界**
- [ ] **Step 4: 断言 completed `result_summary` 精确包含 answer 与 menu_summary，且禁止字段投影仍有效**
- [ ] **Step 5: 运行 `uv run pytest tests/c3/test_mc03_committed_projection.py tests/integration/test_api_application.py -q` 并提交**

### Task 2: MC-03 结构化菜单公开投影

**Files:**
- Create: `src/food_agent_v2/application/menu_projection.py`
- Modify: `src/food_agent_v2/application/commit_service.py`
- Modify: `src/food_agent_v2/c3/runner.py`
- Modify: `src/food_agent_v2/c4/__init__.py`
- Create: `tests/application/test_menu_projection.py`
- Modify: `tests/application/test_commit_validation.py`
- Modify: `tests/integration/test_api_application.py`

**Interfaces:**
- Produces: `build_public_menu(recipe_ids: list[int], build_id: str) -> list[dict]`，每项仅含 `recipe_id/name`。
- `result_summary.menu_summary`、outbox `menu_summary` 与 C4 `current_menu` 均含相同 `plan_id/menu_hash/recipe_ids/items`。

- [ ] **Step 1: 写未知 recipe/build mismatch fail-closed 与确定顺序测试**
- [ ] **Step 2: 用 B3 固定 Repository 实现只读菜名投影**
- [ ] **Step 3: 在提交健康证据与 outbox 中绑定 items，不解析回答文本**
- [ ] **Step 4: C4 会话查询按唯一 ready build 派生同一 items**
- [ ] **Step 5: 运行 Application/C4/API 回归并提交**

### Task 3: MC-04 真实 readiness

**Files:**
- Create: `src/food_agent_v2/application/readiness.py`
- Modify: `src/food_agent_v2/api_app.py`
- Create: `tests/integration/test_readiness.py`

**Interfaces:**
- Produces: `check_readiness() -> dict`；`GET /ready` 成功 200，失败 503 `SERVICE_NOT_READY`。

- [ ] **Step 1: 写 MySQL/Redis/Qdrant 任一失败即 503 的测试**
- [ ] **Step 2: 校验唯一 ready build、必需固定 artifact、Qdrant collection/点位与 Redis PING**
- [ ] **Step 3: 保持 `/health` 为无 I/O liveness，`/ready` 不加载模型、不写数据**
- [ ] **Step 4: 在 T23 现有环境实测 `/ready` 并提交**

### Task 4: MC-05 前端菜单与断线/刷新恢复

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/stores/recommendation.ts`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/features/chat/ChatPanel.vue`
- Modify: `frontend/src/stores/recommendation.test.ts`
- Modify: `frontend/src/features/chat/ChatPanel.test.ts`
- Modify: `frontend/e2e/browser.spec.ts`

**Interfaces:**
- Produces: `PublicMenuSummary`、`getSession(sessionId)`、store `currentMenu/restoreSession()`。

- [ ] **Step 1: 写 result_committed、轮询 completed、刷新 session 三种恢复测试**
- [ ] **Step 2: store 统一归并 `menu_summary`，SSE 与 polling 不产生两套状态**
- [ ] **Step 3: App 挂载恢复会话菜单，ChatPanel 展示菜名列表与完成身份**
- [ ] **Step 4: 运行 `npm test -- --run`、`npm run build` 并提交**

### Task 5: MC-06 既有 T23 只读复验脚本

**Files:**
- Modify: `scripts/run_full_acceptance.ps1`
- Modify: `tests/README.md`
- Modify: `tests/e2e/test_full_chain_real.py`

**Interfaces:**
- Produces: `-UseExistingAuthorizedT23` 与 `-InitializeAuthorizedEmptyT23` 互斥入口。

- [ ] **Step 1: 写脚本 guard 测试，证明复验模式不含 rebuild/initialize/delete**
- [ ] **Step 2: 复验模式校验精确容器、卷、端口、manifest/build、artifact 计数与 Qdrant 1914 点位**
- [ ] **Step 3: API 启动门槛改用 `/ready`，保留 live 0 skip/JUnit/退出码校验**
- [ ] **Step 4: 更新跨库断言，使 API/SSE/session/outbox 前端共享同一菜单 items 与身份**
- [ ] **Step 5: 运行脚本静态 guard 与确定性回归并提交**

### Task 6: T24 真实验收与报告

**Files:**
- Create: `reports/2026-08-12-v2-final-delivery-verification.md`
- Update only if behavior changed: affected `docs/modules/**` and `docs/contracts/**`

**Interfaces:**
- Produces: `READY_FOR_OWNER_ACCEPTANCE` 或精确 `BLOCKED_* / FAIL / NOT_RUN` 报告。

- [ ] **Step 1: 运行后端非 live、ruff、前端单测与 build**
- [ ] **Step 2: 在 `UseExistingAuthorizedT23` 模式启动隔离 API，运行 7 个真实 prompt live**
- [ ] **Step 3: 运行 API/SSE E2E 与 Playwright，不允许 skip**
- [ ] **Step 4: 核对 MySQL/Qdrant/Redis/API/前端同一菜单身份与 T23 零写入策略**
- [ ] **Step 5: 写入原始命令、退出码、计数、阻断和 Git commit，提交报告**
