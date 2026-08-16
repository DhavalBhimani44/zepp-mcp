"""Start the MCP server over stdio and confirm it lists its tools.

Deliberately has no PEP 723 header: it must run inside the PROJECT
environment so that `import mcp` resolves. An inline-dependency block would
give it an isolated interpreter that cannot see zepp_mcp at all.

The unit tests exercise the decoders directly, which means they would still
pass if the MCP layer were broken -- a bad tool signature, a schema the SDK
rejects, an import error at module scope. This catches that class.

No credentials and no network: the server registers its tools at import time
and answers tools/list without ever contacting Zepp.

Sends one request at a time and waits for its response before sending the
next. An earlier version wrote all three messages up front and closed stdin,
which raced: the server could reach end-of-input and shut down before
answering tools/list. That produced a CI failure on one Python version and a
pass on the others from identical code -- the worst kind of check, because it
looks like a real incompatibility.

Run:  uv run tools/smoke_test.py
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 60

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


def _reader(stream, sink: queue.Queue) -> None:
    for line in stream:
        sink.put(line)
    sink.put(None)


def _await_response(sink: queue.Queue, request_id: int) -> dict | None:
    """Read lines until the response with this id arrives, or time out."""
    while True:
        try:
            line = sink.get(timeout=TIMEOUT_SECONDS)
        except queue.Empty:
            return None
        if line is None:  # stdout closed
            return None
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue  # not JSON-RPC; ignore
        if message.get("id") == request_id:
            return message


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, "-m", "zepp_mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, cwd=ROOT,
    )
    sink: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, sink),
                     daemon=True).start()

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def bail(reason: str) -> int:
        print(f"FAILED: {reason}", file=sys.stderr)
        process.kill()
        stderr = process.stderr.read() if process.stderr else ""
        if stderr.strip():
            print(stderr[:2000], file=sys.stderr)
        return 1

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "smoke-test", "version": "1"}}})
        response = _await_response(sink, 1)
        if response is None:
            return bail("no response to initialize")
        info = response.get("result", {}).get("serverInfo", {})
        print(f"initialize -> {info.get('name')} {info.get('version')}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        response = _await_response(sink, 2)
        if response is None:
            return bail("no response to tools/list")

        listed = {tool["name"] for tool in response["result"]["tools"]}
        print(f"tools/list -> {len(listed)} tools")

        missing = EXPECTED_TOOLS - listed
        extra = listed - EXPECTED_TOOLS
        if missing:
            return bail(f"missing tools: {sorted(missing)}")
        if extra:
            return bail(
                f"undeclared tools present: {sorted(extra)}. Add them to "
                f"EXPECTED_TOOLS and to the README table."
            )
    finally:
        if process.poll() is None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
