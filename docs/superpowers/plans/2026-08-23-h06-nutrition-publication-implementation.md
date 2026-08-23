# H06 Nutrition Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已验证的1932道菜新营养画像发布到完全独立的H06数据容器和API进程，完成全局回归后安全切换，同时保留H05整体回滚能力。

**Architecture:** H06使用独立compose project、容器名、数据卷、端口和冷启动API进程。readiness除MySQL/Qdrant/Redis外，还核对C1/B4/B5/B6实际加载的build ID；切换发生在独立H06验证完成之后，H05代码/API配置/容器作为整体回滚单元保留。

**Tech Stack:** Docker Compose、MySQL 8、Qdrant、Redis 7、FastAPI/Uvicorn、PowerShell、pytest、curl。

## Global Constraints

- 前三份实施计划及其测试必须通过。
- H06固定使用独立资源：compose project `food_agent_v2_h06`，MySQL `3309`，Qdrant REST/gRPC `6339/6340`，Redis `6382`，临时API `8002`。
- H05当前资源保持：MySQL `3308`、Qdrant `6337/6338`、Redis `6381`、API入口 `8001`。
- H06验证前不得停止、写入或删除H05。
- H06 API必须是新进程，不得复用H05的C1/B5/B6单例或缓存。
- H06未通过全部门禁时不得切换API。
- 删除H05不属于本计划；必须另获用户明确授权。
- Windows后台进程使用 `Start-Process -WindowStyle Hidden`。

---

### Task 1: 暴露运行时服务实际加载的 build ID

**Files:**
- Modify: `src/food_agent_v2/c1/__init__.py`
- Modify: `src/food_agent_v2/b4/repository.py`
- Modify: `src/food_agent_v2/b5/__init__.py`
- Modify: `src/food_agent_v2/b6/__init__.py`
- Modify: `tests/c1/test_full_hybrid_retrieval.py`
- Modify: `tests/b4/test_health_engine_matrix.py`
- Modify: `tests/b5/test_task_graph_scheduler.py`
- Modify: `tests/b6/test_soft_evidence_availability.py`

**Interfaces:**
- Consumes: 各服务现有 `ready_build_id()`。
- Produces: `loaded_build_id: str | None` 只读属性。

- [ ] **Step 1: 写加载身份失败测试**

```python
def test_service_exposes_the_build_it_loaded() -> None:
    source = FakeSource(build_id="h06-build", records=_records())
    service = NutritionScoringService(source)
    assert service.loaded_build_id is None
    service.load()
    assert service.loaded_build_id == "h06-build"
```

分别覆盖C1、B5、B6；B4 repository 的 `ready_build_id()`必须保持同一实例内稳定。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/c1/test_full_hybrid_retrieval.py tests/b4/test_health_engine_matrix.py tests/b5/test_task_graph_scheduler.py tests/b6/test_soft_evidence_availability.py -q`

Expected: FAIL，服务尚不保存公开loaded ID。

- [ ] **Step 3: 保存并暴露加载身份**

```python
@property
def loaded_build_id(self) -> str | None:
    return self._loaded_build_id
```

每次 `load()`先读取 source ready ID，成功加载全部记录后才原子写入 `_loaded_build_id`；加载失败不得留下新ID。

- [ ] **Step 4: 运行专项测试**

Run: `uv run pytest tests/c1/test_full_hybrid_retrieval.py tests/b4/test_health_engine_matrix.py tests/b5/test_task_graph_scheduler.py tests/b6/test_soft_evidence_availability.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/food_agent_v2/c1/__init__.py src/food_agent_v2/b4/repository.py src/food_agent_v2/b5/__init__.py src/food_agent_v2/b6/__init__.py tests/c1/test_full_hybrid_retrieval.py tests/b4/test_health_engine_matrix.py tests/b5/test_task_graph_scheduler.py tests/b6/test_soft_evidence_availability.py
git commit -m "feat: expose loaded artifact build identity"
```

### Task 2: 将运行时 build 一致性纳入 readiness

**Files:**
- Modify: `src/food_agent_v2/application/readiness.py`
- Modify: `src/food_agent_v2/api_app.py`
- Modify: `tests/application/test_readiness.py`
- Modify: `tests/integration/test_api_readiness.py`

**Interfaces:**
- Consumes: Task 1 的 loaded ID、MySQL ready build ID。
- Produces: `runtime_artifact_probe(expected_build_id) -> dict` 和 fail-closed `/ready`。

- [ ] **Step 1: 写交叉build失败测试**

```python
def test_readiness_rejects_a_runtime_service_loaded_from_another_build() -> None:
    with pytest.raises(ServiceNotReady) as exc:
        check_readiness(
            mysql_probe=lambda: _mysql_ready("h06-build"),
            redis_probe=_redis_ready,
            qdrant_probe=_qdrant_ready,
            siliconflow_probe=_models_ready,
            runtime_probe=lambda expected: {
                "c1": "h06-build", "b4": "h06-build",
                "b5": "h06-build", "b6": "h05-build",
            },
        )
    assert exc.value.checks["runtime_artifacts"]["status"] == "unavailable"
```

覆盖C1/B4/B5/B6全一致时通过、任一未加载/错build失败、异常信息不暴露连接密钥。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/application/test_readiness.py tests/integration/test_api_readiness.py -q`

Expected: FAIL，readiness当前只核对存储和模型。

- [ ] **Step 3: 实现冷加载探针**

`runtime_artifact_probe`通过正式getter加载C1/B5/B6；B4创建新的 `HealthDataRepository` 并读取ready ID。所有结果必须等于MySQL build ID。C2无独立数据缓存，其B5/B6依赖已经覆盖。

- [ ] **Step 4: 在API lifespan中预热固定Artifact**

API启动时加载运行时固定服务，失败则 `/ready` 保持 unavailable。不得提供“重连另一个build”的热切换接口；build切换必须通过新进程。

- [ ] **Step 5: 运行专项测试**

Run: `uv run pytest tests/application/test_readiness.py tests/integration/test_api_readiness.py tests/integration/test_api_application.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/food_agent_v2/application/readiness.py src/food_agent_v2/api_app.py tests/application/test_readiness.py tests/integration/test_api_readiness.py tests/integration/test_api_application.py
git commit -m "feat: verify runtime artifact build readiness"
```

### Task 3: 固化H06安全发布脚本

**Files:**
- Create: `scripts/publish_h06.ps1`
- Create: `tests/infrastructure/test_h06_publication_contract.py`
- Create: `docs/runbooks/h06-publication.md`

**Interfaces:**
- Consumes: 最终 `.staging/h06-nutrition-complete/build_manifest.json` 和环境中的数据库凭据/API key。
- Produces: 仅支持 `Preflight/StartStores/Initialize/StartApi/Verify` 的非破坏式H06操作。

- [ ] **Step 1: 写发布契约失败测试**

```python
def test_h06_script_uses_only_isolated_resources() -> None:
    text = Path("scripts/publish_h06.ps1").read_text(encoding="utf-8")
    for required in ("food_agent_v2_h06", "3309", "6339", "6340", "6382", "8002"):
        assert required in text
    assert "down -v" not in text
    assert not re.search(
        r"(?i)(down|rm|remove|stop).{0,80}food_agent_v2_h05", text
    )
```

同时检查manifest必须显式传入、目标路径必须位于项目 `.staging/h06-nutrition-complete`、后台API使用 `-WindowStyle Hidden`。

- [ ] **Step 2: 运行失败测试**

Run: `uv run pytest tests/infrastructure/test_h06_publication_contract.py -q`

Expected: FAIL，脚本尚不存在。

- [ ] **Step 3: 实现Preflight和独立容器启动**

脚本固定设置：

```powershell
$H06Project = "food_agent_v2_h06"
$env:MYSQL_CONTAINER_NAME = "food_agent_v2_h06_mysql"
$env:QDRANT_CONTAINER_NAME = "food_agent_v2_h06_qdrant"
$env:REDIS_CONTAINER_NAME = "food_agent_v2_h06_redis"
$env:MYSQL_PORT = "3309"
$env:QDRANT_REST_PORT = "6339"
$env:QDRANT_GRPC_PORT = "6340"
$env:REDIS_PORT = "6382"
```

Preflight解析manifest绝对路径、验证目标在工作树内、检查端口空闲、列出H05状态但不修改。启动命令为 `docker compose -p food_agent_v2_h06 up -d mysql qdrant redis`。

- [ ] **Step 4: 实现初始化、API冷启动和验证动作**

初始化调用 `uv run food-agent-v2 data-initialize --manifest <absolute> --confirm-empty-v2`。API使用指向H06的环境和 `Start-Process -WindowStyle Hidden` 在8002启动；Verify检查 `/health`、`/ready`、build ID和容器health。

- [ ] **Step 5: 运行契约测试**

Run: `uv run pytest tests/infrastructure/test_h06_publication_contract.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add scripts/publish_h06.ps1 tests/infrastructure/test_h06_publication_contract.py docs/runbooks/h06-publication.md
git commit -m "ops: add isolated h06 publication workflow"
```

### Task 4: 运行最终全局代码回归

**Files:**
- Verify only: 全项目代码和测试。

**Interfaces:**
- Consumes: 前三份计划和 Tasks 1–3。
- Produces: 允许创建H06容器的代码证据。

- [ ] **Step 1: 运行非live全量测试**

Run: `uv run pytest -q -m "not live"`

Expected: PASS，0 failed。

- [ ] **Step 2: 运行静态检查**

Run: `uv run ruff check src tests scripts`

Expected: PASS。

- [ ] **Step 3: 验证最终数据Manifest**

Run: `uv run food-agent-v2 data-verify --manifest .staging/h06-nutrition-complete/build_manifest.json`

Run: `uv run food-agent-v2 validate-data --manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: 1932/1932营养可用，G14/G15/G18及全部既有门禁通过。

- [ ] **Step 4: 主代理审核diff**

核对非营养代码变更仅限G12、审计字段、readiness和必要Schema；检查RAG文档仍不包含营养/时间/健康结论。

### Task 5: 创建并初始化独立H06存储

**Files:**
- Runtime only: Docker project `food_agent_v2_h06`。

**Interfaces:**
- Consumes: 已验证manifest。
- Produces: H06 MySQL/Qdrant/Redis ready build。

- [ ] **Step 1: 运行Preflight**

Run: `powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action Preflight -Manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: H06端口空闲、H05三容器仍healthy、manifest路径和hash有效。

- [ ] **Step 2: 启动H06存储**

Run: `powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action StartStores -Manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: 仅 `food_agent_v2_h06_mysql/qdrant/redis` 新增且healthy。

- [ ] **Step 3: 确认目标为空并初始化**

Run: `powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action Initialize -Manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: MySQL唯一ready build等于manifest；19类运行时Artifact计数一致；Qdrant点数1932且payload build ID一致。

- [ ] **Step 4: 确认H05未变化**

Run: `docker ps --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}"`

Expected: H05和H06各三容器healthy，端口和卷不共享。

### Task 6: 冷启动H06 API并执行全局验收

**Files:**
- Runtime logs: `.staging/h06-api.log`、`.staging/h06-api.err`。

**Interfaces:**
- Consumes: H06 stores。
- Produces: 可切换的H06 API候选。

- [ ] **Step 1: 启动独立H06 API**

Run: `powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action StartApi -Manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: 新进程监听127.0.0.1:8002；8001的H05 API不受影响。

- [ ] **Step 2: 验证health和ready**

Run: `powershell -ExecutionPolicy Bypass -File scripts/publish_h06.ps1 -Action Verify -Manifest .staging/h06-nutrition-complete/build_manifest.json`

Expected: `/health=200`、`/ready=200`；MySQL/Qdrant和C1/B4/B5/B6 loaded build ID全部等于H06 manifest build ID。

- [ ] **Step 3: 运行H06集成场景**

Run: `powershell -ExecutionPolicy Bypass -Command "$env:T23_API_BASE='http://127.0.0.1:8002'; uv run pytest tests/integration -q"`

Expected: PASS。额外核对老人晚餐、低钠晚餐、高蛋白低脂晚餐、泛健康晚餐、套餐固定依赖和程序联动。

- [ ] **Step 4: 核对H05/H06非营养差异**

比较1932个recipe ID、名称、原始label、RAG payload hash、健康关系、时间画像、87条套餐/备料和11条程序依赖。允许差异仅包括build ID、营养画像、已批准Schema/审计字段和G12修复结果。

- [ ] **Step 5: 运行一遍最终全局审查**

主代理检查：语义重写不扩大目标、C1不读取营养、B4不读取营养、B6只收safe set、C2不恢复排除菜、所有存储同build、H05回滚单元仍ready。

### Task 7: 用户批准后切换入口

**Files:**
- Runtime only: API入口进程/服务配置。

**Interfaces:**
- Consumes: Task 6全部证据和用户明确切换授权。
- Produces: 8001对外入口运行H06；H05仍可恢复。

- [ ] **Step 1: 向用户提交切换证据**

报告H06 build ID、1932/1932覆盖、测试结果、H05/H06资源、非营养差异和回滚命令。未获明确授权时停在此步。

- [ ] **Step 2: 获批后原子切换API**

停止旧8001 H05 API进程，使用与已验证8002进程相同的H06代码版本和环境在8001冷启动新进程；不得让原进程热换数据库。

- [ ] **Step 3: 切换后复验**

Run: `curl.exe -fsS http://127.0.0.1:8001/ready`

Expected: build ID等于H06；四个关键行为场景通过。

- [ ] **Step 4: 保留H05**

H05容器、数据卷和启动配置保持不变。除非用户之后明确要求，否则不执行任何 `docker compose down`、volume删除或H05清理。
