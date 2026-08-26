# H06 Standard Default Ports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已发布 H06 安全迁移到 API `8000`、MySQL `3306`、Qdrant `6333/6334`、Redis `6379`，并上线语义重写非法标签修复。

**Architecture:** 保留 compose project、容器名、Qdrant collection 和三个 H06 数据卷，只强制重建存储容器的宿主机端口映射。API 以新代码在 `8000` 冷启动，验证后停止旧 `8001`；失败时使用同一 H06 卷恢复旧端口。

**Tech Stack:** Python、Pydantic、pytest、PowerShell、Docker Compose、FastAPI/Uvicorn、MySQL、Qdrant、Redis。

## Global Constraints

- 最终端口固定为 API `8000`、MySQL `3306`、Qdrant REST/gRPC `6333/6334`、Redis `6379`。
- 必须复用 `food_agent_v2_h06_mysql_v2_data`、`food_agent_v2_h06_qdrant_v2_data`、`food_agent_v2_h06_redis_v2_data`；不得使用 `-v` 或删除卷。
- build ID 必须保持 `fefd8bd7-dafa-4cc5-be0a-40ca22939392`，MySQL 19 类 Artifact、1932 道菜和 Qdrant 1932 点不变。
- 模型重写不得把 `两人` 写入 `population_tags`，不得把普通 `健康` 写成健康约束。
- 必须保留工作树中所有无关脏改动，精确暂存。
- Windows 后台 API 使用 `Start-Process -WindowStyle Hidden`；新入口未 ready 前不得停止可用入口。

---

### Task 1: 收口语义重写闭集校验

**Files:**
- Modify: `src/food_agent_v2/c3/query_normalizer.py`
- Modify: `tests/c3/test_query_normalizer.py`

**Interfaces:**
- Consumes: `QueryNormalizer.normalize(...) -> SemanticRewrite`。
- Produces: 非法模型标签触发既有重试并最终使用 `deterministic_semantic_fallback`。

- [ ] **Step 1: 保留并核对失败测试证据**

回归输入固定为 `两人晚餐，都不要海鲜，清淡健康`，模型连续返回 `population_tags=["两人"]`、`health_constraints=["健康"]`。修复前测试必须证明非法输出被接受。

- [ ] **Step 2: 实现最小校验**

`population_tags` 仅允许 `_POPULATIONS`；健康约束必须含显式疾病、过敏或不耐受信号。无效输出进入既有三次重试，不做静默删字段。

- [ ] **Step 3: 运行专项测试**

Run: `uv run pytest tests/c3/test_query_normalizer.py tests/c3/test_orchestrator.py tests/c3/test_mc02_online_b2.py -q`

Expected: PASS。

- [ ] **Step 4: 精确提交**

```powershell
git add src/food_agent_v2/c3/query_normalizer.py tests/c3/test_query_normalizer.py
git commit -m "fix: reject invalid semantic retrieval tags"
```

### Task 2: 将代码与发布契约切到标准端口

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `src/food_agent_v2/core/config.py`
- Modify: `scripts/publish_h06.ps1`
- Modify: `tests/infrastructure/test_h06_publication_contract.py`
- Modify: `docs/runbooks/h06-publication.md`
- Create: `tests/test_config_defaults.py`

**Interfaces:**
- Consumes: 环境变量 `API_PORT/MYSQL_PORT/QDRANT_REST_PORT/QDRANT_GRPC_PORT/REDIS_PORT`。
- Produces: 无环境覆盖时返回 `8000/3306/6333/6334/6379`。

- [ ] **Step 1: 写默认端口失败测试**

断言 `load_config()` 在清除五个端口环境变量后产生标准端口；发布脚本 Preflight JSON 和 StartApi/Initialize 测试环境也必须使用相同数值。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/infrastructure/test_h06_publication_contract.py tests/test_config_defaults.py -q`

Expected: 当前错峰端口断言失败。

- [ ] **Step 3: 修改最小默认值与契约**

将 compose、配置、`.env.example`、H06 脚本、测试和 runbook 的运行端口统一为标准值；容器内端口不变。

- [ ] **Step 4: 运行专项测试和静态检查**

Run: `uv run pytest tests/infrastructure/test_h06_publication_contract.py tests/test_config_defaults.py -q`

Run: `uv run ruff check src/food_agent_v2/core/config.py tests/test_config_defaults.py tests/infrastructure/test_h06_publication_contract.py`

Expected: PASS。

- [ ] **Step 5: 精确提交**

```powershell
git add docker-compose.yml .env.example src/food_agent_v2/core/config.py scripts/publish_h06.ps1 tests/infrastructure/test_h06_publication_contract.py docs/runbooks/h06-publication.md tests/test_config_defaults.py
git commit -m "ops: use standard service ports"
```

### Task 3: 原地重建 H06 并切换 API

**Files:**
- Runtime only: H06 containers/volumes and API processes。

**Interfaces:**
- Consumes: Task 1–2 提交和现有 H06 卷。
- Produces: 标准端口 H06 与 `http://127.0.0.1:8000`。

- [ ] **Step 1: 预检**

确认标准端口空闲、三个 H06 卷精确匹配、8001 仍 ready，记录旧映射用于回滚。

- [ ] **Step 2: 重建三存储容器**

设置 H06 容器名和标准端口环境，运行 `docker compose -p food_agent_v2_h06 -f <absolute docker-compose.yml> up -d --force-recreate mysql qdrant redis`。不得运行 `down -v`。

- [ ] **Step 3: 验证存储身份**

等待三容器 healthy；用应用 probe 验证 MySQL build ID、19 类 Artifact、1932 菜与 Qdrant 1932 点。

- [ ] **Step 4: 冷启动 8000**

以 H06 标准端口和新代码启动隐藏 Uvicorn 进程；轮询 `/health`、`/ready`，build ID 必须一致。

- [ ] **Step 5: 连续场景验收**

针对 `两人晚餐，都不要海鲜，清淡健康` 连续运行至少三次真实请求，全部必须完成；同时运行配置、语义和发布脚本专项测试。

- [ ] **Step 6: 停止旧入口并终验**

停止旧 `8001` 进程；确认 `8000` ready，旧端口 `8001/3309/6339/6340/6382` 不再监听，H05 容器和卷为零。

- [ ] **Step 7: 失败回滚**

任一步失败则停止新 API，使用同一 H06 卷恢复 `3309/6339/6340/6382`，并在 `8001` 冷启动旧入口；不得删除 H06 卷。
