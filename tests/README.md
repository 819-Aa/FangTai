# 测试与验收入口

当前验收环境是 H07：MySQL `3309`、Qdrant `6339/6340`、Redis `6382`，生成模型为 `qwen3.8-max`。健康硬筛选、工具权限、状态流转和最终菜单一致性采用确定性断言；模型表达质量测试不能替代健康安全验证。

## 推荐命令

```powershell
# H07/千问配置预检
powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1 -PreflightOnly

# 当前默认验收：预检 + 非 live 全量回归
powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1

# 额外执行真实模型提示词验收
powershell -ExecutionPolicy Bypass -File scripts/run_full_acceptance.ps1 -IncludeModelLive
```

也可直接运行：

```powershell
uv run pytest -m "not live" -q
uv run pytest tests/test_prompts_live.py -q
```

`live` 标记包含真实模型、真实端到端或依赖外部批准缓存的测试。全量数据重建测试需要未纳入 Git 的 `data/cache/recipe_time_graphs.jsonl`；没有该缓存时不得在 rebuild 内隐式调用模型。

Windows 未开放符号链接权限时，3 条别名安全测试会按设计 skip。前端 Playwright 使用 `H07_API_BASE`，默认指向 `http://localhost:8003`。
