"""项目路径常量。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REFERENCE_DIR = DATA_DIR / "reference"
CLEANED_DIR = DATA_DIR / "cleaned"

REPORTS_DIR = PROJECT_ROOT / "reports"
PIPELINE_REPORTS_DIR = REPORTS_DIR / "data_pipeline"

# Raw input files
RECIPES_RAW = RAW_DIR / "recipes_sample_2000.csv"
USERS_RAW = RAW_DIR / "50个用户健康档案_详细版7.13.json"
DIALOGUE_CASES = RAW_DIR / "对话用例.json"

# Reference files
FOOD_COMPOSITION = REFERENCE_DIR / "china_food_composition.jsonl"
FOOD_COMPOSITION_MANIFEST = REFERENCE_DIR / "china_food_composition_manifest.json"

# Cleaned output files
CLEANED_RECIPES = CLEANED_DIR / "clean_recipes.jsonl"
CLEANED_USERS = CLEANED_DIR / "user_profiles.jsonl"

# Quality report
QUALITY_REPORT = PIPELINE_REPORTS_DIR / "quality_report.json"
PIPELINE_RUN_LOG = PIPELINE_REPORTS_DIR / "pipeline_run.json"
