import importlib.util
import json
from pathlib import Path

import pytest

_AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_health_relations",
    Path(__file__).parents[2] / "scripts" / "audit_health_relations.py",
)
assert _AUDIT_SPEC is not None
assert _AUDIT_SPEC.loader is not None
_AUDIT_MODULE = importlib.util.module_from_spec(_AUDIT_SPEC)
_AUDIT_SPEC.loader.exec_module(_AUDIT_MODULE)
audit = _AUDIT_MODULE.audit


class _FakeCursor:
    def __init__(self, ready_rows: list[tuple[str]], records: dict[str, list[dict]]) -> None:
        self.ready_rows = ready_rows
        self.records = records
        self.executed: list[tuple[str, tuple | None]] = []
        self._rows: list[tuple[str]] = []

    def execute(self, query: str, params: tuple | None = None) -> None:
        self.executed.append((query, params))
        if "FROM data_builds" in query:
            self._rows = self.ready_rows
        else:
            if params is None:
                build_id = None
                artifact_name = "ingredient_registry"
            else:
                build_id, artifact_name = params
            self._rows = [
                (json.dumps(record, ensure_ascii=False),)
                for record in self.records.get(artifact_name, [])
                if build_id is None or record["build_id"] == build_id
            ]

    def fetchall(self) -> list[tuple[str]]:
        return self._rows

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self.cursor_value = cursor
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_value

    def close(self) -> None:
        self.closed = True


def _write_decisions(path: Path) -> None:
    path.write_text(
        "constraint_code,ingredient_id,decision,evidence,review_status,reviewer,reviewed_at\n"
        "allergy_seafood,1,hard_exclude,review:direct,approved,project_owner,2026-08-10\n",
        encoding="utf-8",
    )


def _ready_records(build_id: str = "build-ready") -> dict[str, list[dict]]:
    return {
        "ingredient_registry": [
            {"build_id": build_id, "ingredient_id": 1, "name_canonical": "牡蛎"},
            {"build_id": build_id, "ingredient_id": 2, "name_canonical": "小青龙"},
        ],
        "recipe_health_views": [
            {"build_id": build_id, "recipe_id": 1, "ingredient_ids": [1, 2]},
        ],
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("registry_records", "health_view_records", "error_code"),
    (
        (
            [
                {"build_id": "build-a", "ingredient_id": 1, "name_canonical": "牡蛎"},
                {"build_id": "build-b", "ingredient_id": 2, "name_canonical": "小青龙"},
            ],
            [{"build_id": "build-a", "recipe_id": 1, "ingredient_ids": [1]}],
            "BUILD_IDENTITY_MISMATCH",
        ),
        (
            [{"build_id": "build-a", "ingredient_id": 1, "name_canonical": "牡蛎"}],
            [{"build_id": "build-b", "recipe_id": 1, "ingredient_ids": [1]}],
            "BUILD_IDENTITY_MISMATCH",
        ),
        (
            [
                {"build_id": "build-a", "ingredient_id": 1, "name_canonical": "牡蛎"},
                {"build_id": "build-a", "ingredient_id": 1, "name_canonical": "河蚝"},
            ],
            [{"build_id": "build-a", "recipe_id": 1, "ingredient_ids": [1]}],
            "DUPLICATE_INGREDIENT_ID",
        ),
        (
            [{"build_id": "build-a", "ingredient_id": 1, "name_canonical": "牡蛎"}],
            [
                {"build_id": "build-a", "recipe_id": 1, "ingredient_ids": [1]},
                {"build_id": "build-a", "recipe_id": 1, "ingredient_ids": [1]},
            ],
            "DUPLICATE_RECIPE_HEALTH_VIEW",
        ),
    ),
)
def test_local_snapshot_audit_rejects_untrusted_snapshot_pairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registry_records: list[dict],
    health_view_records: list[dict],
    error_code: str,
) -> None:
    """Cross-build or duplicate local records must not be silently normalized."""
    decisions_path = tmp_path / "decisions.csv"
    registry_path = tmp_path / "ingredient_registry.jsonl"
    health_views_path = tmp_path / "recipe_health_views.jsonl"
    report_path = tmp_path / "report.txt"
    _write_decisions(decisions_path)
    _write_jsonl(registry_path, registry_records)
    _write_jsonl(health_views_path, health_view_records)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_health_relations.py",
            "--decisions",
            str(decisions_path),
            "--registry",
            str(registry_path),
            "--health-views",
            str(health_views_path),
            "--out",
            str(report_path),
        ],
    )

    assert _AUDIT_MODULE.main() == 2
    assert error_code in report_path.read_text(encoding="utf-8")


def test_mysql_audit_fails_closed_when_ready_build_is_not_unique(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mixing historical rows must not replace a unique ready-build snapshot."""
    decisions_path = tmp_path / "decisions.csv"
    report_path = tmp_path / "report.txt"
    _write_decisions(decisions_path)
    cursor = _FakeCursor([("build-old",), ("build-ready",)], _ready_records())
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(_AUDIT_MODULE.pymysql, "connect", lambda **_: connection)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_health_relations.py",
            "--decisions",
            str(decisions_path),
            "--out",
            str(report_path),
        ],
    )

    assert _AUDIT_MODULE.main() == 2
    assert "BUILD_IDENTITY_UNAVAILABLE" in report_path.read_text(encoding="utf-8")


def test_mysql_audit_uses_health_views_to_detect_missing_matrix_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An eligible health-view ingredient absent from the matrix must fail the closed set."""
    decisions_path = tmp_path / "decisions.csv"
    report_path = tmp_path / "report.txt"
    _write_decisions(decisions_path)
    cursor = _FakeCursor([("build-ready",)], _ready_records())
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(_AUDIT_MODULE.pymysql, "connect", lambda **_: connection)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_health_relations.py",
            "--decisions",
            str(decisions_path),
            "--out",
            str(report_path),
        ],
    )

    assert _AUDIT_MODULE.main() == 1
    output = report_path.read_text(encoding="utf-8")
    assert "missing_matrix_keys=1" in output
    assert "  2" in output
    assert cursor.executed[0][1] is None
    assert cursor.executed[1][1] == ("build-ready", "ingredient_registry")
    assert cursor.executed[2][1] == ("build-ready", "recipe_health_views")


def test_seafood_audit_separates_confirmed_hits_guards_and_ambiguous_names() -> None:
    """A missing alias, promoted guard, and vague seafood name must remain distinguishable."""
    registry = {
        1: "小青龙",
        2: "牡蛎",
        3: "川贝粉",
        4: "蟹味菇",
        5: "海鲜酱",
        6: "海鲜汤",
        7: "蒸鱼鼓油",
    }
    decisions = {
        ("allergy_seafood", 1): "no_hard_relation",
        ("allergy_seafood", 2): "hard_exclude",
        ("allergy_seafood", 3): "no_hard_relation",
        ("allergy_seafood", 4): "hard_exclude",
        ("allergy_seafood", 5): "no_hard_relation",
        ("allergy_seafood", 6): "no_hard_relation",
        ("allergy_seafood", 7): "no_hard_relation",
    }

    assert audit(registry, decisions) == {
        "confirmed_false_negatives": [(1, "小青龙")],
        "known_false_positive_promotions": [(4, "蟹味菇")],
        "ambiguous_names": [(6, "海鲜汤")],
    }
