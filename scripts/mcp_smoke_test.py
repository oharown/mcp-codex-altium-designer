#!/usr/bin/env python3
"""Smoke-test the MCP stdio server without launching Altium Designer."""

from __future__ import annotations

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
        init = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "0.1.0"},
                },
            },
        )
        tools = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        status = send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "altium_bridge_status", "arguments": {}},
            },
        )

        assert init["result"]["serverInfo"]["name"] == "codex-altium-bridge"
        tool_names = [tool["name"] for tool in tools["result"]["tools"]]
        assert "altium_bridge_status" in tool_names
        assert "altium_ping" in tool_names
        assert "altium_find_output_jobs" in tool_names
        assert "altium_prepare_output_generation" in tool_names
        assert "altium_project_health_check" in tool_names
        assert "altium_export_project_health_report" in tool_names
        assert "altium_run_project_validation" in tool_names
        assert "altium_open_pcb_drc_dialog" in tool_names
        assert "altium_run_active_output_container" in tool_names
        assert "altium_list_pcb_nets" in tool_names
        assert "altium_check_pcb_nets" in tool_names
        assert "altium_export_pcb_net_report" in tool_names
        assert "altium_export_bom_with_fields" in tool_names
        assert "altium_export_component_list" in tool_names
        assert "altium_export_component_parameters" in tool_names
        assert "altium_export_sch_pcb_comparison_report" in tool_names
        assert "altium_check_component_fields" in tool_names
        assert "altium_export_component_field_report" in tool_names
        assert "altium_suggest_schematic_designator_fixes" in tool_names
        assert "altium_apply_schematic_designator_fixes" in tool_names
        assert "altium_update_schematic_parameters" in tool_names
        assert status["result"]["content"][0]["type"] == "text"

        print("MCP smoke test passed.")
        print("Available tools:", ", ".join(tool_names))
        print("Bridge status payload:")
        print(status["result"]["content"][0]["text"])
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
