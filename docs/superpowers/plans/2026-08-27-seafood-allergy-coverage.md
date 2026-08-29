# Seafood Allergy Coverage Correction Plan

> Scope: only correct the approved `allergy_seafood` ingredient coverage. Do not change query rewriting, time semantics, clarification policy, bundle/dependency output, nutrition, or runtime orchestration.

**Goal:** Ensure every clearly aquatic/seafood ingredient currently used by eligible recipes is hard-excluded for `allergy_seafood`, while protecting known name-only false positives.

**Design:** Keep `data/review/health_relation_decisions.csv` as the authoritative closed matrix. Improve the conservative candidate matcher for future rebuilds, update only the confirmed current matrix cells, and run a full registry-versus-matrix audit. Do not infer seafood from broad category/family fields because those fields contain false-positive families such as 川贝、杏鲍菇、蟹味菇 and 贝贝南瓜.

**Implementation constraints:** The working tree and the matrix already contain earlier user work. Make targeted edits only; never regenerate or rewrite the whole matrix; do not commit unrelated changes.

---

## Task 1: Correct semantics and approved matrix cells with TDD

**Files:**

- Modify: `tests/b1/test_health_relation_matrix.py`
- Modify: `src/food_agent_v2/b1/health_relation_builder.py`
- Modify targeted rows only: `data/review/health_relation_decisions.csv`
- Modify if needed for the audit: `scripts/audit_health_relations.py`
- Add or modify a focused audit test under `tests/b1/`

### Step 1: Write failing semantic tests

Add parameterized coverage proving `allergy_seafood` suggests `hard_exclude` for:

- 小青龙、澳洲带子、带子肉、六头鲍、南日鲍、牡蛎、瑶柱、80头干瑶柱、河鳗、泥鳅、白鳝、银鲳
- Future/non-current variants where useful: 大头虾、皮皮虾、银鱼柳、鳗鱼

Add negative coverage proving it remains `no_hard_relation` for:

- 川贝、川贝粉、杏鲍菇、蟹味菇、鲜蟹味菇、贝贝南瓜、海鲜菇、海鲜酱、海鲜酱油、蒸鱼豉油、素蚝油

Run the focused tests and record the expected RED failures for the missing positives.

### Step 2: Implement the smallest conservative matcher correction

Extend `allergy_seafood` with explicit aquatic aliases/markers and narrow guards. Do not use ingredient family/category as an unconditional rule and do not make the literal word `海鲜` sufficient when it denotes a sauce or mushroom.

Run the focused semantic tests and reach GREEN.

### Step 3: Correct only confirmed current approved decisions

In `data/review/health_relation_decisions.csv`, change only existing `allergy_seafood` cells for confirmed current eligible ingredients from `no_hard_relation` to `hard_exclude`. Preserve the row count, key set, ordering, encoding, all unrelated rows, and the existing review signature fields. Use a concise internal semantic-classification evidence string; do not add confidence fields or external-source requirements.

At minimum, verify the current eligible IDs identified in the incident review: `69, 291, 423, 441, 856, 1053, 1236, 1414, 1531, 1634, 1707, 2131`.

### Step 4: Run a full closed-set audit

Audit all current eligible ingredient names against the complete `allergy_seafood` decision slice. The audit must:

- report zero confirmed false negatives after correction;
- report zero known false-positive guards promoted to `hard_exclude`;
- verify exactly one matrix decision per eligible ingredient and no key-count change;
- remain read-only against MySQL and work with the standard local port `3306` or an explicit CLI argument/environment value.

If the lexical audit returns ambiguous names, report them separately instead of auto-promoting them.

### Step 5: Run focused and regression tests

Run at least:

```powershell
uv run pytest tests/b1/test_health_relation_matrix.py -q
uv run pytest tests/b1 -q
```

Then run the closed-set audit and preserve its output in the implementation report. Do not rebuild or publish until review accepts the source and data change.

### Step 6: Report for review

Report changed files, RED/GREEN evidence, the exact corrected IDs/names, audit counts, test results, and any residual ambiguity. Do not claim deployment; root review decides the subsequent rebuild/publish step within this approved item.
