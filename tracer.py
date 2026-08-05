"""CLI for Agent Call Tracer — query traces, replay spans."""

import json
import sys
import httpx


BASE = "http://localhost:8770"


def api(method, path, **kwargs):
    url = f"{BASE}{path}"
    r = httpx.request(method, url, **kwargs)
    r.raise_for_status()
    return r.json()


def cmd_traces(args):
    result = api("GET", f"/traces?limit={args.limit}")
    if not result:
        print("No traces found.")
        return
    for t in result:
        finished = "✓" if t.get("finished_at") else "⋯"
        spans = t.get("span_count", "?")
        print(f"  {finished} {t['id']}  {t['agent']:<16}  {spans} spans  {t['started_at'][:19]}")
        if t.get("task"):
            print(f"     task: {t['task']}")


def cmd_trace(args):
    t = api("GET", f"/traces/{args.trace_id}")
    print(f"Trace: {t['id']}")
    print(f"Agent: {t['agent']}  Task: {t['task']}")
    print(f"Spans: {t['span_count']}")
    print()
    for s in t.get("spans", []):
        dur = f"({s.get('duration_ms', '?')}ms)" if s.get('duration_ms') else ""
        err = " ❌" if s.get("error") else ""
        print(f"  #{s['sequence']} {s['tool_name']} {dur}{err}")
        if s.get("error"):
            print(f"    error: {s['error'][:100]}")
        if s.get("result") and len(s["result"]) < 200:
            print(f"    result: {s['result'][:100]}")


def cmd_spans(args):
    params = f"?tool_name={args.tool}" if args.tool else f"?trace_id={args.trace}" if args.trace else ""
    result = api("GET", f"/spans{params}")
    for s in result:
        dur = f"({s.get('duration_ms', '?')}ms)" if s.get('duration_ms') else ""
        err = " ❌" if s.get("error") else ""
        print(f"  #{s['id']} {s['tool_name']} {dur}{err}")


def cmd_errors(args):
    result = api("GET", "/spans/errors")
    for s in result:
        print(f"  #{s['id']} {s['tool_name']}")
        print(f"    error: {s.get('error', '')[:200]}")


def cmd_replay(args):
    req = {"span_id": args.span_id}
    data = api("POST", f"/spans/{args.span_id}/replay", json=req)
    print(f"Tool: {data['original_tool']}")
    print(f"Merged args: {json.dumps(data['merged_arguments'], indent=2)}")
    print()
    print(f"Replay command:")
    print(f"  {data['replay_instructions']}")


def cmd_promote(args):
    data = api("POST", f"/traces/{args.trace_id}/promote", params={"task": args.task} if args.task else {})
    print(f"✅ Golden case created: {data['case_id']}")
    print(f"   {data['name']}")
    print(f"   source trace: {data['source_trace_id']} ({data['spans']} spans)")


def cmd_cases(args):
    params = f"?agent={args.agent}" if args.agent else ""
    result = api("GET", f"/cases{params}")
    if not result:
        print("No golden cases yet. Promote a trace: tracer.py promote <trace_id>")
        return
    for c in result:
        verdict = c.get("last_verdict", "—")
        score = c.get("last_score", "—")
        mark = {"match": "✅", "drift": "⚠️", "regression": "🔴"}.get(verdict, "⚪")
        print(f"  {mark} {c['id']}  {c['name']}")
        print(f"     agent={c['agent']}  last={verdict} ({score})  src={c['source_trace_id']}")


def cmd_case(args):
    data = api("GET", f"/cases/{args.case_id}")
    print(f"Case: {data['name']}  ({data['id']})")
    print(f"Agent: {data['agent']}  Task: {data['task']}")
    print(f"Source trace: {data['source_trace_id']}")
    print(f"Last check: {data.get('last_verdict', '—')} ({data.get('last_score', '—')})")
    print()
    sig = data["signature"]
    print(f"Golden signature: {sig['span_count']} spans")
    for i, s in enumerate(sig["spans"]):
        mark = "❌" if s["status"] == "error" else "✓"
        print(f"  #{i + 1} {s['tool']} {s['arg_keys']} {mark}")


def cmd_check(args):
    data = api("POST", f"/cases/{args.case_id}/check", params={"trace_id": args.trace_id} if args.trace_id else {})
    mark = {"match": "✅", "drift": "⚠️", "regression": "🔴"}.get(data["verdict"], "?")
    print(f"{mark} {data['verdict'].upper()}  (score {data['score']})")
    print(f"   checked trace: {data['checked_trace_id']}  ({data['checked_agent']}/{data['checked_task']})")
    print(f"   golden {data['golden_span_count']} spans → new {data['new_span_count']} spans")
    for d in data["diffs"]:
        print(f"   - [{d['kind']}] {d['message']}")
    if data["verdict"] == "match":
        print("   identical to golden case")


def cmd_check_trace(args):
    data = api("POST", f"/traces/{args.trace_id}/check")
    if not data.get("found"):
        print(f"⚪ {data['message']}")
        return
    mark = {"match": "✅", "drift": "⚠️", "regression": "🔴"}.get(data["verdict"], "?")
    print(f"{mark} {data['verdict'].upper()}  (score {data['score']})")
    print(f"   case: {data['case_name']}")
    print(f"   golden {data['golden_span_count']} spans → new {data['new_span_count']} spans")
    for d in data["diffs"]:
        print(f"   - [{d['kind']}] {d['message']}")
    if data["verdict"] == "match":
        print("   identical to golden case")


def main():
    global BASE
    import argparse
    parser = argparse.ArgumentParser(description="Agent Call Tracer CLI")
    parser.add_argument("--base", default=BASE, help="API base URL")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("traces", help="List recent traces")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_traces)

    p = sub.add_parser("trace", help="Show a trace with all spans")
    p.add_argument("trace_id")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("spans", help="List spans")
    p.add_argument("--tool", default="", help="Filter by tool name")
    p.add_argument("--trace", default="", help="Filter by trace ID")
    p.set_defaults(func=cmd_spans)

    p = sub.add_parser("errors", help="List error spans")
    p.set_defaults(func=cmd_errors)

    p = sub.add_parser("replay", help="Get replay instructions for a span")
    p.add_argument("span_id", type=int)
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("promote", help="Promote a trace into a golden regression case")
    p.add_argument("trace_id")
    p.add_argument("--task", default="", help="Override task name for the case")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("cases", help="List golden regression cases")
    p.add_argument("--agent", default="", help="Filter by agent")
    p.set_defaults(func=cmd_cases)

    p = sub.add_parser("case", help="Show a regression case's golden signature")
    p.add_argument("case_id")
    p.set_defaults(func=cmd_case)

    p = sub.add_parser("check", help="Check a case against a trace (or its agent's latest)")
    p.add_argument("case_id")
    p.add_argument("--trace", dest="trace_id", default="", help="Specific trace ID to check")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("check-trace", help="Check a trace against the golden case for its agent")
    p.add_argument("trace_id")
    p.set_defaults(func=cmd_check_trace)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    BASE = args.base
    args.func(args)


if __name__ == "__main__":
    main()
