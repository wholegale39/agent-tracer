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


def main():
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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    BASE = args.base
    args.func(args)


if __name__ == "__main__":
    main()
