# H05 新画像构建与独立容器发布收尾计划

**Goal:** 修复新版固定数据构建的最后一条时长图门禁，在完全独立的 H05 容器组中初始化并验证新版 MySQL/Qdrant/Redis，切换 API 后删除旧 H04 容器组。

## Global Constraints

- H05 使用独立容器、独立数据卷和独立端口：MySQL `3308`、Qdrant REST/gRPC `6337/6338`、Redis `6381`。
- H05 初始化完成前不得停止、写入或删除 H04；H04 仅在 H05 数据、向量、API 与关键检索均通过后删除。
- 不读取或复制 `datas`；新版事实来源仍为原始菜品表和已批准的 `data/review` / `data/reference`。
- 不放宽 `G16_TIME_GRAPH_COMPLETE_AND_ACYCLIC`，不增加虚假步骤或虚假时长来绕过门禁。
- 不修改用户拥有的候选报告和审阅脚本；不得把 pending/rejected 决定自动改为 approved。
- 不创建普通分支提交；沿用独立临时 Git index 与不可达 `commit-tree` 构建快照。
- 每项执行后由独立审查代理检查规格与质量；最终由控制代理做全局一致性验收。

## Task 1: 修复菜品 408 的时长图步骤覆盖

- 以新版 staging 中 `recipe_id=408` 的 6 个步骤和 5 个任务为复现证据。
- 先增加或调整回归测试，证明每个源步骤至少映射到一个合法任务。
- 找到遗漏的真实步骤并通过原子化/审阅决定/缓存的正确入口修复；不得修改质量门禁阈值。
- 运行时长图局部测试，并重新执行固定数据构建，确认 G16 不再报告该菜。
- 报告改动文件、测试命令、构建证据和剩余风险。

## Task 2: 构建并初始化独立 H05 数据存储

- 从 Task 1 通过后的不可达 Git 构建快照生成全新 staging；校验 manifest 和全部质量门禁。
- 初始化前确认 H05 MySQL 与 Qdrant 为空，确认目标端口和容器名属于 `food_agent_v2_h05`。
- 将同一 `build_id` 的固定 Artifact 原子发布到 H05 MySQL 与 Qdrant；Redis 保持独立。
- 校验 ready build、schema versions、Artifact 数量、1932 个菜品 RAG 文档、Qdrant alias/point 数、MySQL/Qdrant recipe ID 一致性和依赖字段样例。
- 不停止或删除 H04。

## Task 3: H05 API 验收、切换与 H04 清理

- 先在 `127.0.0.1:8002` 启动指向 H05 的临时 API，启用 RAG warmup。
- 校验 `/health`、`/ready`、真实 Qdrant 检索及关键“老人晚餐”餐次硬过滤链路。
- 审核通过后停止旧 8001 API，在 8001 启动指向 H05 的 API并再次校验。
- 删除前列出 H04 的精确容器、网络和卷；只删除 compose project `food_agent_v2_h04` 的资源。
- 最终确认仅 H05 容器组运行，8001 API ready，H04 资源已不存在。

