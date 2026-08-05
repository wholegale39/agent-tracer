# Agent Call Tracer · Agent 调用追踪器

![License](https://img.shields.io/badge/license-MIT-green) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![GitHub stars](https://img.shields.io/github/stars/wholegale39/agent-tracer)

记录 Agent 的每一次工具调用——入参、出参、耗时、错误。支持按会话回溯、跨工具过滤、错误汇总，还能对失败的调用生成重放指令。**升级版自带回归测试闭环：把一次成功运行提升为黄金用例，后续运行自动对比，漂移和回归一眼可见。**

## 为什么做这个？

Agent 跑了什么？调了什么工具？为什么失败了？

- ❌ Agent 执行完说"完成"了，但结果不对，不知道中间发生了什么
- ❌ 某个工具调用超时了，但重跑可能就好了——得手动记下参数再跑一次
- ❌ cron 任务失败了，翻日志像大海捞针
- ❌ Agent 的行为悄悄变了（多调了个工具、参数结构变了、原来成功的步骤开始失败），没人发现

Agent Call Tracer 像 DVR 一样录下 Agent 的每个动作，事后可以一帧一帧回放，还能**自动发现行为漂移**。

## 快速开始

```bash
git clone https://github.com/wholegale39/agent-tracer.git
cd agent-tracer

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 启动
python3 -m uvicorn src.api:app --host 0.0.0.0 --port 8770
```

## 使用示例

### 记录一次 Agent 运行

```python
import httpx
c = httpx.Client(base_url="http://localhost:8770")

# 创建 trace
trace = c.post("/traces", json={
    "agent": "market-summary",
    "task": "收盘汇总",
}).json()
trace_id = trace["trace_id"]

# 记录每个工具调用
c.post(f"/traces/{trace_id}/spans", json={
    "tool_name": "web_search",
    "arguments": {"query": "上证指数收盘"},
    "result": "上证指数收盘3200.15 涨0.85%",
    "duration_ms": 850,
})

c.post(f"/traces/{trace_id}/spans", json={
    "tool_name": "api_call",
    "arguments": {"url": "https://api.example.com/market"},
    "error": "timeout after 30s",
    "duration_ms": 30500,
})

# 结束 trace
c.post(f"/traces/{trace_id}/finish")
```

### 事后排查

```bash
# 看所有错误
curl -s http://localhost:8770/spans/errors

# 查某个 trace 的完整链路
curl -s http://localhost:8770/traces/4bd366dc7e0d

# 对失败的调用生成重放指令
curl -s -X POST http://localhost:8770/spans/4/replay \
  -H 'Content-Type: application/json' \
  -d '{"arguments_override":{"command":"python3 gen_chart.py --timeout 60"}}'
```

## CLI 工具

```bash
# 列 traces
python3 tracer.py traces --limit 10

# 查看完整 trace
python3 tracer.py trace <trace_id>

# 只看错误 span
python3 tracer.py errors

# 生成重放指令
python3 tracer.py replay <span_id>

# ── 回归测试闭环 (v0.2) ──

# 把一次运行提升为黄金用例
python3 tracer.py promote <trace_id>

# 列黄金用例（带最近检测结果）
python3 tracer.py cases

# 看某个用例的黄金签名
python3 tracer.py case <case_id>

# 用指定 trace 检测（或省略 --trace 自动用该 agent 最近一次）
python3 tracer.py check <case_id> [--trace <trace_id>]

# 检测某 trace 是否符合它 agent 的黄金用例
python3 tracer.py check-trace <trace_id>

# ── 导出 & 漂移报告 (v0.3) ──

# 导出黄金用例为自包含 pytest 回归测试文件
python3 tracer.py export-case <case_id> -o test_golden.py

# 导出某次 trace 为 JSON（离线分析 / 喂给 pytest）
python3 tracer.py export-trace <trace_id> -o trace.json

# 批量漂移趋势报告：某 agent 最近 N 次运行 vs 黄金用例
python3 tracer.py drift <agent> [--limit 20]
```

## 回归测试闭环（v0.2 新增）

把一次成功运行 `promote` 为**黄金用例**，之后同一 agent+task 的每次运行都自动对比：

- ✅ **match** — 与黄金用例结构完全一致
- ⚠️ **drift** — 行为变了但没坏：工具顺序变化、参数结构增减、多了/少了调用
- 🔴 **regression** — 原来成功的步骤开始失败（status flip），或关键步骤缺失

对比基于**结构签名**（工具序列 + 每个 span 的参数 key 集合 + 成败状态），不依赖具体返回值，所以能发现"参数悄悄多了一个字段"这类静默变化。适合挂进 CI 或 cron：每次 agent 跑完调一下 `check-trace`，回归秒级暴露。

## 导出 & 漂移报告（v0.3 新增）

把黄金用例导出成**自包含的 pytest 回归测试文件**——嵌入了签名和对比逻辑，不依赖本仓库，直接丢进任何项目的 `tests/` 就能跑：

```bash
# 导出黄金用例为 pytest 文件
python3 tracer.py export-case <case_id> -o test_golden.py

# 导出某次 trace 为 JSON（喂给 pytest / 离线分析）
python3 tracer.py export-trace <trace_id> -o trace.json

# 跑回归测试（宽松：不许出现 regression；严格：必须与黄金完全一致）
TRACE_FILE=trace.json pytest test_golden.py
```

生成的测试含两个断言：

- `test_no_regression` — 宽松：任何在黄金用例中成功的步骤现在失败即报错
- `test_structure_matches_golden` — 严格：工具序列 + 参数结构必须与黄金一致

批量漂移趋势报告——一次对比某 agent 最近 N 次运行，看回归率：

```bash
python3 tracer.py drift <agent> [--limit 20]
# API: GET /agents/{agent}/drift-report?limit=20
```

输出每次运行的判定 + 汇总（✅/⚠️/🔴 计数、regression rate、首次回归时间），适合 cron 定期跑，Agent 行为漂移第一时间暴露。

## API

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/traces` | 创建一次 trace |
| `POST` | `/traces/{id}/finish` | 结束 trace |
| `GET` | `/traces` | 列 traces（`?agent=xxx` 过滤） |
| `GET` | `/traces/{id}` | 完整 trace + 所有 spans |
| `POST` | `/traces/{id}/spans` | 添加一条 span |
| `GET` | `/spans?tool=xxx` | 查 spans |
| `GET` | `/spans/errors` | 只看失败的 spans |
| `POST` | `/spans/{id}/replay` | 生成重放指令 |
| `POST` | `/traces/{id}/promote` | 提升为黄金回归用例 (v0.2) |
| `GET` | `/cases` | 列黄金用例（`?agent=xxx` 过滤）(v0.2) |
| `GET` | `/cases/{id}` | 用例详情 + 黄金签名 (v0.2) |
| `POST` | `/cases/{id}/check?trace_id=xxx` | 用例 vs 指定 trace（省略 trace_id 自动用该 agent 最近一次）(v0.2) |
| `POST` | `/traces/{id}/check` | trace vs 该 agent 的黄金用例 (v0.2) |
| `GET` | `/traces/{id}/export` | 导出 trace 为 JSON (v0.3) |
| `GET` | `/cases/{id}/export` | 导出黄金用例为 pytest 文件 (v0.3) |
| `GET` | `/agents/{agent}/drift-report` | 批量漂移趋势报告 (v0.3) |

## 数据模型

```
Trace (一次 Agent 运行)
 ├── agent: "market-summary"
 ├── task: "收盘汇总"
 └── spans: [...]
      └── Span (一次工具调用)
           ├── tool_name: "web_search"
           ├── arguments: {query: "上证指数"}
           ├── result: "3200点"
           ├── error: null
           └── duration_ms: 850

Case (黄金回归用例, v0.2)
 ├── name: "market-bot/收盘汇总 · 7c53817c"
 ├── source_trace_id: 被提升的 trace
 ├── signature: {tool_sequence, spans: [{tool, arg_keys, status}]}
 └── last_verdict: match | drift | regression
```

## 架构

```
FastAPI (8770)
  └─ TraceStore (SQLite)
       ├─ traces 表
       ├─ spans 表 + 索引 (by trace, by tool, by error)
       └─ cases 表 + 索引 (by agent)   ← v0.2 回归用例
```

## 测试

```bash
# 单元测试（无需起服务）：回归引擎 + 导出功能
python3 -m pytest -q

# 端到端（需先起服务）
python3 -m uvicorn src.api:app --host 127.0.0.1 --port 8770
python3 test_e2e.py
python3 test_e2e_regression.py
```

## 许可证

MIT
