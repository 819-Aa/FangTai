# Public Repository Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a clean, portable, current-version presentation of the Fotile health meal Agent to the GitHub repository `819-Aa/code` without changing application behavior.

**Architecture:** Treat publication cleanup as a documentation and Git-boundary change. Keep runtime source, MySQL schema, Qdrant/Redis integrations, tests, and reviewed data intact; remove Docker Compose only from Git tracking, sanitize machine-specific paths, and normalize human-facing document names without renaming imported Python packages.

**Tech Stack:** Git, Markdown, PowerShell, Python/FastAPI project metadata, Vue 3 project metadata

## Global Constraints

- Do not modify business logic, API behavior, database schema, or frontend behavior.
- Do not rename `b1`/`b2`/`c1`-style Python packages or imports.
- Keep `docker-compose.yml` on the local machine but absent from the Git index.
- Do not rerun application, Docker, database, frontend, or live-model tests.
- Validate only Git structure, Markdown links, tracked-path portability, and publication safety.
- Preserve the user's existing README work and the deletion of the abandoned streaming/profile design documents.

---

### Task 1: Align the public README with the active project

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the current `WORKFLOW_MODE=fast_path` implementation and existing H07/data-audit reports.
- Produces: the GitHub landing page and its setup/documentation links.

- [ ] **Step 1: Update the project identity and workflow diagram**

Change the title to `方太个性化健康膳食推荐 Agent`, replace `B4`/`C2` diagram labels with `健康规则引擎`/`菜单规划器`, and remove the sentence describing the old multi-model workflow.

- [ ] **Step 2: Remove Docker Compose from the public contract**

Remove Docker Compose from the technology line, architecture table, project tree, prerequisites, commands, and explanatory text. Retain MySQL 8.0, Qdrant, and Redis as externally supplied service prerequisites.

- [ ] **Step 3: Align static evidence**

Change the non-live backend result to `1528 passed, 3 skipped`, matching `reports/2026-09-01-h07-agent-chain-integration-verification.md`. Do not claim a new test run.

- [ ] **Step 4: Validate README links and diff**

Run the PowerShell relative-link scan used during review and `git diff --check -- README.md`. Expected: 10 links checked, zero broken links, and no whitespace errors.

### Task 2: Normalize public-facing tracked files

**Files:**
- Rename: `reports/FINAL_REVIEW.md` to `reports/final-review.md`
- Rename: `reports/data_review/REVIEW_REQUIRED.md` to `reports/data_review/review-required.md`
- Rename: `execution/tasks/T01.yaml` to `execution/tasks/t01.yaml`
- Move: `DEEPSEEK_START_HERE.md` to `docs/legacy-remediation-handoff.md`
- Modify: tracked Markdown, JSON, and YAML references to the renamed files
- Modify: the 10 tracked Markdown/JSON files containing `C:\Users\...` or `C:/Users/...`

**Interfaces:**
- Consumes: existing historical documentation and execution-envelope references.
- Produces: portable repository-relative references and consistent human-facing filenames.

- [ ] **Step 1: Perform Git-aware renames**

Use `git mv` for each exact source/destination pair above so history remains traceable.

- [ ] **Step 2: Update references**

Update references in `reports/README.md`, `reports/2026-08-09-v2-code-review.md`, `docs/README.md`, `reports/2026-08-09-v2-handoff-baseline.md`, `execution/README.md`, `execution/tasks/t01.yaml`, and the affected plans.

- [ ] **Step 3: Sanitize absolute paths**

Replace repository paths with `<repo-root>` in prose and repository-relative paths in machine-readable fields. Replace the parent competition directory with `<workspace-root>` where the parent itself is the subject.

- [ ] **Step 4: Validate naming and portability**

Run `rg -n "[A-Za-z]:[/\\]Users[/\\]"` over tracked Markdown/JSON files and scan references to the old filenames. Expected: zero matches.

### Task 3: Keep Compose local and exclude it from publication

**Files:**
- Modify: `.gitignore`
- Remove from Git index only: `docker-compose.yml`

**Interfaces:**
- Consumes: the local Compose file currently used for development.
- Produces: a published tree without Compose while retaining the local file.

- [ ] **Step 1: Add the root Compose file to Git ignore rules**

Add `/docker-compose.yml` under local infrastructure/runtime exclusions in `.gitignore`.

- [ ] **Step 2: Remove Compose from the index without deleting the local file**

Run `git rm --cached -- docker-compose.yml`.

- [ ] **Step 3: Verify the publication boundary**

Run `Test-Path docker-compose.yml`, `git ls-files docker-compose.yml`, and `git check-ignore -v docker-compose.yml`. Expected: local file exists, tracked output is empty, and the ignore rule matches.

### Task 4: Review and commit the publication tree

**Files:**
- Modify: only files listed in Tasks 1–3 plus the user's two already-deleted abandoned design documents.

**Interfaces:**
- Consumes: the cleaned worktree.
- Produces: one reviewable publication commit.

- [ ] **Step 1: Run structure-only verification**

Run `git diff --check`, README link validation, tracked secret-filename scan, absolute-path scan, and inspect `git status --short` plus `git diff --stat`. Do not run application tests.

- [ ] **Step 2: Stage the exact publication changes**

Stage README, `.gitignore`, Compose index deletion, document/path normalization, and the two abandoned design-document deletions. Review `git diff --cached --name-status` before committing.

- [ ] **Step 3: Commit**

Create a commit with message `docs: prepare project for public GitHub release`.

### Task 5: Publish to `819-Aa/code` and rename the GitHub repository

**Files:**
- Modify: local Git remote configuration only.
- External: GitHub repository `https://github.com/819-Aa/code`.

**Interfaces:**
- Consumes: the verified local `main` branch and the target repository's one-file initial commit.
- Produces: a pushed GitHub `main` branch and a repository URL using the owner-approved final name.

- [ ] **Step 1: Configure and fetch the target remote**

Add `origin` as `https://github.com/819-Aa/code.git`, fetch `origin/main`, and verify it still contains only the initial README commit `a7a1e11`.

- [ ] **Step 2: Preserve the existing remote commit**

Merge `origin/main` with `--allow-unrelated-histories` using an `ours` merge so the destination's initial commit remains in history without replacing the cleaned project README.

- [ ] **Step 3: Push without force**

Run `git push -u origin main`. If authentication fails or the remote has moved, stop and report; do not force-push.

- [ ] **Step 4: Rename the repository**

After the owner confirms `fotile-health-meal-agent` or supplies another final name, rename the GitHub repository through the authenticated GitHub UI. Update `origin` to the returned repository URL and verify `git ls-remote origin` succeeds.
