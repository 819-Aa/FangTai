# 整改执行证据与任务信封

本目录保存 T01 建立、由外部控制者维护的不可变任务护栏：

- `task-envelope.schema.json` — TaskEnvelope 的权威 JSON Schema。
- `tasks/T01.yaml` — T01 的任务信封；后续每个任务由外部控制者写入一份
  `tasks/T0X.yaml`，DeepSeek 无写权限。

## 任务信封（TaskEnvelope）

每个任务开始前由外部控制者提供不可变信封，字段见
`task-envelope.schema.json`：

- `task_id`：如 `T05`；
- `spec_hash`：除自身外全部字段的规范 SHA-256；
- `base_commit`：合法 Git 基线 commit，必须等于当前 HEAD；
- `allowed_paths` / `forbidden_paths`：写入白名单/黑名单，支持
  `**`、`*`、`?` glob，`forbidden` 优先于 `allowed`；
- `required_tests` / `required_invariants` / `required_artifacts`；
- `human_gates` / `stop_conditions`。

### 规范 spec_hash 算法

对信封对象中除 `spec_hash` 之外的全部字段：

1. 按键名排序（`sort_keys=True`）；
2. 以 `ensure_ascii=False`、分隔符 `(",", ":")` 序列化为 JSON；
3. 对 UTF-8 字节做 SHA-256。

生成或核对信封时使用：

```powershell
uv run python scripts/execution/verify_task_envelope.py --envelope execution/tasks/T01.yaml --print-spec-hash
```

### 信封格式约束

- 文件名后缀为 `.yaml`，但内容必须是 JSON 兼容 YAML（即合法 JSON），
  以便无需额外 YAML 依赖即可解析。
- 除 T01 外，`tasks/*.yaml` 由外部控制者写入；实现任务不得修改自己的
  信封或已锁定信封。

## 执行护栏脚本

- `scripts/execution/check_changed_paths.py` — 校验变更路径：先拒绝
  `forbidden` 黑名单，再要求全部落在 `allowed` 白名单。
- `scripts/execution/verify_task_envelope.py` — 校验信封结构、规范
  `spec_hash` 与 `base_commit == HEAD`。
- `scripts/execution/collect_evidence.py` — 记录命令证据（stdout/stderr
  落盘 + SHA-256 + UTC 时间 + 退出码）并校验完整性；缺 `exit_code` 拒绝。

错误码与退出码映射见各脚本的 `EXIT_CODES`。
