# Final data-quality audit — 2026-08-28

## Answer first

**Verdict: PASS.** The current fixed data and build logic are trustworthy enough for one formal clean rebuild and subsequent owner-controlled MySQL/Qdrant publication. The audit found **0 Critical, 0 High, 0 Medium, and 0 Low findings**.

The decisive evidence is a fresh T04–T08 build in an empty system temporary directory using build ID `00000000-0000-0000-0000-000000000828`, explicit builder version `a37cd35dfce19adaece135c4547c2100ebf78344`, and the project's declared `deepseek-chat` model IDs. All 19 artifacts were created, the manifest and every artifact hash verified, all 19 reported gate records covering G01–G17 passed, nutrition was independently available for all 1,932 eligible recipes, every time graph independently validated and scheduled, and the B1 test suite passed.

The first build attempt omitted the model IDs normally supplied by project environment configuration and therefore missed the time-graph cache at recipe 1. Re-running with the `.env.example` defaults set only in the audit process succeeded. This was an audit-shell configuration error, not a data finding; no project environment or cache file was changed.

## Scope and method

The audit operated only in `feature/recipe-rag-nutrition-time-v2` at HEAD `a37cd35dfce19adaece135c4547c2100ebf78344`. It called `build_fixed_data_staging()` against an empty system temporary directory and supplied the fixed UUID and explicit HEAD, bypassing only the dirty-worktree check. Canonical source manifests, approved review inputs, schemas, build logic, gates, and manifest verification were unchanged.

No repository staging rebuild, publication, storage initialization, Docker/container command, database command, Qdrant command, API command, runtime mutation, commit, or live vector search was performed. `PYTHONDONTWRITEBYTECODE=1` was used. The temporary build was deleted after inspection.

## Dataset and grain

| Dataset / artifact | Rows | Independent result |
|---|---:|---|
| Source recipes | 2,000 | Unique IDs exactly `1..2000` |
| User profiles | 50 | Unique IDs exactly `1..50` |
| Classifications | 2,000 | All approved; no unknown type |
| Eligible dishes | 1,932 | Exact shared consumer universe |
| Ingredient registry | 1,770 | No pending identity and no zero-relation identity |
| Ingredient occurrences | 17,509 | No unresolved edible occurrence |
| Recipe–ingredient relations | 17,493 | No recipe, ingredient, or occurrence orphan |
| Health matrix | 65,588 | Exactly `38 × 1,726`, all approved |
| RAG / nutrition / time outputs | 1,932 each | Identical recipe-ID sets |

Classification distribution was 1,932 `dish`, 29 `preparation`, 27 `meal_bundle`, 10 `cooking_program`, and 2 `test_record`. All primary/composite-key checks returned zero duplicates. Every row in every manifest artifact carried the single expected build ID and source-manifest hash.

## Manifest and G01–G17

The manifest declared exactly 19 required artifacts. All declared row counts and SHA-256 hashes matched, and `verify_build_manifest()` passed.

| Gate | Detail | Result |
|---|---|---|
| G01 | Source rows 2,000; unique IDs 2,000 | PASS |
| G02 | Classifications 2,000; pending 0; unknown types 0 | PASS |
| G03 | User profiles 50; unique IDs 50 | PASS |
| G04 | Registry 1,770; pending 0 | PASS |
| G05 | Occurrences 17,509; unresolved 0; dangling 0 | PASS |
| G06 | Four base consumer views each 1,932 | PASS |
| G07 | Health universe 1,726; matrix 65,588; pending 0 | PASS |
| G08 | Hard decisions 1,084; hard relations 1,084; coverage 38 | PASS |
| G09 step | Expected/actual 1,932/1,932 | PASS |
| G09 nutrition | Expected/actual 1,932/1,932 | PASS |
| G09 RAG | Expected/actual 1,932/1,932 | PASS |
| G10 | Unavailable nutrition without reason: 0 | PASS |
| G11 | RAG health leakage: 0 | PASS |
| G12 | Label/meal errors: 0 | PASS |
| G13 | Runtime-boundary errors: 0 | PASS |
| G14 | Nutrition all-or-nothing errors: 0 | PASS |
| G15 | Built-in baseline 0; actual 1,932 | PASS, independently strengthened below |
| G16 | Invalid, incomplete, cyclic, or unschedulable graphs: 0 | PASS |
| G17 | Cross-consumer boundary errors: 0 | PASS |

## Identity, lineage, and crosswalk closure

The fresh build produced 22 aliases, 381 forms, and 432 crosswalk rows. Pending crosswalk decisions and unresolved edible occurrences were both zero. For every registry ID, `occurrence_count` equaled the final relation count and `appears_in_recipes` equaled the sorted unique relation recipe IDs; provenance mismatches and zero-relation registry IDs were both zero.

Fifteen crosswalk rows retain a direct signed target that is an intermediate approved merge node rather than a final registry row. This is not a dangling reference under the crosswalk graph's semantics: all 15 resolve to final registry IDs. There are seven distinct intermediate IDs. Representative chains are `大蒜末 → 大蒜#150 → 蒜#50`, `生姜片 → 生姜#38 → 姜#6`, `芝士片 → 芝士#429 → 奶酪#1242`, and `马苏里拉芝士碎 → 马苏里拉芝士#15 → 马苏里拉奶酪#811`. Terminal crosswalk orphans: **0**.

This is a coverage note, not a finding: the current hard gate reports direct relation dangling references but does not separately expose crosswalk terminal-closure metrics. The independent audit supplied that check.

## Eligibility, classification, and dependencies

The recipe-ID sets for health, step binding, nutrition input, retrieval, time, nutrition features, and RAG are identical and contain exactly the 1,932 `dish` records. No non-dish leaked into a consumer artifact.

The reviewed dependency file contains 167 unique approved edges: 97 `requires_component`, 58 `uses_program`, and 12 `bundle_contains`. The production loader validated known parent/child IDs, allowed relation types, type-compatible endpoints, uniqueness, and acyclicity. All required test edges were present, the known forbidden `(77, 828, requires_component)` edge was absent, and no portion or substitute component was inferred.

Representative records were inspected:

- elderly + dinner eligibility: recipe 1, `秋梨膏`;
- ≤30-minute single recipe: recipe 1154, `红柚茉莉果茶`, estimated elapsed 41 seconds;
- meal bundle: recipe 175 → 648, `桂花糖烤栗子＆松仁栗子糕同烹` → `桂花糖烤栗子`;
- preparation dependency: recipe 31 → 39, `固元阿胶糕` → `冰糖粉`;
- cooking-program dependency: recipe 36 → 1579, `西兰花饭团` → `停刀烧煮`;
- approved hard relation: recipe 1 contains `冰糖#3`, hard-excluded for `disease_diabetes`.

## Health matrix and safety boundaries

The health ingredient universe is exactly 1,726 IDs. The matrix has exactly 65,588 unique keys, all approved, with 1,084 `hard_exclude` and 64,504 `no_hard_relation` decisions. Staged hard relations exactly equal hard decisions, and all 38 coverage rows are complete.

The current deterministic candidate generator disagreed with approved decisions on **0** cells. Alcohol decisions changed on **0** cells relative to the pre-conservative-policy snapshot. Ten named preservation checks covering ginkgo, egg-tart, soy, and crab boundaries all passed. RAG documents contain no health conclusion or health runtime field.

## Meal labels and RAG boundary

Retrieval and RAG each contain 1,932 rows with identical IDs. Source labels supplied meal tags for 1,732 recipes; reviewed enrichment filled the 200 recipes with completely missing source meal tags. Meal-tag errors, label-tag mismatches, empty meal tags, empty searchable text, empty ingredient-name lists, and forbidden RAG fields were all zero.

The required `meal` searchable projection is complete. Optional `taste` is empty for 502 rows (25.98%); this does not violate the closed requirements and is not used as a pass criterion.

## Nutrition readiness

The weak built-in baseline of zero was not used as the readiness decision. The audit independently profiled all 1,932 rows:

- available: **1,932 (100%)**;
- unavailable: **0 (0%)**;
- invalid positive-weight/vector rows: **0**;
- incomplete unavailable rows: **0**;
- negative/impossible weight or energy values: **0**.

| Measure | Min | Median | P95 | Max |
|---|---:|---:|---:|---:|
| Raw edible input weight, g | 74 | 568 | 2,190 | 7,675 |
| Raw total energy, kcal | 14.40 | 877.595 | 2,939.6545 | 14,671.76 |
| Raw energy per 100 g, kcal | 1.87 | 146.49 | 395.49 | 627.00 |

The high totals are whole-recipe raw-input totals, not serving or per-person values; the corresponding weight distribution includes bulk recipes. No obvious impossible value was found. The build used 16,820 nutrition input occurrences: 16,347 have explicit quantity status and 473 carry `unknown` at the projection level. The reviewed inputs include 4,220 quantity decisions (4,218 approved, 2 modified), 16,820 retention decisions (16,812 approved, 8 modified), 16,636 approved edible-fraction decisions, and 1,786 nutrition crosswalk decisions (994 approved, 792 modified). After decisions, retention-review pending count is zero. No cooking yield, cooking nutrient retention, serving, or per-person rule was applied.

## Time and step readiness

All 1,932 profiles have a nonempty task graph. The independent atom/task coverage validator and deterministic scheduler accepted every graph; summary mismatches were zero. All durations and recipe summaries are single nonnegative values, never ranges. The 1,854 zero-duration tasks are validated `non_task` atoms; zero-elapsed and zero-active recipes are both zero.

| Measure | Min | Median | P95 | Max |
|---|---:|---:|---:|---:|
| Tasks per recipe | 1 | 10 | 20 | 42 |
| Task duration, seconds | 0 | 60 | 1,800 | 86,400 |
| Estimated elapsed, seconds | 41 | 2,495 | 16,262.5 | 174,995 |
| Active time, seconds | 20 | 1,290 | 4,500 | 13,080 |

The longest samples are `冰糖炖官燕` (174,995 s), `冰花炖官燕` (174,990 s), `皂角银耳羹` (92,040 s), `鸡仔饼` (90,540 s), and `烤火鸡` (90,270 s). These graphs contain long wait/rest tasks but remain within validator limits and are schedulable; no impossible outlier was established. There are 49 approved time decisions: 43 set durations and 6 ignore non-task atoms, with zero pending. Runtime time output contains no confidence, source, evidence, nutrition, label, meal, or health fields.

## Tests and prohibited-surface proof

| Command | Exit | Summary |
|---|---:|---|
| `uv run pytest tests/b1 -q` | 0 | 699 passed, 3 skipped in 29.08 s |
| `uv run pytest tests/b1/test_recipe_dependencies.py -q` | 0 | 8 passed in 8.49 s |
| `uv run ruff check …/audit_runner.py` | 0 | All checks passed |

Stable digests prove the formal data surfaces did not change:

| Tree | Pre SHA-256 | Post SHA-256 | Result |
|---|---|---|---|
| `.staging` | `0e3d08ce73a2daa38d7c81552b7212db1636d6be1e5e3d8f9ae6acbdcd5881c3` | same | unchanged |
| `data/review` | `73a3b028878129ba2ec1029353b636c59f1ba566712a328b7e4463dca7f3d7fe` | same | unchanged |

No Docker, container, database, Qdrant, API, readiness, initialization, publication, or live vector-search command was invoked.

## Findings, limitations, and exact next step

There are no Critical, High, Medium, or Low findings. The three non-finding notes are: crosswalk closure is independently verified but not separately surfaced by the current hard gate; optional taste projections are empty for 502 recipes; and this audit intentionally did not run live vector search or online model evaluation.

**Exact next step:** the project owner may authorize one formal clean rebuild at reviewed HEAD with the project's model IDs explicitly configured, verify the resulting manifest again, and only then proceed through the separate publication/initialization workflow. No publication action was authorized or performed by this audit.

Machine-readable evidence: `.superpowers/sdd/2026-08-28-final-data-quality-audit/audit-evidence.json`.

## 2026-08-30 addendum — superseding publication conclusion

This addendum supersedes the 2026-08-28 report's publication-ready wording.
The Task 4 closure re-verified the repaired Task 1–3 surface with a fresh
temporary production build, but it did so from a still-dirty overlay worktree
using the current `HEAD` only as an explicit audit-only `builder_version`
override. That evidence is sufficient for final review of the repair itself,
not for clean-provenance publication or initialization.

### Repaired in code

- G12 now derives expected `label_tags` and `meal_tags` from source
  `labels_raw` plus review-authorized profile enrichment instead of letting retrieval
  and RAG self-confirm each other.
- G18 now requires the published `recipe_dependencies` artifact and exact
  approved/modified dependency closure.
- readiness now trusts manifest-bound `artifact_counts` instead of a static
  handwritten dataset-count dictionary, and strictly rejects malformed or
  duplicate actual MySQL counts before Qdrant is probed.

### Verified by Task 4

- A superseding temp-only production `build_fixed_data_staging()` run passed
  after all final-review fixes with fixed UUID
  `8a8598aa-e21e-4e17-b984-51e0beaf4650`, explicit
  `LLM_MODEL_REASONING=deepseek-chat`, explicit
  `LLM_MODEL_ANSWER=deepseek-chat`, and audit-only
  `builder_version=a37cd35dfce19adaece135c4547c2100ebf78344`.
- Production `data-verify` passed on the emitted manifest; its SHA-256 is
  `0944cbf478f158f4879d60bec167edd5f2675c02df6d9f3df4e575f2e0075e4c`.
- The emitted build contained all 20 artifacts, and the quality report passed
  all extracted gates `G01` through `G18`.
- Independent dependency extraction confirmed `167` rows,
  `167` unique four-tuples, `12` `bundle_contains`, approved-only statuses,
  correct build/source identity on every row, and `18` non-dish dependency
  rows derivable from emitted classifications. Focused mutation tests also
  prove that loader-authorized `modified` relationships pass exact closure and
  that deleting or altering one fails G18.
- The final allowed regression suite passed:
  `809 passed, 3 skipped, 1 pre-existing warning`; Ruff was clean; scoped
  `git diff --check` exited `0` with only LF→CRLF notices.
- Repository `.staging` remained unchanged at 75 files: pre/post digest
  `1b9016bd9ca473cecc7d5ad518350fd8bde21a1306c6440bc5edfdcc26d205ac`.
  The pre timestamp preceded the build, and the exact canonical digest
  serialization is recorded in `final-verification.md`.
- The missing Task 3 Fix 3 before-file snapshot was disclosed and not
  fabricated. Per owner direction it is a non-blocking attribution note;
  completion is judged from the current implementation and current-state
  verification above.

### Still deferred and not treated as defects in this phase

- a formal clean reviewed commit and clean-provenance rebuild
- owner-controlled MySQL/Qdrant initialization
- publication
- any new library/bootstrap operation
- RAG strategy optimization

### Current GO / NO-GO

- GO: the code/data-gate repair is ready for final review.
- NO-GO: publication and initialization remain blocked until a clean reviewed
  provenance build is produced from a clean state.
