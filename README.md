# Agent Call Tracer · Agent 调用追踪器

![License](https://img.shields.io/badge/license-MIT-green) ![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![GitHub stars](https://img.shields.io/github/stars/wholegale39/agent-tracer)


记录 Agent 的每一次工具调用——入参、出参、耗时、错误。支持按会话回溯、跨工具过滤、错误汇总，还能对失败的调用生成重放指令。

## 为什么做这个？

Agent 跑了什么？调了什么工具？为什么失败了？

- ❌ Agent 执行完说"完成"了，但结果不对，不知道中间发生了什么
- ❌ 某个工具调用超时了，但重跑可能就好了——得手动记下参数再跑一次
- ❌ cron 任务失败了，翻日志像大海捞针

Agent Call Tracer 像 DVR 一样录下 Agent 的每个动作，事后可以一帧一帧回放。

## 快速开始

```bash
git clone https://github.com/wholegale39/agent-call-tracer.git
cd agent-call-tracer

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
```

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
```

## 架构

```
FastAPI (8770)
  └─ TraceStore (SQLite)
       ├─ traces 表
       └─ spans 表 + 索引 (by trace, by tool, by error)
```

## 许可证

MIT