# H07 Database Bootstrap Plan

## Objective

Build the currently reviewed data overlay from clean Git provenance, then initialize a brand-new isolated MySQL, Qdrant, and Redis environment. Do not reuse, mutate, stop, delete, or switch traffic away from H06.

## Authorized scope

- Owner authorization: 2026-08-30, after the exact persistent writes and API embedding work were explained.
- Create a non-branch Git snapshot with a temporary index and `commit-tree`.
- Build into the new persistent directory `.staging/h07-data-rebuild-20260830`.
- Create Docker Compose project `food_agent_v2_h07` with new containers and volumes.
- Initialize one empty MySQL database and one empty Qdrant namespace from the same verified manifest.
- Use the configured SiliconFlow embedding API (`BAAI/bge-m3`) for all RAG vectors.
- Perform a scoped Redis read/write/delete probe under prefix `v2:h07`.

## Fixed isolation map

| Resource | H07 target |
| --- | --- |
| Compose project | `food_agent_v2_h07` |
| MySQL container / port / database | `food_agent_v2_h07_mysql` / `3309` / `food_agent_v2_h07` |
| Qdrant container / REST / gRPC | `food_agent_v2_h07_qdrant` / `6339` / `6340` |
| Qdrant published alias | `recipe_retrieval_v2_h07` |
| Redis container / port / key prefix | `food_agent_v2_h07_redis` / `6382` / `v2:h07` |
| Optional future API port | `8003` (not started in this task) |

## Execution gates

1. Record H06 container IDs, volumes, health, MySQL fixed-data counts, Qdrant alias, and Redis database size.
2. Fail closed if any H07 name, volume, port, or staging target already exists.
3. Freeze the reviewed overlay using a separate temporary Git index; do not update a branch, normal index, or user worktree.
4. Add a detached clean worktree at the snapshot commit, copy only the ignored reproducible time-graph cache, and prove tracked status clean.
5. Run `data-rebuild`, `data-verify`, and manifest/count/hash checks in the clean worktree.
6. Start only the three H07 stores and require all of them to become healthy.
7. Run the sole supported initializer, `data-initialize --confirm-empty-v2`, with explicit H07 endpoints and collection name. MySQL must stay uncommitted until Qdrant count, ID, and payload parity pass.
8. Verify MySQL build/artifact counts, Qdrant alias/physical collection/point count/build identity, and a scoped Redis read/write/delete round trip.
9. Recheck the H06 baseline. No API start, traffic switch, old alias update, old resource deletion, or RAG strategy optimization is authorized.

## Failure policy

- Build failure: do not create H07 stores.
- Store startup failure: preserve evidence; do not touch H06 or delete material resources automatically.
- Initialization failure: rely on the initializer's MySQL/Qdrant rollback, record the failure report, and stop before any API work.
- Never expose API keys or database passwords in commands, logs, or reports.

