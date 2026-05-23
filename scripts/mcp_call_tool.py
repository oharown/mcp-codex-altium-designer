#!/usr/bin/env python3
"""Call one tool on the local MCP stdio server."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


def send(proc: subprocess.Popen[str], message: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("MCP server closed stdout")
    return json.loads(line)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("tool_name")
    parser.add_argument("--arguments-json", default=None)
    parser.add_argument("--source", choices=["both", "schematic", "pcb"], default=None)
    parser.add_argument("--format", choices=["csv", "json"], default=None)
    parser.add_argument("--filename", default=None)
    parser.add_argument("--required-field", action="append", default=None)
    parser.add_argument("--include-field", action="append", default=None)
    parser.add_argument("--mode", choices=["folder_structure", "pdf"], default=None)
    parser.add_argument("--skip-bom", action="store_true")
    parser.add_argument("--skip-output-jobs", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    args = parser.parse_args()

    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    try:
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-call-tool", "version": "0.1.0"},
                },
            },
        )
        tool_arguments = (
            json.loads(args.arguments_json)
            if args.arguments_json is not None
            else {}
        )
        tool_arguments.setdefault("timeout_seconds", args.timeout_seconds)
        if args.source is not None:
            tool_arguments["source"] = args.source
        if args.format is not None:
            tool_arguments["format"] = args.format
        if args.filename is not None:
            tool_arguments["filename"] = args.filename
        if args.required_field is not None:
            tool_arguments["required_fields"] = args.required_field
        if args.include_field is not None:
            tool_arguments["include_fields"] = args.include_field
        if args.mode is not None:
            tool_arguments["mode"] = args.mode
        if args.skip_bom:
            tool_arguments["include_bom"] = False
        if args.skip_output_jobs:
            tool_arguments["include_output_jobs"] = False
        if args.confirm:
            tool_arguments["confirm"] = True

        result = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": args.tool_name,
                    "arguments": tool_arguments,
                },
            },
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
