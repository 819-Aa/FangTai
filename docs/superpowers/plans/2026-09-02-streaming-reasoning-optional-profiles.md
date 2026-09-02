# Streaming Reasoning and Optional Profiles Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让用户在零/一/多健康档案下都能发起推荐，并在同一助手气泡中实时看到基于已验证事实的自然讲解和最终答案流。

**Architecture:** D1 将空档案请求内部映射为 `guest -> None`，C3/C4/B4 保留 guest 的临时健康约束但跳过 B2 永久档案加载。B2 向 D1 提供只含安全摘要的 50 份档案选项。C3 新增独立 `ReasoningNarrator`，仅消费白名单事实快照，在三个既有工件检查点发布增量 SSE；权威菜单、健康约束和最终提交逻辑保持不变。前端以序号去重累积讲解与答案，并将档案选择与 session 绑定组合分开持久化。

**Tech Stack:** Python 3.11、FastAPI、OpenAI-compatible Qwen API、MySQL/Qdrant/Redis、pytest；Vue 3、Pinia、TypeScript、Vitest、Playwright。

---

## Global constraints

- 公共请求只允许 `p1..p50`；`guest` 只能由服务端为 `participants=[]` 创建，公共端显式提交必须 422。
- `guest` 不读取固定档案、不产生永久健康约束，但聊天中验证通过的临时过敏/疾病/禁忌仍进入 C4 和 B4。
- 讲解只描述结构化事实、选择与取舍，不输出原始提示词、模型隐藏思维链、用户健康明细或内部标识。
- 最终校验之前不得声称“安全”“已经生成/提交”；`result_committed` 仍是唯一提交成功依据。
- `answer_delta` 拼接值必须与最终 `answer_ready.text` 完全相等；模型讲解失败不得使推荐失败。
- 不重建数据库，不修改 H07 数据构建，不改变既有 RAG、B4 健康规则和 outbox 权威提交契约。

## Task 1: Add safe health-profile options API

**Files:**

- Modify: `src/food_agent_v2/b2/service.py`
- Modify: `src/food_agent_v2/api_app.py`
- Test: `tests/b2/test_profile_options.py`
- Test: `tests/integration/test_api_application.py`

### Step 1: Write failing B2 projection tests

Add tests using `InMemoryUserProfileSource` to assert:

```python
catalog = service.list_public_profile_options()
assert catalog["build_id"] == "build-test"
assert len(catalog["items"]) == 50
assert catalog["items"][0] == {
    "participant_ref": "p1",
    "label": "档案 1",
    "gender": "男",
    "age": 30,
    "constraint_count": expected_count,
}
assert not ({"user_id", "allergies", "diseases", "health_metrics"} & catalog["items"][0].keys())
```

Also assert invalid or mixed build data still fails through the existing B2 repository validation.

### Step 2: Run the focused test and confirm failure

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\b2\test_profile_options.py -q
```

Expected: fail because `list_public_profile_options` does not exist.

### Step 3: Implement the minimal B2 projection

In `UserHealthProfileService`:

```python
def list_public_profile_options(self) -> dict:
    if not self._loaded:
        self.load()
    build_ids = {str(user.get("build_id") or "") for user in self._users.values()}
    if len(build_ids) != 1 or "" in build_ids:
        raise HealthProfileError("BUILD_IDENTITY_MISMATCH", "档案 build_id 不唯一")
    items = []
    for uid in sorted(self._users):
        user = self._users[uid]
        validation = self.validate_profile(uid)
        if not validation.valid:
            raise HealthProfileError("PROFILE_INVALID", f"档案 {uid} 无效")
        items.append({
            "participant_ref": f"p{uid}",
            "label": f"档案 {uid}",
            "gender": user["gender"],
            "age": user["age"],
            "constraint_count": validation.derived_constraint_count,
        })
    return {"build_id": next(iter(build_ids)), "items": items, "total": len(items)}
```

The method returns a new projection only; it must not mutate the loaded records.

### Step 4: Add the FastAPI route and endpoint tests

Add `GET /v1/health-profile-options`. Construct `UserHealthProfileService`, call the projection, and translate B2/repository failures to a stable 503 response without database details. In integration tests monkeypatch the service method and assert exact safe keys and total 50.

Keep `/v1/users` unchanged for compatibility.

### Step 5: Verify and commit

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\b2\test_profile_options.py tests\integration\test_api_application.py -q
git add src/food_agent_v2/b2/service.py src/food_agent_v2/api_app.py tests/b2/test_profile_options.py tests/integration/test_api_application.py
git commit -m "feat: expose safe health profile options"
```

## Task 2: Support zero profiles with an internal guest participant

**Files:**

- Modify: `src/food_agent_v2/d1/schemas.py`
- Modify: `src/food_agent_v2/d1/__init__.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Modify: `src/food_agent_v2/c3/runner.py`
- Modify: `src/food_agent_v2/c3/tool_handler.py`
- Modify: `src/food_agent_v2/c4/__init__.py`
- Test: `tests/d1/test_request_validation.py`
- Test: `tests/c4/test_context_manifest.py`
- Test: `tests/c3/test_tool_handler.py`
- Test: `tests/c3/test_orchestrator.py`

### Step 1: Write failing request-boundary tests

Add assertions:

```python
assert validate_create_request({
    "idempotency_key": "zero-profile", "participants": [], "message": "清淡晚餐"
}) is None

resolved, errors = resolve_participants([])
assert errors == []
assert resolved == [{"participant_ref": "guest", "label": "当前用户", "user_id": None}]

_, errors = resolve_participants([{"participant_ref": "guest"}])
assert errors
```

Retain existing rejection tests for `user_id`, invalid `pN`, duplicate refs, and non-list values.

### Step 2: Run and confirm the focused tests fail

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\d1\test_request_validation.py -q
```

Expected: empty participants is rejected and no guest is produced.

### Step 3: Implement the D1 guest boundary

- `validate_create_request` accepts an empty list but still rejects missing/non-list/invalid items.
- `resolve_participants([])` returns the single internal guest object above.
- Public response/status/SSE projection must continue stripping `user_id`; no payload may expose `guest` as a selectable option.
- Session creation keeps its public `participant_refs=[]`; only recommendation execution receives guest.

### Step 4: Write failing guest propagation tests

Cover these behaviors with fakes:

- `B2PermanentConstraintLoader.load({"guest": None})` does not call `derive_constraints` and returns no permanent constraints.
- `ToolHandler` profile-preference and permanent-constraint branches skip `None`.
- Candidate and final B4 evaluation still merge C4 temporary constraints whose `participant_ref == "guest"`.
- A normal zero-profile request reaches the deterministic orchestrator without calling `int(None)`.

### Step 5: Implement nullable mappings minimally

Change relevant annotations to `dict[str, int | None]`. Build mappings as:

```python
user_id_mapping = {
    p["participant_ref"]: int(p["user_id"]) if p.get("user_id") is not None else None
    for p in participants
}
```

At every B2 fixed-profile call, filter `uid is not None`. Always preserve every mapping key when merging C4 temporary constraints or constructing the B4 participant map. Do not add a second guest abstraction.

### Step 6: Verify and commit

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\d1\test_request_validation.py tests\c4\test_context_manifest.py tests\c3\test_tool_handler.py tests\c3\test_orchestrator.py -q
git add src/food_agent_v2/d1 src/food_agent_v2/c3 src/food_agent_v2/c4 tests/d1 tests/c3 tests/c4
git commit -m "feat: support recommendations without saved profiles"
```

## Task 3: Add reasoning and answer delta SSE contracts

**Files:**

- Modify: `src/food_agent_v2/d1/schemas.py`
- Modify: `src/food_agent_v2/d1/__init__.py`
- Test: `tests/d1/test_sse_replay.py`
- Test: `tests/integration/test_api_application.py`

### Step 1: Write failing SSE contract tests

Add tests publishing:

```python
api.publish_reasoning_delta(rid, "query", 0, "我先确认一下需求。")
api.publish_reasoning_completed(rid, "query")
api.publish_answer_delta(rid, 0, "推荐")
api.publish_answer_delta(rid, 1, "如下")
```

Assert exact event names and payloads, stable unique event IDs, replay after `Last-Event-ID`, and forbidden-field scanning. Re-publishing the same segment/sequence must not create a second fact.

### Step 2: Run and confirm failure

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\d1\test_sse_replay.py tests\integration\test_api_application.py -q
```

Expected: event enum and publish methods do not exist.

### Step 3: Implement the three event types and publishers

Add enum values `REASONING_DELTA`, `REASONING_COMPLETED`, `ANSWER_DELTA` and methods:

```python
def publish_reasoning_delta(self, request_id: str, segment_id: str,
                            sequence: int, delta: str) -> None: ...
def publish_reasoning_completed(self, request_id: str, segment_id: str) -> None: ...
def publish_answer_delta(self, request_id: str, sequence: int, delta: str) -> None: ...
```

Use deterministic IDs `reasoning:{segment_id}:{sequence}`, `reasoning-complete:{segment_id}`, and `answer-delta:{sequence}` scoped by request in the event store. Reject empty delta, negative/non-integer sequence, invalid segment IDs, and forbidden fields before emission.

### Step 4: Verify and commit

Run the focused test command from Step 2, then:

```powershell
git add src/food_agent_v2/d1 tests/d1/test_sse_replay.py tests/integration/test_api_application.py
git commit -m "feat: add streaming reasoning SSE contracts"
```

## Task 4: Narrate validated facts at three workflow checkpoints

**Files:**

- Create: `src/food_agent_v2/c3/reasoning_narrator.py`
- Modify: `src/food_agent_v2/c3/llm_client.py`
- Modify: `src/food_agent_v2/c3/orchestrator.py`
- Test: `tests/c3/test_reasoning_narrator.py`
- Test: `tests/c3/test_orchestrator.py`

### Step 1: Write failing narrator unit tests

Define an injected fake LLM and assert:

- only the keys allowed for `query`, `candidate_review`, and `final_choice` are serialized;
- model chunks are normalized and emitted with zero-based sequence;
- model exception, timeout, or empty content yields one deterministic fact-based fallback segment;
- pre-validation checkpoints reject/replace phrases that claim safety or submission;
- no narration result can mutate its input snapshot.

### Step 2: Run and confirm failure

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\c3\test_reasoning_narrator.py -q
```

Expected: module does not exist.

### Step 3: Add minimal text streaming to the existing Qwen client

Add:

```python
def stream_text(self, role: str, system_prompt: str,
                user_message: str, timeout_seconds: float | None = None):
    ...
```

Use the existing OpenAI client/config and `model_for_role(role)`, call `chat.completions.create(..., stream=True)`, and yield only non-empty `choice.delta.content`. Keep the existing non-streaming `invoke` behavior unchanged.

### Step 4: Implement `ReasoningNarrator`

Expose:

```python
class ReasoningNarrator:
    def publish(self, request_id: str, checkpoint: str,
                facts: dict[str, object]) -> str: ...
```

The method validates the checkpoint whitelist, asks Qwen for a short first-person Chinese paragraph, publishes each returned chunk through D1, then publishes `reasoning_completed`. On any failure it publishes the checkpoint-specific deterministic fallback and returns that text. It never raises into the workflow.

### Step 5: Hook the three existing artifact boundaries

In `DeterministicRecommendationOrchestrator`, publish after:

1. semantic rewrite + `QueryPlanArtifact` creation;
2. RAG retrieval and B4 candidate audit receipt;
3. `FinalValidationArtifact` and selected menu are complete.

Build each snapshot explicitly from public counts, requested constraints, excluded candidate counts, selected public recipe names, and validation outcome. Do not pass whole artifacts, context manifests, profile records, prompts, receipts, or user IDs.

### Step 6: Stream the authoritative answer without changing commit order

Before `answer_ready`, split the already-produced authoritative answer into lossless chunks and publish sequential `answer_delta` events. Assert in the orchestrator test:

```python
assert "".join(answer_deltas) == answer_ready["text"]
```

This is intentionally lossless chunk publication of the authoritative answer; narration uses true provider streaming. Keep `answer_ready` and outbox `result_committed` unchanged.

### Step 7: Verify and commit

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\c3\test_reasoning_narrator.py tests\c3\test_orchestrator.py tests\d1\test_sse_replay.py -q
git add src/food_agent_v2/c3 src/food_agent_v2/d1 tests/c3 tests/d1
git commit -m "feat: stream user-facing recommendation reasoning"
```

## Task 5: Replace anonymous slots with optional profile selection

**Files:**

- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/stores/recommendation.ts`
- Modify: `frontend/src/features/participants/ParticipantContext.vue`
- Modify: `frontend/src/App.vue`
- Test: `frontend/src/features/participants/ParticipantContext.test.ts`
- Test: `frontend/src/stores/recommendation.test.ts`

### Step 1: Write failing component and store tests

Cover:

- profile options load and render `档案 N / 性别 / 年龄 / 约束数`;
- searchable multi-select, remove chip, disabled state;
- default selected refs is empty and `canSend` is true when idle;
- no selection sends `participants: []`;
- exact one/many refs are sent when selected;
- changing selection clears `currentMenu`, closes an active stream, and next send creates a new session;
- old `v2.session_*` values are not restored by the upgraded storage namespace.

### Step 2: Run and confirm failure

Run:

```powershell
Set-Location frontend
npm test -- --run src/features/participants/ParticipantContext.test.ts src/stores/recommendation.test.ts
```

Expected: profile API/types/actions and zero-profile sending do not exist.

### Step 3: Add client types and loading action

Add `HealthProfileOption` / `HealthProfileOptionsResponse` types and:

```ts
export function getHealthProfileOptions(): Promise<HealthProfileOptionsResponse>
```

Store state becomes `profileOptions`, `selectedProfileRefs`, `profilesLoading`; preserve `sessionRefs` as the refs bound to the current session. Use only new `v3.session_id`, `v3.session_refs`, and `v3.selected_profile_refs` keys so old auto-`p1` state cannot hydrate.

### Step 4: Implement optional profile selector

Replace add/remove anonymous controls with a compact searchable multi-select and selected chips. No form fields and no profile-health detail expansion. On mount call `loadProfileOptions()` and keep the default selection empty.

Selection change must call a single store action that closes active connections, clears current request/menu/clarification, clears the old session binding, saves the selected refs, and waits to create the new session until send.

### Step 5: Verify and commit

Run the focused frontend tests, then:

```powershell
npm run build
Set-Location ..
git add frontend/src
git commit -m "feat: add optional health profile selection"
```

## Task 6: Render reasoning and answer streams in one assistant bubble

**Files:**

- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/stores/recommendation.ts`
- Modify: `frontend/src/features/chat/ChatPanel.vue`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/stores/recommendation.test.ts`
- Test: `frontend/src/features/chat/ChatPanel.test.ts`
- Test: `frontend/e2e/browser.spec.ts`

### Step 1: Write failing stream reducer tests

Add tests that send duplicate and out-of-order events:

```ts
{ event: "reasoning_delta", data: { segment_id: "query", sequence: 0, delta: "我先确认需求。" } }
{ event: "answer_delta", data: { sequence: 0, delta: "推荐" } }
```

Assert `(segment_id, sequence)` and answer sequence are independently deduplicated, earlier sequence order wins, `answer_ready` replaces/reconciles answer text authoritatively, and `result_committed` alone sets the completed menu state.

### Step 2: Write failing presentation tests

Assert the assistant bubble shows:

- a “分析与取舍” section while reasoning streams;
- a “推荐结果” section while answer deltas stream;
- both sections in the same bubble;
- no main “执行阶段” list;
- “菜单已生成” only after `result_committed`.

### Step 3: Run and confirm failure

Run:

```powershell
Set-Location frontend
npm test -- --run src/stores/recommendation.test.ts src/features/chat/ChatPanel.test.ts
```

Expected: the new events are not subscribed or rendered.

### Step 4: Implement SSE consumption and store state

Subscribe to `reasoning_delta`, `reasoning_completed`, `answer_delta`, and the existing `answer_started`. Extend the assistant message with `reasoning` and `answer` fields, plus store-private seen-sequence sets keyed by the active assistant/request. Reset them at every send.

For `answer_ready`, compare its full text with accumulated answer and replace with the authoritative full text if needed. Poll fallback continues to use `result_summary.answer.text`.

### Step 5: Implement the conversational UI

Render streamed reasoning and answer inside the same assistant bubble with subtle labels. Keep a small neutral loading indicator only before the first delta. Remove the phase panel from the primary UI; `phases` remains in store for debugging and terminal semantics.

Update empty copy to explain that health profiles are optional and health information can be typed directly in the chat box.

### Step 6: Run full verification

Backend:

```powershell
Set-Location ..
& .\.venv\Scripts\python.exe -m pytest tests\d1 tests\b2 tests\c3 tests\c4 tests\integration\test_api_application.py -q
```

Frontend:

```powershell
Set-Location frontend
npm test -- --run
npm run build
npm run test:e2e -- --project=chromium
```

H07 smoke test after restarting the API:

1. `GET /ready` is ready and `GET /v1/health-profile-options` returns the current build plus 50 safe options.
2. No profile + taste-only message completes.
3. No profile + explicit allergy/disease message produces guest temporary constraints and a health-validated menu or an honest no-safe terminal.
4. One profile loads permanent constraints.
5. Multiple profiles enforce all participants; ambiguous unqualified health ownership returns clarification.
6. Temporarily force narrator failure and confirm fallback text streams while recommendation still completes.

### Step 7: Commit and record completion

```powershell
Set-Location ..
git add frontend tests docs
git commit -m "feat: complete conversational streaming recommendation UI"
```

Write a Chinese verification report containing: implemented items, test commands/results, H07 scenario outcomes, commits, remaining known limitations, and the frontend/backend entry URLs. Do not claim completion until all required verification commands have fresh passing output.
