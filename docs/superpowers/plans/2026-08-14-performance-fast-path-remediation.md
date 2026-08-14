# Performance Fast Path Remediation and Agent Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复当前快速路径的安全、并发和取消阻断，补齐混合式 Agent 的意图决策与多轮 delta，并用真实 API 证据证明竞赛性能合格后再切换默认路径。

**Architecture:** 保留 `WORKFLOW_MODE=legacy|fast_path` 回滚边界。Agent 层负责类型化意图、歧义澄清、一次模型归一化和多轮状态决策；确定性执行层固定调用 C1/B2/B4/C2、最终 B4 校验和原子提交。嵌入与重排固定使用 SiliconFlow API；Redis、HTTP 连接和取消控制通过拥有模块的公共接口提供。

**Tech Stack:** Python 3.13、FastAPI、Pydantic v2、redis-py 6、httpx 0.28、MySQL、Qdrant、pytest、ruff、SiliconFlow API、OpenAI-compatible LLM API。

**Design reference:** `docs/superpowers/specs/2026-08-14-performance-fast-path-design.md`

## Global Constraints

- `WORKFLOW_MODE` 在 Task 1–8 期间必须继续默认 `legacy`；只有 Task 9 的全部门禁通过后才能修改默认值。
- 在线 embedding/rerank 固定为 SiliconFlow API，不新增本地模型、CPU/GPU 自动切换或索引重建。
- 按人营养摄入、份量换算和用户可见营养数值不在本计划范围内。
- 任何过敏、疾病、指标或参与者归属不明确的临时健康语义必须 fail-closed：B2 严格验证、一次模型归一化或 `needs_clarification`，不得继续普通推荐。
- 推荐菜品只能来自唯一 ready build；菜单必须由权威安全候选产生并通过最终 B4 `PASS`。
- 取消或失锁后不得提交菜单、回答或成功 outbox；工具/基础设施失败不得伪装成业务终态。
- 单元和契约测试不得调用真实外部 API；真实 API 只在 Task 9 的隔离验收环境运行。
- 竞赛合格线：`visible_ttft_ms < 5000`、成功单轮 `e2e_ms < 15000`、每组成功多轮平均 `< 12000`。
- 每个 Task 单独提交，并在进入下一 Task 前进行代码审查。

---

## Level L0 — 上线阻断修复

### Task 1: 修复 SSE 等待、通知所有权和跨 worker 刷新

**Files:**
- Create: `src/food_agent_v2/c4/event_notifier.py`
- Modify: `src/food_agent_v2/c4/__init__.py`
- Modify: `src/food_agent_v2/d1/__init__.py:41-66,266-328`
- Modify: `src/food_agent_v2/api_app.py:145-198`
- Create: `tests/c4/test_event_notifier.py`
- Modify: `tests/integration/test_api_application.py`
- Modify: `tests/d1/test_sse_replay.py`

**Interfaces:**
- Produces: `RequestEventNotifier.publish(request_id: str) -> None`
- Produces: `RequestEventNotifier.subscribe(request_id: str) -> EventSubscription`
- Produces: `EventSubscription.wait(timeout_seconds: float) -> bool` and `close() -> None`
- Produces: `RecommendationAPI.subscribe_events(request_id, last_event_id=None, *, refresh=False) -> list[dict]`
- Constraint: `api_app.py` 和 D1 不得访问 `RedisSessionStore._client/_connect/_key`。

- [ ] **Step 1: 写出无事件等待和跨 worker 刷新的失败测试**

```python
def test_subscription_passes_real_timeout(fake_subscription):
    fake_subscription.wait(15.0)
    assert fake_subscription.timeout_values == [15.0]

def test_subscriber_refreshes_persisted_events_after_notification(api_worker_b, persisted_blob):
    api_worker_b.subscribe_events("r1")
    persisted_blob.append_answer_ready("r1", event_id="ev_answer_r1")
    events = api_worker_b.subscribe_events("r1", refresh=True)
    assert [e["id"] for e in events] == ["ev_answer_r1"]
```

- [ ] **Step 2: 运行测试并确认当前实现失败**

Run: `.venv\Scripts\python.exe -m pytest tests\c4\test_event_notifier.py tests\integration\test_api_application.py tests\d1\test_sse_replay.py -q`

Expected: FAIL，至少证明位置参数没有设置 `timeout=15`，以及本地已缓存请求不会刷新其他 worker 写入的事件。

- [ ] **Step 3: 在 C4 实现公共事件通知接口**

```python
class EventSubscription:
    def wait(self, timeout_seconds: float) -> bool:
        message = self._pubsub.get_message(timeout=timeout_seconds)
        return message is not None

    def close(self) -> None:
        self._pubsub.close()


class EventNotificationBackend(Protocol):
    def publish(self, request_id: str) -> None:
        raise NotImplementedError

    def subscribe(self, request_id: str) -> EventSubscription:
        raise NotImplementedError


class RequestEventNotifier:
    def __init__(self, backend: EventNotificationBackend) -> None:
        self._backend = backend

    def publish(self, request_id: str) -> None:
        self._backend.publish(request_id)

    def subscribe(self, request_id: str) -> EventSubscription:
        return self._backend.subscribe(request_id)
```

使用进程级 Redis client/connection pool；发布顺序保持“持久事件写入成功 → notify”。Redis 不可用时由同一接口回退进程内 `threading.Condition`，不得让调用者触碰 Redis 私有成员。

- [ ] **Step 4: 接线 D1 与 SSE，并强制通知后刷新持久事件**

`_emit_event` 调用 `RequestEventNotifier.publish`；SSE 使用：

```python
await loop.run_in_executor(None, subscription.wait, 15.0)
events = api.subscribe_events(request_id, last_event_id, refresh=True)
```

`refresh=True` 从 Redis 重新加载并按稳定 `event_id` 合并，不重复、不丢失，不覆盖本 worker 已有的更新事件。

- [ ] **Step 5: 验证 heartbeat 有界、即时唤醒和断线续传**

Run: `.venv\Scripts\python.exe -m pytest tests\c4\test_event_notifier.py tests\integration\test_api_application.py tests\d1\test_sse_replay.py tests\integration\test_transactional_outbox.py -q`

Expected: PASS；无事件测试在 1 秒观察窗内不得产生第二个 heartbeat；发布事件后无需等待 15 秒即可收到；稳定 event ID 续传不重不漏。

- [ ] **Step 6: 静态检查并提交**

Run: `.venv\Scripts\python.exe -m ruff check src\food_agent_v2\c4\event_notifier.py src\food_agent_v2\d1\__init__.py src\food_agent_v2\api_app.py tests\c4\test_event_notifier.py tests\integration\test_api_application.py tests\d1\test_sse_replay.py`

Commit: `fix(sse): make event notification blocking and replay-safe`

### Task 2: 用线程安全连接池替换 SiliconFlow 单连接

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/food_agent_v2/c1/siliconflow.py`
- Modify: `src/food_agent_v2/api_app.py`
- Modify: `src/food_agent_v2/application/readiness.py`
- Create: `tests/c1/test_siliconflow_client.py`
- Modify: `tests/application/test_readiness.py`
- Modify: `tests/integration/test_api_readiness.py`

**Interfaces:**
- Produces: `SiliconFlowHTTPClient.post(path: str, body: dict, timeout_seconds: float) -> dict`
- Produces: `get_siliconflow_http_client() -> SiliconFlowHTTPClient`
- Produces: `close_siliconflow_http_client() -> None`
- Preserves: `SiliconFlowEmbedder.encode(self, texts, normalize_embeddings=True, **kwargs)` and `SiliconFlowReranker.predict(self, pairs)` public behavior.

- [ ] **Step 1: 为并发、超时和 HTTP 错误写失败测试**

```python
def test_shared_client_allows_concurrent_posts(local_json_server):
    client = SiliconFlowHTTPClient(api_key="test", base_url=local_json_server.url)
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda i: client.post("/test", {"i": i}, 1.0), range(20)))
    assert len(results) == 20

def test_timeout_argument_is_enforced(slow_json_server):
    client = SiliconFlowHTTPClient(api_key="test", base_url=slow_json_server.url)
    with pytest.raises(SiliconFlowError, match="timeout"):
        client.post("/test", {}, timeout_seconds=0.05)
```

测试文件内用 `ThreadingHTTPServer` 提供 `local_json_server` 和 `slow_json_server` fixture：前者延迟 50 ms 返回 JSON，后者延迟 500 ms；fixture 在 `yield` 后调用 `shutdown()` 和 `server_close()`。

- [ ] **Step 2: 运行测试并复现当前共享 `HTTPConnection` 错误**

Run: `.venv\Scripts\python.exe -m pytest tests\c1\test_siliconflow_client.py -q`

Expected: FAIL；并发测试出现 `CannotSendRequest/ResponseNotReady` 类错误，或接口尚不存在。

- [ ] **Step 3: 声明并实现 httpx 进程级客户端**

在 `pyproject.toml` 增加直接依赖 `httpx>=0.28,<1`，同步 `uv.lock`。实现使用：

```python
self._client = httpx.Client(
    base_url=base_url.rstrip("/"),
    headers={"Authorization": f"Bearer {api_key}"},
    timeout=httpx.Timeout(timeout_seconds),
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
)
```

每次请求传入本轮剩余预算形成的 timeout；不做隐式指数重试。HTTP 4xx/5xx 与连接/超时错误转换为不含 API key 和完整供应商正文的 `SiliconFlowError`。

- [ ] **Step 4: 保持 embed/rerank 兼容、记录 warmup 并在 lifespan 关闭客户端**

`_post` 只委托共享客户端；`SiliconFlowEmbedder`、`SiliconFlowReranker` 的输入输出不变。启动 warmup 的成功/失败和模型身份保存到 readiness 状态，`/ready` 在 embedding 或 rerank 探测失败时 fail-closed，并只返回脱敏供应商/模型身份。`api_app.lifespan` 退出时调用 `close_siliconflow_http_client()`。

- [ ] **Step 5: 运行 C1 契约与并发测试**

Run: `.venv\Scripts\python.exe -m pytest tests\c1\test_siliconflow_client.py tests\c1 tests\application\test_readiness.py tests\integration\test_api_readiness.py -q`

Expected: PASS；20 路并发零连接状态错误，超时在测试容差内生效，空输入和响应数量校验保持原语义。

- [ ] **Step 6: 静态检查并提交**

Run: `.venv\Scripts\python.exe -m ruff check src\food_agent_v2\c1\siliconflow.py tests\c1\test_siliconflow_client.py`

Commit: `fix(c1): use pooled thread-safe SiliconFlow client`

### Task 3: 建立取消、失锁和提交前统一门卫

**Files:**
- Modify: `src/food_agent_v2/c4/redis_store.py`
- Modify: `src/food_agent_v2/d1/__init__.py:332-365`
- Modify: `src/food_agent_v2/c3/runner.py:1318-1332`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Create: `tests/c3/test_fast_path_cancellation.py`
- Modify: `tests/integration/test_api_application.py`

**Interfaces:**
- Produces: `RedisSessionStore.mark_request_cancelled(request_id: str, ttl_seconds: int = 3600) -> None`
- Produces: `RedisSessionStore.is_request_cancelled(request_id: str) -> bool`
- Produces: `DeterministicRecommendationOrchestrator._guard_active(state, c4, session_id, lock_token, lost) -> WorkflowState | None`

- [ ] **Step 1: 写取消竞争和失锁的失败测试**

```python
def test_cancel_before_commit_never_commits(orchestrator, fake_c4, fake_d1):
    fake_c4.cancel_after("evaluate_recipe_health")
    orchestrator.run("r1", "s1", "晚饭不要辣", [{"participant_ref": "p1", "user_id": "1"}])
    assert fake_d1.status == "cancelled"
    assert fake_c4.committed_menus == []
    assert fake_d1.success_events == []

def test_lost_fencing_token_stops_before_next_tool(orchestrator, fake_c4):
    fake_c4.lose_lock_after("retrieve_recipes")
    orchestrator.run("r1", "s1", "安排晚饭", [{"participant_ref": "p1", "user_id": "1"}])
    assert fake_c4.called_tools == ["retrieve_recipes"]
```

- [ ] **Step 2: 运行失败测试**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_fast_path_cancellation.py -q`

Expected: FAIL，当前确定性路径仍提交或继续调用后续工具。

- [ ] **Step 3: 将取消标记访问收口到 C4 公共方法**

D1 使用 `mark_request_cancelled`；legacy runner 和 fast-path 都使用 `is_request_cancelled`，删除对 `_client` 和硬编码 `v2:cancel:` 的访问。

- [ ] **Step 4: 在所有阶段边界和原子提交前调用统一门卫**

```python
def _guard_active(self, state, c4, session_id, lock_token, lost):
    if c4.is_request_cancelled(state.request_id):
        return reduce_workflow_state(state, action="set_status", status=RequestStatus.CANCELLED)
    if lost.is_set() or not self._session_lock_held(c4, session_id, lock_token):
        return self._fail(state, "SESSION_LOCK_LOST", "会话锁已失效")
    return None
```

在 Context、C1、B2/B4、C2、最终 B4、回答构建和 `atomic_commit` 前调用。任何 guard 返回终态时立即 `_finalize`，不得再调用成功工具或提交。

- [ ] **Step 5: 验证取消、锁和 outbox 不变量**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_fast_path_cancellation.py tests\integration\test_session_lock_and_restore.py tests\integration\test_transactional_outbox.py tests\integration\test_api_application.py -q`

Expected: PASS；取消后无成功菜单、无 `answer_ready/result_committed`，失锁返回精确错误。

- [ ] **Step 6: 提交**

Commit: `fix(c3): stop deterministic workflow on cancel or lock loss`

---

## Level L1 — Agent 决策与多轮完整性

### Task 4: 重建类型化 FastIntentRouter 的 fail-closed 边界

**Files:**
- Modify: `src/food_agent_v2/c3/fast_intent.py`
- Modify: `src/food_agent_v2/contracts/artifacts.py:35-65`
- Modify: `tests/c3/test_fast_intent.py`
- Create: `tests/c3/test_fast_intent_health_boundary.py`

**Interfaces:**
- Produces: `IntentKind = Literal["new_recommendation", "add_constraint", "replace", "reject_plan", "restore", "conflict", "needs_clarification", "model_fallback"]`
- Produces: immutable `IntentDelta` containing `intent`, target identity, additions/removals, time, `preserve_unmentioned_items`, `clarification_reason`, and unresolved-health marker.
- Constraint: 健康关键词只能触发安全路由，不得直接生成疾病医学结论。

- [ ] **Step 1: 写明确、歧义和健康边界的失败测试**

```python
@pytest.mark.parametrize("text", ["我对花生过敏", "最近血压有点高", "二号参与者不能吃虾"])
def test_unresolved_health_language_never_continues_as_plain_recommendation(text):
    result = FastIntentRouter.route(text, participant_refs=("p1", "p2"))
    assert result.intent in {"model_fallback", "needs_clarification"}

def test_not_too_sweet_is_soft_preference():
    result = FastIntentRouter.route("别太甜", participant_refs=("p1",))
    assert result.health_exclusions == ()
    assert "甜" in result.preference_exclusions

def test_relative_multi_person_conflict_requires_clarification():
    result = FastIntentRouter.route("一个人想吃辣，一个人一点辣都不想碰", participant_refs=("p1", "p2"))
    assert result.intent == "conflict"
```

- [ ] **Step 2: 运行测试并确认当前错误输出**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_fast_intent.py tests\c3\test_fast_intent_health_boundary.py -q`

Expected: FAIL；花生过敏被当普通推荐、多人冲突被错误绑定 p1、“别太甜”被升级为硬禁糖。

- [ ] **Step 3: 实现通用安全路由和参与者归属检查**

只对唯一且明确的表达生成确定性 delta；出现健康语义但无法由现有 B2 信号格式唯一表达时设置 `intent="model_fallback"` 和 `unresolved_health_text`。相对称谓“一个人/另一个人”在没有唯一映射时返回冲突或澄清。

- [ ] **Step 4: 补齐中文时间、恢复和目标意图**

支持“十分钟/一刻钟/45分钟”；明确点名当前菜或槽位才能 `replace`；明确整套否定才能 `reject_plan`；明确引用“上一版/刚才版本”才能 `restore`。普通新问题不得因存在当前菜单自动变成追加约束。

- [ ] **Step 5: 运行路由契约测试并提交**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_fast_intent.py tests\c3\test_fast_intent_health_boundary.py -q`

Commit: `fix(c3): make fast intent routing typed and fail-closed`

### Task 5: 实现单次 QueryNormalizer 与明确分流

**Files:**
- Create: `src/food_agent_v2/c3/query_normalizer.py`
- Modify: `src/food_agent_v2/c3/llm_client.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py:208-252`
- Create: `tests/c3/test_query_normalizer.py`
- Modify: `tests/c3/test_orchestrator.py`

**Interfaces:**
- Produces: `QueryNormalizer.normalize(message: str, participant_refs: Sequence[str], current_menu: dict | None, timeout_seconds: float = 3.0) -> IntentDelta`
- Consumes: `FastIntentRouter.route(message: str, participant_refs: Sequence[str]) -> IntentDelta`
- Constraint: 无工具、单次调用、严格 Schema、无自修复循环；失败统一转 `needs_clarification`。

- [ ] **Step 1: 写成功、超时、非 JSON 和健康歧义测试**

```python
def test_normalizer_uses_one_call_and_no_tools(fake_llm):
    current_menu = {"recipe_ids": [1, 2, 3]}
    result = QueryNormalizer(fake_llm).normalize("把清淡些的要求留着，重做主菜", ("p1",), current_menu)
    assert fake_llm.calls == 1
    assert fake_llm.last_tools is None
    assert result.intent == "replace"

@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("bad schema")])
def test_normalizer_failure_becomes_clarification(fake_llm, failure):
    fake_llm.raise_on_call = failure
    result = QueryNormalizer(fake_llm).normalize(
        "给老人换一道更容易咀嚼的菜", ("p1",), current_menu={"recipe_ids": [1, 2, 3]}
    )
    assert result.intent == "needs_clarification"
```

- [ ] **Step 2: 运行失败测试**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_query_normalizer.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 给 LLMClient 增加单次请求 timeout override**

新增可选 `timeout_seconds` 参数，向 OpenAI-compatible 调用传递本轮 3 秒上限；不改变 legacy 默认 timeout，不开启 SDK 自动重试。

- [ ] **Step 4: 实现严格 Schema normalizer 并接入 orchestrator**

处理顺序固定为：确定性 `IntentDelta` → `model_fallback` 时单次 normalizer → Schema/参与者/当前菜单目标校验 → 分流。`needs_clarification/conflict` 直接发布澄清终态，不进入 C1/B4/C2。

- [ ] **Step 5: 验证模型调用上限和分流**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_query_normalizer.py tests\c3\test_orchestrator.py tests\c3\test_runner_chain.py -q`

Expected: PASS；明确场景 0 次模型调用，复杂场景最多 1 次，失败不继续推荐。

- [ ] **Step 6: 提交**

Commit: `feat(c3): add single-call query normalization fallback`

### Task 6: 实现完整多轮 delta 和最小修改

**Files:**
- Create: `src/food_agent_v2/c3/delta_planner.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py:298-380`
- Modify: `src/food_agent_v2/c4/__init__.py`
- Create: `tests/c3/test_delta_planner.py`
- Replace weak assertions in: `tests/c3/test_orchestrator.py:118-146`
- Modify: `tests/integration/test_session_lock_and_restore.py`

**Interfaces:**
- Produces: `DeltaExecutionPlan(intent, dish_count, locked_recipe_ids, rejected_recipe_ids, target_recipe_id, restore_version, restore_recipe_ids)`
- Consumes: validated `IntentDelta`, current committed menu and menu history.
- Produces: exact previous/new `recipe_id` diff for `AuthoritativeAnswerBuilder`.

- [ ] **Step 1: 写逐 recipe_id 最小修改失败测试**

```python
def test_add_constraint_preserves_current_count_and_all_still_valid_dishes():
    plan = DeltaPlanner().plan(
        current_recipe_ids=(1, 2, 3),
        intent=IntentDelta(intent="add_constraint", flavor_preferences=("清淡",)),
        safe_recipe_ids={1, 2, 3, 4},
    )
    assert plan.dish_count == 3
    assert plan.locked_recipe_ids == (1, 2, 3)

def test_replace_changes_only_named_recipe():
    plan = DeltaPlanner().plan(
        current_recipe_ids=(1, 2, 3),
        intent=IntentDelta(intent="replace", target_recipe_id=2),
        safe_recipe_ids={1, 3, 4, 5},
    )
    assert plan.dish_count == 3
    assert plan.locked_recipe_ids == (1, 3)
    assert plan.rejected_recipe_ids == (2,)

def test_restore_binds_existing_committed_version():
    plan = DeltaPlanner(menu_history=committed_history).restore(version="v1")
    assert plan.restore_recipe_ids == committed_history["v1"].recipe_ids
```

- [ ] **Step 2: 运行测试并确认当前菜数和 delta 失败**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_delta_planner.py tests\c3\test_orchestrator.py -q`

Expected: FAIL；`args["dish_count"]` 被忽略或 replace/restore 尚不存在。

- [ ] **Step 3: 实现 DeltaPlanner 和当前菜单/历史绑定**

追加：保留满足所有新硬约束的当前菜；替换：仅解锁目标；否定：将当前 IDs 设为 rejected 并保留参与者/有效约束；恢复：读取已提交版本，不从模型文本重建菜谱身份。

- [ ] **Step 4: 修正 C2 工具参数优先级**

```python
explicit_count = args.get("dish_count")
dish_count = explicit_count if explicit_count is not None else requested_count
hard = MenuHardConstraints(
    dish_count=dish_count or MenuHardConstraints().dish_count,
    locked_recipe_ids=set(args.get("locked_recipe_ids") or []),
    rejected_recipe_ids=set(args.get("rejected_recipe_ids") or []),
)
```

新菜单无论 delta 类型都重新执行全员 B4 最终复核；不得只校验被替换菜。

- [ ] **Step 5: 用 hermetic fake 替换“只断言 completed”的弱测试**

测试必须断言最终 `recipe_ids`、菜数、保留集合、目标差异、B4 调用输入和模型调用次数；不得以“未 completed”证明 legacy fallback 被调用。

- [ ] **Step 6: 运行多轮与恢复测试并提交**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_delta_planner.py tests\c3\test_orchestrator.py tests\integration\test_session_lock_and_restore.py -q`

Commit: `feat(c3): complete deterministic multi-turn deltas`

### Task 7: 收紧工具错误、稳定选优和回答审计

**Files:**
- Modify: `src/food_agent_v2/c3/orchestrator.py:139-204,275-318`
- Modify: `src/food_agent_v2/c3/authoritative_answer.py`
- Create: `tests/c3/test_deterministic_error_mapping.py`
- Create: `tests/c3/test_authoritative_answer.py`
- Modify: `tests/c3/test_artifact_grounding.py`

**Interfaces:**
- Produces: `require_tool_result(name: str, result: object, expected_receipt: str) -> object`
- Produces: deterministic selection ordered by `(-total_score, plan_id)`.
- Consumes: previous menu and exact delta diff for authoritative change summary.

- [ ] **Step 1: 写工具错误、tie-break 和回答绑定失败测试**

```python
def test_generate_tool_error_is_failed_not_no_feasible_menu():
    state = run_with_tool_result("generate_feasible_menus", {"error": "db unavailable"})
    assert state.status == "failed"
    assert state.error.error_code == "TOOL_EXECUTION_FAILED"

def test_equal_scores_use_stable_plan_id_tiebreak():
    assert select_best([plan("b", 1.0), plan("a", 1.0)]).plan_id == "a"

def test_delta_answer_names_exact_changes():
    answer = build_answer(previous=(1, 2, 3), current=(1, 4, 3))
    assert answer.changed_recipe_ids == (2, 4)
```

- [ ] **Step 2: 运行失败测试**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_deterministic_error_mapping.py tests\c3\test_authoritative_answer.py -q`

- [ ] **Step 3: 在解释业务终态前统一验证每个必需工具结果**

检索、健康审查、菜单生成和最终校验均先检查异常 dict、缺失 receipt、request/build/node 身份和输出数量；只有已验证的 `note` 可以转换为 `no_safe_menu/no_feasible_menu/strict_time_indeterminate`。

- [ ] **Step 4: 执行稳定选优、回答绑定和 ReviewArtifact hash**

选优使用 `sorted(plans, key=lambda p: (-p.total_score, p.plan_id))[0]`。构建回答后调用现有 `validate_answer_menu_binding(answer, decision, final)`；`ReviewArtifact` 使用规范 `_content_hash`，不得保留全零 hash。

- [ ] **Step 5: 运行 Artifact、安全和 orchestrator 测试**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_deterministic_error_mapping.py tests\c3\test_authoritative_answer.py tests\c3\test_artifact_grounding.py tests\c3\test_orchestrator.py -q`

- [ ] **Step 6: 提交**

Commit: `fix(c3): preserve deterministic error and artifact semantics`

---

## Level L2 — 性能证据与竞赛门禁

### Task 8: 完成双 TTFT、请求预算和 SSE 性能 harness

**Files:**
- Modify: `src/food_agent_v2/d1/schemas.py`
- Modify: `src/food_agent_v2/d1/__init__.py`
- Modify: `src/food_agent_v2/c3/perf.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify: `scripts/perf_harness.py`
- Create: `tests/c3/test_performance_budget.py`
- Create: `tests/performance/test_perf_harness.py`
- Modify: `tests/integration/test_api_application.py`

**Interfaces:**
- Produces: `SSEEventType.ANSWER_STARTED`
- Produces: `PerformanceBudget.remaining_seconds()`, `require(stage)`, `allow_optional(seconds)`.
- Produces metrics: `visible_ttft_ms`, `authoritative_ttft_ms`, `e2e_ms`, `multi_turn_avg_ms`.
- Produces: `measure_turn(event_stream)`, `evaluate_turn(status, visible_ttft_ms, authoritative_ttft_ms, e2e_ms)`, `multi_turn_average(turn_times)` and CLI `--output-json`.

- [ ] **Step 1: 写双 TTFT、失败不计通过和多轮平均测试**

```python
def test_harness_distinguishes_visible_and_authoritative_ttft(fake_sse):
    fake_sse.emit(0.1, "answer_started")
    fake_sse.emit(1.2, "answer_ready")
    result = measure_turn(fake_sse)
    assert result.visible_ttft_ms == 100
    assert result.authoritative_ttft_ms == 1200

def test_failed_turn_never_counts_as_performance_pass():
    result = evaluate_turn(
        status="failed",
        visible_ttft_ms=10,
        authoritative_ttft_ms=None,
        e2e_ms=20,
    )
    assert result.passed is False

def test_multi_turn_average_is_per_case():
    assert multi_turn_average([1000, 3000, 2000]) == 2000
```

- [ ] **Step 2: 运行失败测试**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_performance_budget.py tests\performance\test_perf_harness.py -q`

Expected: FAIL，当前 harness 不消费 SSE，且没有双 TTFT 字段。

- [ ] **Step 3: 实现请求级单调时钟预算和 `answer_started`**

在 C1、QueryNormalizer、B2/B4/C2、回答和提交阶段记录时间。`answer_started` 只能包含不承诺菜单的文本；`answer_ready` 只在最终校验和原子提交后发布。外部调用 timeout 取阶段上限与请求剩余预算的较小值。

- [ ] **Step 4: 重写 harness 为 SSE 驱动并校验业务结果**

每轮 POST 后立即订阅 SSE，记录首个 `answer_started` 和 `answer_ready`；终态必须符合该用例预期且菜单身份存在，才计算性能通过。输出 JSON 和 Markdown，逐用例包含 status、两个 TTFT、E2E、模型调用次数和失败原因。

- [ ] **Step 5: 验证性能工具自身**

Run: `.venv\Scripts\python.exe -m pytest tests\c3\test_performance_budget.py tests\performance\test_perf_harness.py tests\integration\test_api_application.py -q`

Expected: PASS；测试使用 fake SSE/clock，不访问外部 API。

- [ ] **Step 6: 提交**

Commit: `feat(perf): measure authoritative TTFT and enforce budgets`

---

## Level L3 — 真实验收、灰度和交付

### Task 9: 运行正式验收并切换默认模式

**Files:**
- Create: `reports/2026-08-14-fast-path-performance-acceptance.md`
- Modify: `docs/modules/07-rag-retrieval.md`
- Modify: `docs/modules/09-agent-workflow.md`
- Modify: `docs/modules/11-api-and-sse.md`
- Modify: `docs/modules/13-testing-and-acceptance.md`
- Modify: `.env.example`
- Modify after all gates pass: `src/food_agent_v2/d1/__init__.py:491-500`

**Interfaces:**
- Consumes: Tasks 1–8 全部接口和测试。
- Produces: 逐用例验收报告、回滚演练记录、最终默认模式决定。
- Constraint: 此 Task 才允许真实 SiliconFlow/LLM API 和隔离 MySQL/Redis/Qdrant 环境。

- [ ] **Step 1: 运行静态检查和全部非 live 测试**

Run: `.venv\Scripts\python.exe -m ruff check src tests scripts`

Run: `.venv\Scripts\python.exe -m pytest -q -m "not live"`

Expected: 全部 PASS；不得把缺少 API 服务的 setup error 报告成通过。

- [ ] **Step 2: 建立隔离验收命名空间并 warmup**

设置独立数据库/Redis key prefix/outbox 前缀和唯一请求前缀，显式设置 `WORKFLOW_MODE=fast_path`。启动后确认 MySQL、Redis、Qdrant、SiliconFlow embedding/rerank readiness 和一次不计分 warmup 成功。

- [ ] **Step 3: 跑 20 组真实 API 对话**

Run: `.venv\Scripts\python.exe scripts\perf_harness.py --cases data\raw\对话用例.json --api http://localhost:8001 --max-wait 30 --output-json reports\fast-path-performance.json`

Expected: 每个预期成功场景真实 completed；硬约束零违反；`visible_ttft_ms < 5000`；单轮 `e2e_ms < 15000`；每组多轮平均 `< 12000`；逐例披露 `authoritative_ttft_ms`。

- [ ] **Step 4: 跑合成 Agent 多轮与并发/取消验收**

至少覆盖局部替换、整套否定、模糊追问、需求冲突、上下文恢复、临时过敏信号、20 路并发检索、SSE 无事件等待、跨 worker 事件刷新和取消提交竞争。功能用例必须通过；不得为了速度降级为纯词法或跳过 B4。

- [ ] **Step 5: 执行回滚演练**

依次执行 `fast_path → legacy → fast_path`，验证同一 ready build、固定数据和数据库 Schema 未变化；每次模式切换后完成一个单轮请求和一个多轮追加请求。

- [ ] **Step 6: 写验收报告并做默认模式决定**

报告必须包含环境身份、warmup、逐例指标、失败样本、模型/API 调用次数、回滚结果和门禁结论。任何一条合格线或安全门禁失败时保持 `src/food_agent_v2/d1/__init__.py` 的默认 `legacy`；全部通过时才把默认值改为 `fast_path`，同时保留显式 `legacy` 开关。同步在 `.env.example` 增加 `WORKFLOW_MODE=fast_path`，删除已不属于在线链路的 `BGE_MODEL_PATH`、`RERANKER_MODEL_PATH`、`MODEL_LOW_MEMORY_MODE` 和 `MODEL_DEVICE` 示例项。

- [ ] **Step 7: 同步模块文档并提交**

Commit: `docs: record fast-path acceptance and rollout decision`

## Deferred Backlog

1. `NarrativePolisher`：仅在 Task 9 已达到性能优秀线、默认关闭且有至少 2.5 秒剩余预算时另立任务；它不能阻塞权威回答。
2. 按人营养摄入：需要独立的数据覆盖、份量模型、Schema、回答契约和医学/竞赛验收设计，不属于本计划。
3. 本地 embedding/rerank、CPU/GPU 切换：当前无收益证据且会扩大部署与索引一致性范围，不进入竞赛交付路径。
4. legacy 删除：竞赛交付前不删除；只有 fast_path 稳定运行并完成回滚观察期后再单独评估。
