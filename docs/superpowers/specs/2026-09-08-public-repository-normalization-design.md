# Public Repository Normalization Design

## Goal

Prepare the current project for GitHub presentation without changing business behavior. The public tree should make the active Qwen `fast_path` workflow easy to understand, avoid publishing local infrastructure orchestration, and remove machine-specific paths from tracked content.

## Scope

- Keep `docker-compose.yml` available locally, remove it from Git tracking, and ignore it going forward.
- Remove Docker Compose references and commands from the public README while retaining MySQL, Qdrant, and Redis as application dependencies.
- Use the project name `方太个性化健康膳食推荐 Agent` consistently in the README.
- Replace internal workflow labels such as `B4` and `C2` in the README diagram with business-facing component names.
- Describe only the active `WORKFLOW_MODE=fast_path` workflow in the README.
- Replace tracked Windows user-directory paths with repository-relative or generic paths.
- Normalize human-facing documentation and execution-envelope filenames by convention: Markdown uses lowercase kebab-case, Python files use snake_case, Vue components use PascalCase, and task envelopes use lowercase identifiers.
- Preserve the existing `b1`/`b2`/`c1`-style Python package names because renaming them would be a behavior-affecting refactor across source, tests, and documentation.
- Preserve fixed source-data filenames because they are part of Source Manifest identity and data-build contracts.

## Publication Boundary

The GitHub commit will contain source code, database schema, configuration examples, tests, documentation, and reviewed sample data. It will not contain `.env`, local runtime state, dependency directories, database volumes, generated build artifacts, or `docker-compose.yml`.

The local `docker-compose.yml` remains usable on this machine but is intentionally absent from the published tree.

## Documentation Changes

The README will retain the system workflow, core implementation ideas, data scale, architecture, project structure, API surface, and project boundaries. Local setup will describe the three required storage services as prerequisites rather than claiming that the repository provides their container orchestration.

Static validation counts will be aligned with the linked H07 verification report. No new runtime or live-model test claim will be introduced.

## Validation

Per the owner's instruction, this cleanup will not rerun the application test suites. Validation is limited to:

- Git diff and whitespace checks;
- README relative-link checks;
- scan for tracked local absolute paths and secret-like files;
- verification that `docker-compose.yml` is ignored and absent from the Git index;
- review of the final staged file list before commit and push.

## Non-goals

- No business logic, API, database schema, or frontend behavior changes.
- No renaming of Python packages or imports.
- No renaming of fixed source-data files.
- No Docker startup, data rebuild, or paid live-model execution.
- No deletion of local Docker volumes or other runtime data.
