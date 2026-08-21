"""离线整菜时间图模型适配器；运行时与普通 rebuild 均不调用模型。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import BaseModel

from food_agent_v2.b1.schemas import StepAtom
from food_agent_v2.b1.time_graph_profiler import (
    GeneratedTimeGraph,
    GraphValidationError,
    RecipeTimeProfile,
    TimeGraphCache,
    VerifierResult,
    load_cached_recipe_time_graph,
    profile_recipe_time_graph,
    time_graph_cache_key,
    time_graph_pipeline_model_id,
)
from food_agent_v2.core.config import load_config
from food_agent_v2.core.paths import PROJECT_ROOT

GENERATOR_SYSTEM_PROMPT = """你是菜谱原子步骤时间图生成器。输入是一道菜的完整原子步骤。
必须原样覆盖每个 atom_id，不能删除、合并、增加或改写步骤。对每个 atom 输出：
duration_seconds、task_type、resources、depends_on。只给单一秒数，不给区间、置信度、来源或解释。
task_type 仅可为 manual、attended_equipment、unattended_equipment、passive、non_task。
resource 仅可为 cook、burner、oven、steamer、microwave、blender、fridge、counter。
manual 使用 cook；attended_equipment 使用 cook 和设备；unattended_equipment 只使用设备；
passive 只可为空或 fridge/counter；non_task 必须为 0 且资源为空。
资源组合必须严格遵守：manual 只能 ["cook"] 或 ["cook","counter"]；
attended_equipment 必须含 "cook" 且另含 burner/oven/steamer/microwave/blender 中至少一个，不能含 counter；
unattended_equipment 只能含上述设备且不能含 cook/counter；passive 只能是 []、["counter"] 或 ["fridge"]；
只有输入 explicit_duration_seconds=0 的 atom 才能标 non_task，其他 atom 绝不能标 non_task。
“至其入味/至食材熟透/发酵/静置/浸泡/冷藏/冷冻/醒发/重复步骤/预热结束后”等都不是
non_task；它们是等待、设备或手工任务，必须给正整数时长。不要把步骤片段或状态词因为看起来
不完整就标成 non_task。explicit_duration_seconds 为 null 时也绝不能标 non_task。
尤其是“至其入味、至食材入味、至食材熟透、使面团全部变硬”必须按整道菜上下文估算
1 到 21600 秒内的正整数等待时长，禁止返回 0。
显式 duration_locked 步骤仍返回一个值，但程序会以原文解析值为准。缺失时长必须估算一个正整数秒数。
manual/attended 估算不超过 21600 秒，unattended/passive 估算不超过 604800 秒。
依赖必须表达真实先后关系，不能只为了顺序而阻止本可并行的任务。
顶层 JSON 必须且只能是 {"tasks":[任务对象,...]}，不得使用 atoms 等其他顶层键。"""

VERIFIER_SYSTEM_PROMPT = """你是独立的菜谱时间任务图复核器。核对原始 atoms 与候选 step_tasks：
是否忠实覆盖步骤、是否漏掉等待、任务类型/资源是否合理、依赖是否错误允许并行、时长是否明显失真。
只能返回 issues；code 仅可为 STEP_MISMATCH、MISSING_WAIT、INVALID_PARALLELISM、
TASK_TYPE_MISMATCH、RESOURCE_MISMATCH、DEPENDENCY_MISMATCH、DURATION_IMPLAUSIBLE；
atom_ids 只填相关 atom ID。没有问题时返回空 issues。不得返回自由解释。
顶层 JSON 必须且只能是 {"issues":[问题对象,...]}。"""


class LLMStructuredModel:
    """把现有 LLMClient 适配为一次整 JSON 对象调用。"""

    def __init__(
        self,
        *,
        client,
        model_id: str,
        role: str,
        system_prompt: str,
        output_model: type[BaseModel],
        schema_name: str,
    ):
        self.client = client
        self.model_id = model_id
        self.role = role
        self.system_prompt = system_prompt
        self.output_model = output_model
        self.schema_name = schema_name
        self.schema_prompt = json.dumps(
            output_model.model_json_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # DeepSeek's OpenAI-compatible endpoint supports JSON Object mode but rejects
        # response_format=json_schema. Pydantic below remains the strict schema gate.
        self.response_format = {"type": "json_object"}

    def generate(self, payload: dict) -> dict:
        response = self.client.invoke(
            self.role,
            (
                f"{self.system_prompt}\n输出对象 {self.schema_name} 必须逐字段满足以下"
                f"完整 JSON Schema；required 数组中的字段即使为空也必须输出："
                f"{self.schema_prompt}"
            ),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            response_format=self.response_format,
        )
        content = response.get("content", "")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("结构化模型必须返回 JSON 对象")
        return parsed


def create_time_graph_models(client=None) -> tuple[LLMStructuredModel, LLMStructuredModel]:
    """创建提示词独立的 generator 与 verifier 调用器。"""
    if client is None:
        from food_agent_v2.c3.llm_client import get_llm_client

        client = get_llm_client()
    model_id = load_config().llm.model_for_role("unified_review") or "unconfigured"
    generator = LLMStructuredModel(
        client=client,
        model_id=model_id,
        role="unified_review",
        system_prompt=GENERATOR_SYSTEM_PROMPT,
        output_model=GeneratedTimeGraph,
        schema_name="recipe_time_graph",
    )
    verifier = LLMStructuredModel(
        client=client,
        model_id=model_id,
        role="unified_review",
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        output_model=VerifierResult,
        schema_name="recipe_time_graph_verification",
    )
    return generator, verifier


def generate_time_graph_review(
    recipes: tuple[tuple[int, str, tuple[StepAtom, ...]], ...],
    *,
    output: Path,
    cache_path: Path | None = None,
    generator=None,
    verifier=None,
    max_workers: int = 8,
    checkpoint_size: int = 25,
    max_attempts: int = 2,
) -> dict:
    """生成/复用时间图缓存，仅将程序或 verifier 冲突写入审阅输出。"""
    if generator is None or verifier is None:
        default_generator, default_verifier = create_time_graph_models()
        generator = generator or default_generator
        verifier = verifier or default_verifier
    if max_attempts < 1 or max_attempts > 2:
        raise ValueError("时间图每道菜只允许 1 或 2 次生成尝试")
    cache = TimeGraphCache(
        cache_path or PROJECT_ROOT / "data" / "cache" / "recipe_time_graphs.jsonl"
    )
    ready: list[RecipeTimeProfile] = []
    conflicts: list[dict] = []
    pending: list[tuple[int, str, tuple[StepAtom, ...], str]] = []
    pipeline_model_id = time_graph_pipeline_model_id(generator.model_id, verifier.model_id)
    for recipe_id, recipe_name, atoms in recipes:
        cache_key = time_graph_cache_key(recipe_id, atoms, pipeline_model_id)
        try:
            ready.append(
                load_cached_recipe_time_graph(
                    recipe_id=recipe_id,
                    recipe_name=recipe_name,
                    atoms=atoms,
                    generator_model_id=generator.model_id,
                    verifier_model_id=verifier.model_id,
                    cache=cache,
                )
            )
            continue
        except Exception as exc:
            from food_agent_v2.b1.time_graph_profiler import TimeGraphCacheMiss

            if not isinstance(exc, TimeGraphCacheMiss):
                conflicts.append(_time_graph_conflict(recipe_id, recipe_name, exc))
                continue
        pending.append((recipe_id, recipe_name, atoms, cache_key))

    checkpoint: dict[str, RecipeTimeProfile] = {}

    def generate_one(recipe_id: int, recipe_name: str, atoms: tuple[StepAtom, ...]):
        last_error = None
        for _ in range(max_attempts):
            try:
                return profile_recipe_time_graph(
                    recipe_id=recipe_id,
                    recipe_name=recipe_name,
                    atoms=atoms,
                    model=generator,
                    verifier=verifier,
                    cache=TimeGraphCache(),
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
        future_rows = {
            executor.submit(generate_one, recipe_id, recipe_name, atoms): (
                recipe_id,
                recipe_name,
                cache_key,
            )
            for recipe_id, recipe_name, atoms, cache_key in pending
        }
        for future in as_completed(future_rows):
            recipe_id, recipe_name, cache_key = future_rows[future]
            try:
                profile = future.result()
                ready.append(profile)
                checkpoint[cache_key] = profile
                if len(checkpoint) >= checkpoint_size:
                    cache.put_many(checkpoint)
                    checkpoint.clear()
            except Exception as exc:
                conflicts.append(_time_graph_conflict(recipe_id, recipe_name, exc))
    if checkpoint:
        cache.put_many(checkpoint)

    conflicts.sort(key=lambda item: int(item["recipe_id"]))
    ready.sort(key=lambda item: item.recipe_id)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for conflict in conflicts:
            handle.write(json.dumps(conflict, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "total": len(recipes),
        "ready": len(ready),
        "conflicts": len(conflicts),
        "cache_entries": len(cache),
        "output": str(output),
    }


def _time_graph_conflict(recipe_id: int, recipe_name: str, exc: Exception) -> dict:
    if isinstance(exc, GraphValidationError):
        return {
            "recipe_id": recipe_id,
            "recipe_name": recipe_name,
            "issues": [
                {"code": code, "atom_ids": list(atom_ids)}
                for code, atom_ids in exc.issues
            ],
        }
    return {
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "issues": [{"code": "MODEL_CALL_FAILED", "atom_ids": []}],
        "error_type": type(exc).__name__,
    }


def run(*, sample: int | None = None, **_: object) -> dict:
    """保留旧命令名的确定性迁移提示，避免继续写旧分钟画像。"""
    return {
        "status": "migrated",
        "sample": sample,
        "command": (
            "food-agent-v2 data-review --kind time-graphs "
            "--output reports/data_review/time_graph_conflicts.jsonl"
        ),
    }
