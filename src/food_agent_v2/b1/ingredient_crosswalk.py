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


def processing_base(name: str) -> str | None:
    """若名称以处理语后缀结尾且去掉后缀后仍有基底字符，返回基底名，否则 None。"""
    for suffix in PROCESSING_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 1:
            return name[: -len(suffix)]
    return None


def generate_crosswalk_decisions(
    canonical_names: list[str],
    *,
    non_edible: set[str],
    synonyms: dict[str, str],
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

    # merge：处理语变体归一到基底身份（仅当基底也在候选名中）。
    for name in canonical_names:
        base = processing_base(name)
        if base and base in name_set and base != name:
            decisions.append(
                CrosswalkDecision(
                    source_key=name,
                    operation="merge",
                    target_ingredient_ids=(),
                    reason_code="processing_variant",
                    form=name[len(base):],
                    **_evidence(name),
                )
            )

    # merge：同义词归一到规范名（仅当目标也在候选名中）。
    for source, target in synonyms.items():
        if source in name_set and target in name_set and source != target:
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
                raise ValueError(f"{row.get('source_key')} 的 review_status 必须是 approved|rejected")
            if not reviewer or not reviewed_at:
                raise ValueError(f"{row.get('source_key')} 的决策缺少 reviewer/reviewed_at 签名")
            targets = tuple(int(x) for x in (row.get("target_ingredient_ids") or "").split(";") if x.strip())
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
