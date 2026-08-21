import csv
import io
import zipfile
from decimal import Decimal

from food_agent_v2.b1.usda_nutrition import load_usda_nutrition_archive


def _csv_text(fieldnames: tuple[str, ...], rows: tuple[dict[str, str], ...]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _write_archive(
    path,
    *,
    reported_energy: str | None = None,
    carbohydrate: str = "62.36",
) -> None:
    foods = _csv_text(
        ("fdc_id", "data_type", "description", "food_category_id", "publication_date"),
        (
            {
                "fdc_id": "100",
                "data_type": "foundation_food",
                "description": "Beans, black, raw",
                "food_category_id": "1",
                "publication_date": "2026-04-30",
            },
            {
                "fdc_id": "999",
                "data_type": "sample_food",
                "description": "Internal sample",
                "food_category_id": "1",
                "publication_date": "2026-04-30",
            },
        ),
    )
    nutrients = _csv_text(
        ("id", "name", "unit_name", "nutrient_nbr", "rank"),
        (
            {"id": "1008", "name": "Energy", "unit_name": "KCAL", "nutrient_nbr": "208", "rank": "300"},
            {"id": "2048", "name": "Energy (Atwater Specific Factors)", "unit_name": "KCAL", "nutrient_nbr": "958", "rank": "290"},
            {"id": "1003", "name": "Protein", "unit_name": "G", "nutrient_nbr": "203", "rank": "600"},
            {"id": "1004", "name": "Total lipid (fat)", "unit_name": "G", "nutrient_nbr": "204", "rank": "800"},
            {"id": "1005", "name": "Carbohydrate, by difference", "unit_name": "G", "nutrient_nbr": "205", "rank": "1110"},
            {"id": "1079", "name": "Fiber, total dietary", "unit_name": "G", "nutrient_nbr": "291", "rank": "1200"},
            {"id": "1087", "name": "Calcium, Ca", "unit_name": "MG", "nutrient_nbr": "301", "rank": "5300"},
            {"id": "1089", "name": "Iron, Fe", "unit_name": "MG", "nutrient_nbr": "303", "rank": "5400"},
            {"id": "1093", "name": "Sodium, Na", "unit_name": "MG", "nutrient_nbr": "307", "rank": "5800"},
            {"id": "1253", "name": "Cholesterol", "unit_name": "MG", "nutrient_nbr": "601", "rank": "15700"},
        ),
    )
    food_nutrients = _csv_text(
        ("id", "fdc_id", "nutrient_id", "amount", "data_points", "derivation_id", "min", "max", "median", "footnote", "min_year_acquired"),
        tuple(
            {
                "id": str(index),
                "fdc_id": "100",
                "nutrient_id": nutrient_id,
                "amount": amount,
                "data_points": "1",
                "derivation_id": "1",
                "min": "",
                "max": "",
                "median": "",
                "footnote": "",
                "min_year_acquired": "",
            }
            for index, (nutrient_id, amount) in enumerate(
                (
                    ("2048", "341"),
                    ("1003", "21.60"),
                    ("1004", "1.42"),
                    ("1005", carbohydrate),
                    ("1079", "15.5"),
                    ("1087", "123"),
                    ("1089", "5.02"),
                    ("1093", "5"),
                ) + (("1008", reported_energy),) if reported_energy is not None else (
                    ("2048", "341"),
                    ("1003", "21.60"),
                    ("1004", "1.42"),
                    ("1005", carbohydrate),
                    ("1079", "15.5"),
                    ("1087", "123"),
                    ("1089", "5.02"),
                    ("1093", "5"),
                ),
                1,
            )
        ),
    )
    with zipfile.ZipFile(path, "w") as archive:
        prefix = "fixture/"
        archive.writestr(prefix + "food.csv", foods)
        archive.writestr(prefix + "nutrient.csv", nutrients)
        archive.writestr(prefix + "food_nutrient.csv", food_nutrients)


def test_usda_archive_import_is_mechanical_and_keeps_missing_values(tmp_path) -> None:
    archive_path = tmp_path / "foundation.zip"
    _write_archive(archive_path)

    references = load_usda_nutrition_archive(
        archive_path,
        source_dataset="usda_foundation",
        release="2026-04",
    )

    assert len(references) == 1
    reference = references[0]
    assert reference.reference_id == "usda-fdc-100"
    assert reference.canonical_name == "Beans, black, raw"
    assert reference.form == "raw"
    assert reference.per_100g.energy_kcal == Decimal("341")
    assert reference.per_100g.protein_g == Decimal("21.60")
    assert reference.per_100g.cholesterol_mg is None
    assert reference.food_origin == "unknown"
    assert reference.source_dataset == "usda_foundation"
    assert reference.source_url.endswith("/100/nutrients")


def test_usda_import_prefers_reported_energy_over_atwater_fallback(tmp_path) -> None:
    archive_path = tmp_path / "foundation.zip"
    _write_archive(archive_path, reported_energy="339")

    references = load_usda_nutrition_archive(
        archive_path,
        source_dataset="usda_foundation",
        release="2026-04",
    )

    assert references[0].per_100g.energy_kcal == Decimal("339")


def test_usda_import_keeps_negative_analytical_noise_missing_and_auditable(
    tmp_path,
) -> None:
    archive_path = tmp_path / "foundation.zip"
    _write_archive(archive_path, carbohydrate="-0.25")
    audit = []

    references = load_usda_nutrition_archive(
        archive_path,
        source_dataset="usda_foundation",
        release="2026-04",
        audit=audit,
    )

    assert references[0].per_100g.carbohydrate_g is None
    assert audit == [
        {
            "issue_code": "NEGATIVE_NUTRIENT_TREATED_AS_MISSING",
            "fdc_id": "100",
            "nutrient_id": "1005",
            "raw_amount": "-0.25",
        }
    ]
