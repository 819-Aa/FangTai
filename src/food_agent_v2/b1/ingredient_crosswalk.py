"""食材 crosswalk 决策（T06 修复）。

把源词条（候选名）映射到标准食材身份。决策类型：
- merge：多个源词条归一到一个身份（同义/形态变体）；
- split：组合引用拆成子身份；
- discard：非食用/非食材文本，不进入注册表。

候选生成器只产生 pending 决策，无模型自动批准；人工（H02）在
data/review/ingredient_identity_overrides.csv 签名 approve 或 reject，
rejected 候选保留独立身份并清除 pending。crosswalk 产物携带审核者签名。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

CrosswalkOperation = Literal["merge", "split", "discard"]
CrosswalkReview = Literal["pending", "approved", "rejected"]

#: 处理语后缀：丝/片/末/蓉/丁/花/碎/块/粒/段/条/泥 是切配形态，不构成新身份（INV-020）。
PROCESSING_SUFFIXES = ("丝", "片", "末", "蓉", "丁", "花", "碎", "块", "粒", "段", "条", "泥")

#: 固定源中实际出现的前置形态/状态/预处理词。只有剥离后存在真实基底身份时才生成候选。
PROCESSING_PREFIXES = (
    "半成品",
    "罐装",
    "融化的",
    "洗好的",
    "洗净的",
    "新鲜生",
    "蒸熟",
    "煮熟",
    "打发",
    "去皮",
    "泡发",
    "切丁",
    "切块",
    "切片",
    "法切",
    "装饰",
    "新鲜",
    "冷冻",
    "熟的",
    "熟",
    "干",
)

# 后缀字形与切配形态相同、但整词本身是独立食材的固定源词条。
WHOLE_INGREDIENT_NAMES = {"油条", "鸡米花", "薯条", "甘薯薯条"}


class CrosswalkDecision(BaseModel):
    """单个 merge/split/discard 决策。pending 必须人工 approve/reject 后才落定。

    reviewer/reviewed_at 为人工签名（契约 §5 要求 crosswalk 携带审核者）；
    form 记录形态（姜丝→姜，form=丝）；rejected 保留独立身份并清除 pending。
    """

    model_config = ConfigDict(extra="forbid")

    source_key: str
    operation: CrosswalkOperation
    target_ingredient_ids: tuple[int, ...]
    reason_code: str
    review_status: CrosswalkReview = "pending"
    reviewer: str | None = None
    reviewed_at: str | None = None
    form: str | None = None
    occurrence_count: int = 0
    sample_recipe_ids: tuple[int, ...] = ()
    sample_fragments: tuple[str, ...] = ()


def processing_variant(
    name: str,
    known_names: set[str] | None = None,
) -> tuple[str, str] | None:
    """返回 ``(基底名, 形态)``；有已知名集合时选择可到达的最深真实基底。

    前缀先按源文本顺序剥离，再逐层剥离后缀。例如
    ``罐装去皮番茄 -> 番茄, 罐装+去皮``、
    ``熟花生碎 -> 花生, 熟+碎``。不在固定注册候选中的中间字符串不能成为目标。
    """
    if name in WHOLE_INGREDIENT_NAMES:
        return None

    # 前后缀可能同时存在（干辣椒段）；遍历两种剥离顺序，避免先剥前缀后
    # 错过仍然有效的中间基底“干辣椒”。
    queue: list[tuple[str, tuple[str, ...], tuple[str, ...], int]] = [(name, (), (), 0)]
    seen = {name}
    variants: list[tuple[str, tuple[str, ...], int]] = []
    while queue:
        current, prefix_forms, suffix_forms, depth = queue.pop(0)
        for prefix in PROCESSING_PREFIXES:
            if current.startswith(prefix) and len(current) > len(prefix):
                stripped = current[len(prefix) :]
                if stripped not in seen:
                    seen.add(stripped)
                    next_prefixes = (*prefix_forms, prefix)
                    forms = (*next_prefixes, *reversed(suffix_forms))
                    variants.append((stripped, forms, depth + 1))
                    queue.append((stripped, next_prefixes, suffix_forms, depth + 1))
        if current not in WHOLE_INGREDIENT_NAMES:
            for suffix in PROCESSING_SUFFIXES:
                if current.endswith(suffix) and len(current) > len(suffix):
                    stripped = current[: -len(suffix)]
                    if stripped not in seen:
                        seen.add(stripped)
                        next_suffixes = (*suffix_forms, suffix)
                        forms = (*prefix_forms, *reversed(next_suffixes))
                        variants.append((stripped, forms, depth + 1))
                        queue.append((stripped, prefix_forms, next_suffixes, depth + 1))

    if known_names is None:
        chosen = variants[0] if variants else None
    else:
        matches = [item for item in variants if item[0] in known_names]
        chosen = max(matches, key=lambda item: item[2]) if matches else None
    if chosen is None:
        return None
    base, selected_forms, _ = chosen
    return base, "+".join(selected_forms)


def processing_prefix_variant(name: str) -> tuple[str, str] | None:
    """只剥离连续前置状态词，用于首次出现时建立尚不存在的审核目标身份。"""
    current = name
    forms: list[str] = []
    while True:
        prefix = next(
            (
                item
                for item in PROCESSING_PREFIXES
                if current.startswith(item) and len(current) > len(item)
            ),
            None,
        )
        if prefix is None:
            break
        current = current[len(prefix) :]
        forms.append(prefix)
    if not forms:
        return None
    return current, "+".join(forms)


def processing_base(name: str) -> str | None:
    """兼容旧调用：返回第一层处理语剥离后的基底名。"""
    variant = processing_variant(name)
    return variant[0] if variant else None


def generate_crosswalk_decisions(
    canonical_names: list[str],
    *,
    non_edible: set[str],
    synonyms: dict[str, str],
    form_variants: dict[str, tuple[str, str]],
    occurrence_evidence: dict[str, dict],
) -> list[CrosswalkDecision]:
    """生成 pending 决策：discard 非食用、merge 处理语变体、merge 同义词。

    组合引用在解析层解析到被引用身份，不产生 split 决策；split 操作保留
    供经审核的组合拆解使用。
    """
    name_set = set(canonical_names)
    decisions: list[CrosswalkDecision] = []

    def _evidence(name: str) -> dict:
        return occurrence_evidence.get(name, {})

    # discard：非食用材料不进入注册表。
    for name in canonical_names:
        if name in non_edible:
            decisions.append(
                CrosswalkDecision(
                    source_key=name,
                    operation="discard",
                    target_ingredient_ids=(),
                    reason_code="non_edible",
                    **_evidence(name),
                )
            )

    decision_sources = set(non_edible)

    # merge：处理语变体归一到基底身份（仅当基底也在候选名中）。
    for name in canonical_names:
        if name in non_edible:
            continue
        variant = processing_variant(name, name_set)
        if variant:
            base, form = variant
            decisions.append(
                CrosswalkDecision(
                    source_key=name,
                    operation="merge",
                    target_ingredient_ids=(),
                    reason_code="processing_variant",
                    form=form,
                    **_evidence(name),
                )
            )
            decision_sources.add(name)

    # 固定源中无法由通用前后缀安全表达的状态/形态词（例如温水、鸡蛋液）。
    for source, (target, form) in form_variants.items():
        if (
            source in name_set
            and target in name_set
            and source != target
            and source not in decision_sources
        ):
            decisions.append(
                CrosswalkDecision(
                    source_key=source,
                    operation="merge",
                    target_ingredient_ids=(),
                    reason_code="processing_variant",
                    form=form,
                    **_evidence(source),
                )
            )
            decision_sources.add(source)

    # merge：同义词归一到规范名（仅当目标也在候选名中）。
    for source, target in synonyms.items():
        if (
            source in name_set
            and target in name_set
            and source != target
            and source not in decision_sources
        ):
            decisions.append(
                CrosswalkDecision(
                    source_key=source,
                    operation="merge",
                    target_ingredient_ids=(),
                    reason_code="synonym",
                    **_evidence(source),
                )
            )

    return decisions


def load_approved_decisions(path: Path) -> dict[str, CrosswalkDecision]:
    """读取人工签名 override CSV（含 review_status=approved|rejected）。

    列：source_key,operation,target_ingredient_ids,reason_code,review_status,reviewer,reviewed_at
    批准/拒绝决策必须带 reviewer 与 reviewed_at，否则拒绝（无签名不生效）。
    """
    if not path.exists():
        return {}
    decisions: dict[str, CrosswalkDecision] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            reviewer = (row.get("reviewer") or "").strip()
            reviewed_at = (row.get("reviewed_at") or "").strip()
            status = (row.get("review_status") or "approved").strip().lower()
            if status not in ("approved", "rejected"):
                raise ValueError(
                    f"{row.get('source_key')} 的 review_status 必须是 approved|rejected"
                )
            if not reviewer or not reviewed_at:
                raise ValueError(f"{row.get('source_key')} 的决策缺少 reviewer/reviewed_at 签名")
            targets = tuple(
                int(x) for x in (row.get("target_ingredient_ids") or "").split(";") if x.strip()
            )
            decisions[row["source_key"]] = CrosswalkDecision(
                source_key=row["source_key"],
                operation=row["operation"],  # type: ignore[arg-type]
                target_ingredient_ids=targets,
                reason_code=row.get("reason_code", ""),
                review_status=status,  # type: ignore[arg-type]
                reviewer=reviewer,
                reviewed_at=reviewed_at,
                form=(row.get("form") or "").strip() or None,
            )
    return decisions
