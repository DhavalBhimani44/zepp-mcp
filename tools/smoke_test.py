"""Start the MCP server over stdio and confirm it lists its tools.

Deliberately has no PEP 723 header: it must run inside the PROJECT
environment so that `import mcp` resolves. An inline-dependency block would
give it an isolated interpreter that cannot see zepp_mcp at all.

The unit tests exercise the decoders directly, which means they would still
pass if the MCP layer were broken -- a bad tool signature, a schema the SDK
rejects, an import error at module scope. This catches that class.

No credentials and no network: the server registers its tools at import time
and answers tools/list without ever contacting Zepp.

Run:  uv run tools/smoke_test.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "zepp_auth_status",
    "zepp_daily_summary",
    "zepp_describe_schema",
    "zepp_heart_rate",
    "zepp_list_workouts",
    "zepp_raw_request",
    "zepp_sleep",
    "zepp_workout_detail",
}

REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "smoke-test", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


def main() -> int:
    payload = "".join(json.dumps(message) + "\n" for message in REQUESTS)
    try:
        process = subprocess.run(
            [sys.executable, "-m", "zepp_mcp.server"],
            input=payload, capture_output=True, text=True, timeout=90, cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        print("FAILED: server did not exit after its input stream closed",
              file=sys.stderr)
        return 1

    responses = {}
    for line in process.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" in message:
            responses[message["id"]] = message

    if 1 not in responses:
        print("FAILED: no response to initialize", file=sys.stderr)
        print(process.stderr[:2000], file=sys.stderr)
        return 1

    server_info = responses[1].get("result", {}).get("serverInfo", {})
    print(f"initialize -> {server_info.get('name')} {server_info.get('version')}")

    if 2 not in responses:
        print("FAILED: no response to tools/list", file=sys.stderr)
        print(process.stderr[:2000], file=sys.stderr)
        return 1

    listed = {tool["name"] for tool in responses[2]["result"]["tools"]}
    print(f"tools/list -> {len(listed)} tools")

    missing = EXPECTED_TOOLS - listed
    extra = listed - EXPECTED_TOOLS
    if missing:
        print(f"FAILED: missing tools: {sorted(missing)}", file=sys.stderr)
    if extra:
        print(f"NOTE: undeclared tools present: {sorted(extra)}. Add them to "
              f"EXPECTED_TOOLS and to the README table.", file=sys.stderr)
    if missing or extra:
        return 1

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
