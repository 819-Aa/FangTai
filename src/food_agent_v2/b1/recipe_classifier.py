"""确定性菜品分类器（T05）。

把 2,000 条固定源记录映射到封闭枚举 record_type。分类器只产生候选与
rule evidence：强标记直接判定（approved），弱/冲突标记进入 pending，
只从人工 override 读取最终决定。发布门禁要求 2,000 个唯一分类且
pending=0。不依赖旧 V1 分布，旧分布只用于差异对照。

弱信号必须 pending 而不是静默通过——例如"蒸肠粉/凉粉/米粉"以"粉"结尾却是
菜品，"配鱼子酱"以"酱"结尾却是菜品，"白露|菌香肉酱蒸百合"的"|"是装饰。
"""

from __future__ import annotations

import collections
import csv
import json
from pathlib import Path

from food_agent_v2.b1.schemas import (
    ClassificationOverride,
    ClassificationReview,
    RecipeClassification,
    RecordType,
    SourceRecipeRow,
)

#: 封闭发布类型（不含 UNKNOWN 哨兵）。
PUBLISHABLE_TYPES = {
    RecordType.DISH,
    RecordType.PREPARATION,
    RecordType.MEAL_BUNDLE,
    RecordType.COOKING_PROGRAM,
    RecordType.TEST_RECORD,
}

#: 强标记——确定性、几乎不可能误判。
_TEST_MARKERS = ("测试",)
_STRONG_MEAL_MARKERS = ("套餐", "合辑", "同烹", "同出", "拼盘", "大杂烩")
_STRONG_PROGRAM_MARKERS = (
    "打发", "乳化", "清洗", "蒸米饭", "煮饭",
    "颠勺", "焯水", "醒面", "刀工", "腌制", "烘焙", "晾凉",
)
#: 强备料：明确是酱料/面团/预制食材的整名。
_STRONG_PREP_EXACT = {
    "蒜泥", "蒜油", "豆沙馅", "虾干", "柠檬干", "小橘干",
    "红糖姜片", "红糖姜片干", "蔬菜碎", "绞肉", "蛋白糖", "蛋白糖霜",
    "红枣泥", "黑芝麻丸", "奶酪棒", "养生枣片", "皮冻", "黄桃罐头",
    "派皮", "卡仕达酱", "烧烤酱", "韩式拌饭酱", "叉烧肉酱", "蓝莓酱",
    "照烧汁", "万能凉拌汁", "花生酱", "韩式辣椒酱", "香菇猪肉酱",
    "香辣牛肉酱", "凯撒酱", "菌菇牛肉酱", "傣味番茄酱", "八宝辣酱",
    "炸酱面酱", "草莓酱", "千岛沙拉酱", "醪糟",
}
_STRONG_PREP_CONTAINS = ("面团",)

#: 弱标记——可能命中菜品（如"粉"结尾的肠粉/凉粉、低温慢煮卤鲍鱼），
#: 必须 pending 人工复核，不能静默通过。
_WEAK_PREP_SUFFIX = ("酱", "汁", "膏", "糊", "粉", "泥")
_WEAK_MEAL_MARKERS = ("|", "＆", "&")
_WEAK_PROGRAM_MARKERS = ("慢煮", "快煮", "减脂", "发酵", "低温", "速冻")
#: 命中这些词的弱备料后缀其实指向菜品（蒸肠粉/客家蒸米粉/配鱼子酱）。
_DISH_VERBS = ("配", "蒸", "烤", "炒", "烧", "炖", "煮", "烩", "煎", "炸")


def _record_type(enum_name: str) -> RecordType:
    return RecordType[enum_name]


def _classify_by_markers(name: str) -> tuple[RecordType | None, list[str], str]:
    """返回 (候选类型, 证据, 置信度)。None 表示需要人工 pending。"""
    evidence: list[str] = []

    if any(marker in name for marker in _TEST_MARKERS):
        return _record_type("TEST_RECORD"), ["名称含'测试'"], "high"

    if any(marker in name for marker in _STRONG_MEAL_MARKERS):
        return _record_type("MEAL_BUNDLE"), ["名称含合辑/套餐/同烹标记"], "high"

    if name in _STRONG_PREP_EXACT or any(marker in name for marker in _STRONG_PREP_CONTAINS):
        if name in _STRONG_PREP_EXACT:
            evidence.append("整名命中强备料清单")
        else:
            evidence.append("名称含'面团'")
        return _record_type("PREPARATION"), evidence, "high"

    if any(marker in name for marker in _STRONG_PROGRAM_MARKERS):
        return _record_type("COOKING_PROGRAM"), ["名称含技法/程序标记"], "high"

    # 弱信号：可能命中菜品，必须人工复核。
    weak_hits: list[str] = []
    if name.endswith(_WEAK_PREP_SUFFIX) and not any(m in name for m in _DISH_VERBS):
        weak_hits.append(f"以{name[-1]}结尾")
    if any(marker in name for marker in _WEAK_MEAL_MARKERS):
        weak_hits.append("含合辑/分隔标记")
    if any(marker in name for marker in _WEAK_PROGRAM_MARKERS):
        weak_hits.append("含程序/状态标记")
    if weak_hits:
        evidence.extend(weak_hits)
        return None, evidence, "low"

    return _record_type("DISH"), ["未命中任何非菜标记，默认 dish"], "high"


def classify_recipe(
    row: SourceRecipeRow,
    overrides: dict[int, ClassificationOverride] | None = None,
) -> RecipeClassification:
    """为单行生成分类。override 优先；弱/冲突信号 → pending。"""
    overrides = overrides or {}
    recipe_id = row.recipe_id

    if recipe_id in overrides:
        override = overrides[recipe_id]
        return RecipeClassification(
            recipe_id=recipe_id,
            record_type=override.record_type,
            rule_evidence=[f"人工 override: {override.reason}"],
            confidence="high",
            review_status=ClassificationReview.APPROVED,
        )

    candidate, evidence, confidence = _classify_by_markers(row.name)

    if candidate is None:
        return RecipeClassification(
            recipe_id=recipe_id,
            record_type=RecordType.UNKNOWN,
            rule_evidence=evidence,
            confidence=confidence,
            review_status=ClassificationReview.PENDING,
        )

    return RecipeClassification(
        recipe_id=recipe_id,
        record_type=candidate,
        rule_evidence=evidence,
        confidence=confidence,
        review_status=ClassificationReview.APPROVED,
    )


def classify_all(
    rows: list[SourceRecipeRow],
    overrides: dict[int, ClassificationOverride] | None = None,
) -> list[RecipeClassification]:
    return [classify_recipe(row, overrides) for row in rows]


def load_overrides(path: Path) -> dict[int, ClassificationOverride]:
    """读取人工签名 override CSV：recipe_id,record_type,reason,reviewer,reviewed_at。"""
    if not path.exists():
        return {}
    overrides: dict[int, ClassificationOverride] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            recipe_id = int(row["recipe_id"])
            overrides[recipe_id] = ClassificationOverride(
                recipe_id=recipe_id,
                record_type=RecordType[row["record_type"]],
                reason=row.get("reason", ""),
                reviewer=row.get("reviewer", ""),
                reviewed_at=row.get("reviewed_at", ""),
            )
    return overrides


def pending_recipe_ids(classifications: list[RecipeClassification]) -> list[int]:
    return [c.recipe_id for c in classifications if c.review_status == ClassificationReview.PENDING]


class ClassificationGateError(Exception):
    """发布门禁违约：分类不唯一或存在 pending。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def enforce_classification_gate(classifications: list[RecipeClassification]) -> None:
    """发布门禁：2,000 个唯一分类且 pending=0。违规抛 ClassificationGateError。"""
    unique_ids = {c.recipe_id for c in classifications}
    if len(unique_ids) != len(classifications):
        raise ClassificationGateError(
            "CLASSIFICATION_NOT_UNIQUE",
            f"{len(classifications)} 行但仅 {len(unique_ids)} 个唯一 recipe_id",
        )
    pending = pending_recipe_ids(classifications)
    if pending:
        raise ClassificationGateError(
            "CLASSIFICATION_PENDING",
            f"{len(pending)} 项待人工复核，发布阻断: {pending}",
        )


def write_classification_output(
    rows: list[SourceRecipeRow],
    classifications: list[RecipeClassification],
    staging_dir: Path,
) -> dict:
    """写出分类产物（2,000 行）与差异报告，不初始化数据库。

    pending>0 时报告 status=blocked（不抛错，供 H01 复核清单使用）；
    pending=0 时 status=passed，配合 enforce_classification_gate 可发布。
    """
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)

    classification_path = staging / "recipe_classifications.jsonl"
    with classification_path.open("w", encoding="utf-8") as handle:
        for c in classifications:
            handle.write(
                json.dumps(
                    {
                        "recipe_id": c.recipe_id,
                        "record_type": c.record_type.value,
                        "confidence": c.confidence,
                        "review_status": c.review_status.value,
                        "rule_evidence": c.rule_evidence,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    distribution = collections.Counter(c.record_type.value for c in classifications)
    pending = pending_recipe_ids(classifications)
    report = {
        "row_count": len(rows),
        "classification_count": len(classifications),
        "unique_recipe_ids": len({c.recipe_id for c in classifications}),
        "distribution": dict(distribution),
        "pending_count": len(pending),
        "pending_ids": pending,
        "status": "blocked" if pending else "passed",
    }
    report_path = staging / "classification_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
