# V2 最终交付收口设计

## 1. 当前全局位置

V2 已完成并保留以下事实：固定 2,000 条源菜品已经离线构建，H01/H02 人工决策已冻结，H04/T23 隔离 MySQL、Qdrant、Redis 已初始化为批准 build `8f98393e-4ae2-4c00-bd0b-1cb07cd91a6f`；MC-01 已封闭安全候选与业务终态，MC-02 已让在线健康档案只读唯一 ready MySQL build，并完成 fail-closed 修复。

剩余问题集中在交付面，不再是数据工程：已提交结果无法完整恢复、API readiness 过弱、前端没有结构化菜单、T23 脚本只能创建新环境不能复验既有授权环境，以及最终 live/浏览器/跨库证据尚未重新跑通。

## 2. 交付目标

竞赛交付必须满足一条可演示主链：

`匿名参与者请求 → 唯一 ready build → 检索/健康/菜单/回答 → 原子提交 → API/SSE/轮询/刷新一致展示 → 可重复验收`

完成标准是系统能在当前已授权 T23 隔离环境运行与复验；不要求生产级集群、监控平台或动态数据更新能力。

## 3. 设计原则

1. MySQL 原子提交是成功事实，Redis/SSE 是可重试传输，Qdrant 是检索索引；三者不得互相冒充。
2. 最终菜单的规范身份始终是 `plan_id + menu_hash + recipe_ids`。菜名是从同一 ready build 固定视图派生的公开展示字段。
3. 不新增原始数据、不重建 B1、不修改已签 CSV；所有后续验收复用现有 T23 数据并保持只读。
4. `/health` 只表示进程存活；新增 `/ready` 验证 MySQL 唯一 ready build、Redis、Qdrant collection 与固定数据身份。
5. 前端同时支持 SSE 正常完成、SSE 降级轮询、页面刷新后的会话菜单恢复；三条路径必须展示同一菜单身份。
6. 首次创建 T23 与复验既有 T23 是两个显式模式；默认不删除、不初始化、不重建任何卷。

## 4. 剩余阶段

### MC-03：已提交结果与恢复

- Application 成功后生成公开 `result_summary`；
- outbox 即时投递失败不反写业务失败；
- `result_summary`、outbox 与 C4 会话菜单共享同一身份；
- 菜名由当前唯一 ready build 的 `recipe_retrieval_build_views` 派生，禁止从模型回答解析。

### MC-04：readiness 与 API 边界

- 保留 `/health` 轻量 liveness；
- 新增 `/ready`，成功返回批准/当前 build 与各依赖摘要，失败统一 503 公共错误；
- readiness 不加载大模型，不做数据写入；真实模型能力由 live 测试证明。

### MC-05：前端结构化菜单与恢复

- store 保存 `currentMenu`；
- `result_committed` 与状态轮询均消费同一 `menu_summary`；
- App 挂载后通过 `/v1/sessions/{session_id}` 恢复当前菜单；
- ChatPanel 显示结构化菜名列表、完成身份与回答正文，不渲染健康隐私字段。

### MC-06：验收与交付

- 重构验收脚本为 `InitializeAuthorizedEmptyT23` 与 `UseExistingAuthorizedT23` 两种互斥模式；
- 本次只使用 `UseExistingAuthorizedT23`，验证容器、卷、批准 manifest/build、1914 点位与固定 artifact，不初始化；
- 依次执行确定性回归、真实模型 live、API/SSE E2E、Playwright、跨库一致性；
- 输出 T24 报告，明确 PASS/FAIL/NOT_RUN/BLOCKED。只有真实链路通过才标记可交付。

## 5. 明确不做

- 动态新增/删除菜品或用户档案；
- Kubernetes、云部署、观测平台、复杂权限后台；
- 新的营养/家庭计量体系；
- 为追求测试数量扩大架构；
- 用 mock 结果替代真实模型和浏览器的最终验收。

## 6. 风险控制

- 每个 MC 独立提交并运行受影响回归；失败不跨阶段扩散。
- T23 容器和卷只读复验，不执行 `down -v`、`data-rebuild` 或 `data-initialize`。
- 任何完成状态都必须能在 MySQL 找到同 request 的不可变提交；任何菜单展示都必须与 `menu_hash/recipe_ids` 一致。
- 外部模型或环境不可用时如实返回 BLOCKED/NOT_RUN，不修改系统语义绕过。
