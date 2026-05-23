#!/usr/bin/env python3
"""Minimal MCP server for bridging Codex to Altium Designer.

This server intentionally has no third-party dependencies. It speaks enough of
the MCP JSON-RPC stdio transport for Codex to discover tools and call them, then
passes requests to an Altium DelphiScript bridge through JSON files.
"""

from __future__ import annotations

from collections.abc import Callable
import csv
import datetime as _dt
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import uuid


BASE_DIR = Path(__file__).resolve().parent
SHARED_DIR = Path(os.environ.get("ALTIUM_MCP_SHARED_DIR", BASE_DIR / "shared"))
EXPORTS_DIR = BASE_DIR / "exports"
OPERATIONS_DIR = SHARED_DIR / "operations"
REQUEST_FILE = SHARED_DIR / "request.json"
RESPONSE_FILE = SHARED_DIR / "response.json"
HEARTBEAT_FILE = SHARED_DIR / "heartbeat.json"
STOP_FILE = SHARED_DIR / "bridge.stop"

SERVER_NAME = "codex-altium-bridge"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

_framing = "line"


def utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_shared_dir() -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)


def ensure_exports_dir() -> None:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_operations_dir() -> None:
    OPERATIONS_DIR.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: object) -> None:
    ensure_shared_dir()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp_path, path)


def read_json(path: Path) -> object:
    raw = path.read_bytes()
    last_error: UnicodeDecodeError | None = None

    for encoding in ("utf-8-sig", "mbcs", "gbk"):
        try:
            return json.loads(raw.decode(encoding))
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except LookupError:
            continue

    if last_error is not None:
        raise last_error
    return json.loads(raw.decode("utf-8-sig"))


def remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def bridge_status() -> dict[str, object]:
    ensure_shared_dir()
    status: dict[str, object] = {
        "shared_dir": str(SHARED_DIR),
        "request_file": str(REQUEST_FILE),
        "response_file": str(RESPONSE_FILE),
        "heartbeat_file": str(HEARTBEAT_FILE),
        "stop_file": str(STOP_FILE),
        "heartbeat_present": HEARTBEAT_FILE.exists(),
        "bridge_likely_running": False,
    }

    if HEARTBEAT_FILE.exists():
        age_seconds = max(0.0, time.time() - HEARTBEAT_FILE.stat().st_mtime)
        status["heartbeat_age_seconds"] = round(age_seconds, 2)
        status["bridge_likely_running"] = age_seconds < 5
        try:
            status["heartbeat"] = read_json(HEARTBEAT_FILE)
        except Exception as exc:  # pragma: no cover - defensive diagnostics
            status["heartbeat_error"] = str(exc)

    return status


def send_bridge_command(command: str, args: dict[str, object], timeout_seconds: float) -> dict[str, object]:
    ensure_shared_dir()
    request_id = uuid.uuid4().hex
    remove_if_exists(RESPONSE_FILE)

    request = {
        "id": request_id,
        "command": command,
        "args": args,
        "created_at": utc_now(),
    }
    write_json_atomic(REQUEST_FILE, request)

    deadline = time.monotonic() + timeout_seconds
    last_decode_error: str | None = None

    while time.monotonic() < deadline:
        if RESPONSE_FILE.exists():
            try:
                response = read_json(RESPONSE_FILE)
            except Exception as exc:
                last_decode_error = str(exc)
                time.sleep(0.15)
                continue

            if isinstance(response, dict) and response.get("id") == request_id:
                return response

        time.sleep(0.2)

    status = bridge_status()
    detail = (
        f"Timed out waiting for Altium bridge response to command '{command}'. "
        "Open Altium Designer and run StartMCPBridge from bridge/AltiumMCPBridge.pas."
    )
    if last_decode_error:
        detail += f" Last response parse error: {last_decode_error}"
    try:
        pending_request = read_json(REQUEST_FILE) if REQUEST_FILE.exists() else None
        if isinstance(pending_request, dict) and pending_request.get("id") == request_id:
            remove_if_exists(REQUEST_FILE)
    except Exception:
        pass
    raise TimeoutError(detail + f" Bridge status: {json.dumps(status, ensure_ascii=False)}")


def text_result(payload: object, *, is_error: bool = False) -> dict[str, object]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    result: dict[str, object] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def argument_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def component_designator(component: object) -> str:
    if not isinstance(component, dict):
        return ""
    value = component.get("designator", "")
    return str(value).strip()


def component_comment(component: object) -> str:
    if not isinstance(component, dict):
        return ""
    value = component.get("comment", "")
    return str(value).strip()


def normalize_field_name(field: str) -> str:
    return " ".join(field.strip().lower().split())


def strip_parameter_prefix(field: str) -> str:
    field_name = field.strip()
    for prefix in ("parameter:", "param:"):
        if field_name.lower().startswith(prefix):
            return field_name[len(prefix):].strip()
    return field_name


def component_parameters(component: object) -> list[dict[str, object]]:
    if not isinstance(component, dict):
        return []

    parameters = component.get("parameters", [])
    if isinstance(parameters, list):
        return [item for item in parameters if isinstance(item, dict)]

    if isinstance(parameters, dict):
        return [
            {"name": str(name), "value": value}
            for name, value in parameters.items()
        ]

    return []


def component_parameter_value(component: object, field: str) -> object | None:
    parameter_name = strip_parameter_prefix(field)
    normalized = normalize_field_name(parameter_name)
    if not normalized:
        return None

    for parameter in component_parameters(component):
        name = parameter.get("name", "")
        if normalize_field_name(str(name)) == normalized:
            return parameter.get("value", "")

    return None


def component_field(component: object, field: str) -> str:
    if not isinstance(component, dict):
        return ""

    field_name = field.strip()
    if not field_name:
        return ""

    value = component.get(field_name)
    if value is None:
        normalized = normalize_field_name(field_name)
        for key, candidate in component.items():
            if normalize_field_name(str(key)) == normalized:
                value = candidate
                break

    if value is None:
        value = component_parameter_value(component, field_name)

    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).strip()
    return str(value).strip()


def component_sort_key(component: object) -> tuple[str, str]:
    return (component_designator(component).upper(), component_comment(component).upper())


def designator_sort_key(designator: str) -> str:
    return designator.upper()


def normalize_required_fields(required_fields: object | None) -> list[str]:
    if required_fields is None:
        return ["footprint"]

    raw_fields: list[object]
    if isinstance(required_fields, str):
        raw_fields = required_fields.split(",")
    elif isinstance(required_fields, list):
        raw_fields = []
        for item in required_fields:
            if isinstance(item, str) and "," in item:
                raw_fields.extend(item.split(","))
            else:
                raw_fields.append(item)
    else:
        raise ValueError("required_fields must be a list of field names or a comma-separated string")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_fields:
        field = str(item).strip()
        if not field:
            continue
        key = field.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(field)

    if not normalized:
        raise ValueError("required_fields must contain at least one field name")
    return normalized


def normalize_include_fields(include_fields: object | None) -> list[str]:
    if include_fields is None:
        return []

    raw_fields: list[object]
    if isinstance(include_fields, str):
        raw_fields = include_fields.split(",")
    elif isinstance(include_fields, list):
        raw_fields = []
        for item in include_fields:
            if isinstance(item, str) and "," in item:
                raw_fields.extend(item.split(","))
            else:
                raw_fields.append(item)
    else:
        raise ValueError("include_fields must be a list of field names or a comma-separated string")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_fields:
        field = str(item).strip()
        if not field:
            continue
        key = normalize_field_name(field)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(field)
    return normalized


def component_available_fields(components: list[dict[str, object]]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for component in components:
        for key in component:
            field = str(key)
            normalized = field.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            fields.append(field)
        for parameter in component_parameters(component):
            name = str(parameter.get("name", "")).strip()
            if not name:
                continue
            field = "parameter:" + name
            normalized = normalize_field_name(field)
            if normalized in seen:
                continue
            seen.add(normalized)
            fields.append(field)
    return sorted(fields, key=lambda field: field.upper())


def field_value_as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def tsv_cell(value: object) -> str:
    return field_value_as_text(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def write_operation_tsv(prefix: str, rows: list[list[object]]) -> Path:
    ensure_operations_dir()
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OPERATIONS_DIR / f"{prefix}_{timestamp}_{uuid.uuid4().hex[:8]}.tsv"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        for row in rows:
            writer.writerow([tsv_cell(cell) for cell in row])
    os.replace(tmp_path, path)
    return path


def designator_prefix_and_number(designator: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", designator.strip())
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def unannotated_designator_prefix(designator: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)\?", designator)
    if not match:
        return ""
    return match.group(1).upper()


def extract_components(response: dict[str, object], document_type: str) -> list[dict[str, object]]:
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error for {document_type}: {json.dumps(response, ensure_ascii=False)}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Altium returned an invalid {document_type} payload: {json.dumps(response, ensure_ascii=False)}")

    components = result.get("components")
    if not isinstance(components, list):
        error = result.get("error", "missing components list")
        raise RuntimeError(f"Altium returned no {document_type} components: {error}")

    return [component for component in components if isinstance(component, dict)]


def extract_nets(response: dict[str, object]) -> list[dict[str, object]]:
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error for PCB nets: {json.dumps(response, ensure_ascii=False)}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Altium returned an invalid PCB nets payload: {json.dumps(response, ensure_ascii=False)}")

    nets = result.get("nets")
    if not isinstance(nets, list):
        error = result.get("error", "missing nets list")
        raise RuntimeError(f"Altium returned no PCB nets: {error}")

    return [net for net in nets if isinstance(net, dict)]


def read_workspace_documents(timeout_seconds: float) -> dict[str, object]:
    response = send_bridge_command("list_workspace_documents", {"timeout_seconds": timeout_seconds}, timeout_seconds)
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error for workspace documents: {json.dumps(response, ensure_ascii=False)}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Altium returned an invalid workspace payload: {json.dumps(response, ensure_ascii=False)}")
    return result


def flatten_workspace_documents(payload: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    projects = payload.get("projects", [])
    if not isinstance(projects, list):
        return rows

    for project in projects:
        if not isinstance(project, dict):
            continue
        project_path = str(project.get("project_full_path", ""))
        project_file = str(project.get("project_file_name", ""))
        for scope, key in (("logical", "logical_documents"), ("physical", "physical_documents")):
            documents = project.get(key, [])
            if not isinstance(documents, list):
                continue
            for document in documents:
                if not isinstance(document, dict) or not document.get("has_document", False):
                    continue
                rows.append(
                    {
                        "project_full_path": project_path,
                        "project_file_name": project_file,
                        "scope": scope,
                        "kind": str(document.get("kind", "")),
                        "full_path": str(document.get("full_path", "")),
                        "file_name": str(document.get("file_name", "")),
                        "loaded": bool(document.get("loaded", False)),
                    }
                )
    return rows


def is_output_document(document: dict[str, object]) -> bool:
    full_path = str(document.get("full_path", ""))
    file_name = str(document.get("file_name", ""))
    kind = str(document.get("kind", "")).lower()
    suffix = Path(full_path or file_name).suffix.lower()
    if suffix in (".outjob", ".bomdoc"):
        return True
    return "outjob" in kind or "outputjob" in kind or "bom" in kind


def find_output_jobs(timeout_seconds: float) -> dict[str, object]:
    payload = read_workspace_documents(timeout_seconds)
    documents = flatten_workspace_documents(payload)
    output_documents = [document for document in documents if is_output_document(document)]
    outjobs = [
        document
        for document in output_documents
        if Path(str(document.get("full_path", "") or document.get("file_name", ""))).suffix.lower() == ".outjob"
        or "outjob" in str(document.get("kind", "")).lower()
        or "outputjob" in str(document.get("kind", "")).lower()
    ]
    return {
        "source": "workspace",
        "project_count": payload.get("project_count", 0),
        "document_count": len(documents),
        "output_document_count": len(output_documents),
        "outjob_count": len(outjobs),
        "output_documents": sorted(output_documents, key=lambda item: str(item.get("full_path", "")).lower()),
        "outjobs": sorted(outjobs, key=lambda item: str(item.get("full_path", "")).lower()),
    }


def prepare_output_generation_plan(timeout_seconds: float) -> dict[str, object]:
    report = find_output_jobs(timeout_seconds)
    outjobs = report.get("outjobs", [])
    outjob_count = len(outjobs) if isinstance(outjobs, list) else 0
    return {
        "source": "workspace",
        "status": "ready" if outjob_count > 0 else "needs_outjob",
        "outjob_count": outjob_count,
        "output_document_count": report.get("output_document_count", 0),
        "outjobs": outjobs if isinstance(outjobs, list) else [],
        "manual_steps": [
            "Open or create an OutJob in Altium Designer.",
            "Configure Gerber, NC Drill, Pick-and-Place, assembly drawings, or BOM outputs in that OutJob.",
            "Run the OutJob from Altium after reviewing output paths.",
        ],
        "notes": [
            "This MCP bridge does not automatically run production outputs yet; it first verifies which OutJob documents are present.",
            "Automatic production output generation should remain an explicit confirmed operation because it writes manufacturing files.",
        ],
    }


def confirmed_bridge_action_preview(
    action: str,
    command: str,
    notes: list[str],
) -> dict[str, object]:
    return {
        "confirmed": False,
        "status": "needs_confirmation",
        "action": action,
        "bridge_command": command,
        "notes": notes,
    }


def run_project_validation(confirm: bool, timeout_seconds: float) -> dict[str, object]:
    notes = [
        "Runs Altium project validation through WorkspaceManager:Compile with Action=Compile and ObjectKind=Project.",
        "Validation findings are shown by Altium in the Messages panel; this bridge reports whether the command was dispatched.",
        "The operation does not save the project automatically.",
    ]
    if not confirm:
        return confirmed_bridge_action_preview("run_project_validation", "run_project_validation", notes)

    response = send_bridge_command(
        "run_project_validation",
        {"timeout_seconds": timeout_seconds},
        timeout_seconds,
    )
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error while running project validation: {json.dumps(response, ensure_ascii=False)}")

    result = response.get("result", {})
    if isinstance(result, dict) and result.get("status") == "error":
        raise RuntimeError(f"Altium bridge failed project validation dispatch: {json.dumps(result, ensure_ascii=False)}")
    return {
        "confirmed": True,
        "status": "dispatched",
        "action": "run_project_validation",
        "result": result,
        "notes": notes,
    }


def open_pcb_drc_dialog(confirm: bool, timeout_seconds: float) -> dict[str, object]:
    notes = [
        "Opens Altium's PCB Design Rule Checker dialog through PCB:DesignRuleCheck.",
        "Batch DRC settings and report generation are controlled in the Altium dialog.",
        "This bridge does not click the dialog's Run Design Rule Check button automatically.",
    ]
    if not confirm:
        return confirmed_bridge_action_preview("open_pcb_drc_dialog", "open_pcb_drc_dialog", notes)

    response = send_bridge_command(
        "open_pcb_drc_dialog",
        {"timeout_seconds": timeout_seconds},
        timeout_seconds,
    )
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error while opening PCB DRC dialog: {json.dumps(response, ensure_ascii=False)}")

    result = response.get("result", {})
    if isinstance(result, dict) and result.get("status") == "error":
        raise RuntimeError(f"Altium bridge failed PCB DRC dialog dispatch: {json.dumps(result, ensure_ascii=False)}")
    return {
        "confirmed": True,
        "status": "dispatched",
        "action": "open_pcb_drc_dialog",
        "result": result,
        "notes": notes,
    }


def run_active_output_container(mode: str, confirm: bool, timeout_seconds: float) -> dict[str, object]:
    normalized_mode = mode.strip().lower().replace("-", "_")
    if normalized_mode in ("folder", "folder_structure", "files", "generate_report"):
        normalized_mode = "folder_structure"
    elif normalized_mode in ("pdf", "publish_pdf", "publish_to_pdf"):
        normalized_mode = "pdf"
    else:
        raise ValueError("mode must be 'folder_structure' or 'pdf'")

    notes = [
        "Generates outputs for the currently selected output container in the active OutJob document.",
        "Use mode='folder_structure' for a Folder Structure output container, or mode='pdf' for a PDF output container.",
        "Review the active OutJob, selected container, enabled outputs, and output paths in Altium before confirming.",
    ]
    if not confirm:
        return {
            **confirmed_bridge_action_preview(
                "run_active_output_container",
                "run_active_output_container",
                notes,
            ),
            "mode": normalized_mode,
        }

    response = send_bridge_command(
        "run_active_output_container",
        {"mode": normalized_mode, "timeout_seconds": timeout_seconds},
        timeout_seconds,
    )
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error while generating outputs: {json.dumps(response, ensure_ascii=False)}")

    result = response.get("result", {})
    if isinstance(result, dict) and result.get("status") == "error":
        raise RuntimeError(f"Altium bridge failed output generation dispatch: {json.dumps(result, ensure_ascii=False)}")
    return {
        "confirmed": True,
        "status": "dispatched",
        "action": "run_active_output_container",
        "mode": normalized_mode,
        "result": result,
        "notes": notes,
    }


def net_name(net: object) -> str:
    if not isinstance(net, dict):
        return ""
    return str(net.get("name", "")).strip()


def net_int(net: object, field: str) -> int:
    if not isinstance(net, dict):
        return 0
    value = net.get(field, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def net_bool(net: object, field: str) -> bool:
    if not isinstance(net, dict):
        return False
    value = net.get(field, False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def net_sort_key(net: object) -> tuple[str, int]:
    return (net_name(net).upper(), net_int(net, "pin_count"))


def index_annotated_components(components: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    for component in components:
        designator = component_designator(component)
        if not designator or "?" in designator:
            continue
        indexed[designator.upper()] = component
    return indexed


def compare_component_sets(
    schematic_components: list[dict[str, object]],
    pcb_components: list[dict[str, object]],
) -> dict[str, object]:
    schematic_index = index_annotated_components(schematic_components)
    pcb_index = index_annotated_components(pcb_components)

    schematic_keys = set(schematic_index)
    pcb_keys = set(pcb_index)
    matched_keys = schematic_keys & pcb_keys

    comment_mismatches = []
    for key in sorted(matched_keys):
        schematic_component = schematic_index[key]
        pcb_component = pcb_index[key]
        schematic_comment = component_comment(schematic_component)
        pcb_comment = component_comment(pcb_component)
        if schematic_comment != pcb_comment:
            comment_mismatches.append(
                {
                    "designator": component_designator(schematic_component),
                    "schematic_comment": schematic_comment,
                    "pcb_comment": pcb_comment,
                }
            )

    unannotated_schematic = [
        component for component in schematic_components if "?" in component_designator(component)
    ]
    unannotated_pcb = [component for component in pcb_components if "?" in component_designator(component)]

    only_in_schematic = [schematic_index[key] for key in sorted(schematic_keys - pcb_keys)]
    only_in_pcb = [pcb_index[key] for key in sorted(pcb_keys - schematic_keys)]

    issue_count = (
        len(only_in_schematic)
        + len(only_in_pcb)
        + len(unannotated_schematic)
        + len(unannotated_pcb)
        + len(comment_mismatches)
    )

    return {
        "status": "ok" if issue_count == 0 else "needs_attention",
        "issue_count": issue_count,
        "counts": {
            "schematic_total": len(schematic_components),
            "pcb_total": len(pcb_components),
            "schematic_annotated": len(schematic_index),
            "pcb_annotated": len(pcb_index),
            "matched_annotated": len(matched_keys),
        },
        "only_in_schematic": sorted(only_in_schematic, key=component_sort_key),
        "only_in_pcb": sorted(only_in_pcb, key=component_sort_key),
        "unannotated": {
            "schematic": sorted(unannotated_schematic, key=component_sort_key),
            "pcb": sorted(unannotated_pcb, key=component_sort_key),
        },
        "comment_mismatches": comment_mismatches,
    }


def compare_sch_pcb_components(timeout_seconds: float) -> dict[str, object]:
    command_args = {"timeout_seconds": timeout_seconds}
    schematic_response = send_bridge_command("list_sch_components", command_args, timeout_seconds)
    pcb_response = send_bridge_command("list_pcb_components", command_args, timeout_seconds)
    schematic_components = extract_components(schematic_response, "schematic")
    pcb_components = extract_components(pcb_response, "pcb")
    return compare_component_sets(schematic_components, pcb_components)


def suggest_schematic_designator_fixes(timeout_seconds: float) -> dict[str, object]:
    document_type, components = read_components_for_source("schematic", timeout_seconds)
    existing_numbers: dict[str, set[int]] = {}
    used_designators = {component_designator(component).upper() for component in components if component_designator(component)}
    key_counts: dict[tuple[str, str], int] = {}

    for component in components:
        designator = component_designator(component)
        comment = component_comment(component)
        key_counts[(designator, comment)] = key_counts.get((designator, comment), 0) + 1
        parsed = designator_prefix_and_number(designator)
        if parsed is None:
            continue
        prefix, number = parsed
        existing_numbers.setdefault(prefix, set()).add(number)

    suggestions = []
    next_numbers = {prefix: (max(numbers) + 1 if numbers else 1) for prefix, numbers in existing_numbers.items()}

    for component in sorted(components, key=component_sort_key):
        designator = component_designator(component)
        if "?" not in designator:
            continue

        prefix = unannotated_designator_prefix(designator)
        comment = component_comment(component)
        if not prefix:
            suggestions.append(
                {
                    "old_designator": designator,
                    "new_designator": "",
                    "comment": comment,
                    "safe_to_apply": False,
                    "reason": "Could not infer a letter prefix before '?'.",
                }
            )
            continue

        candidate_number = next_numbers.get(prefix, 1)
        while f"{prefix}{candidate_number}".upper() in used_designators:
            candidate_number += 1

        new_designator = f"{prefix}{candidate_number}"
        next_numbers[prefix] = candidate_number + 1
        used_designators.add(new_designator.upper())
        ambiguous = key_counts.get((designator, comment), 0) > 1
        suggestions.append(
            {
                "old_designator": designator,
                "new_designator": new_designator,
                "comment": comment,
                "safe_to_apply": not ambiguous,
                "reason": "ok" if not ambiguous else "Multiple schematic components have the same designator/comment pair.",
            }
        )

    safe_count = sum(1 for item in suggestions if item.get("safe_to_apply"))
    return {
        "source": document_type,
        "status": "ok" if not suggestions else ("ready" if safe_count == len(suggestions) else "needs_attention"),
        "total_components": len(components),
        "suggestion_count": len(suggestions),
        "safe_suggestion_count": safe_count,
        "suggestions": suggestions,
        "notes": [
            "Suggestions are based on each unannotated designator prefix and the next unused number in the schematic.",
            "Applying suggestions changes the open schematic in Altium but does not save the project file automatically.",
        ],
    }


def normalize_designator_updates(updates: object | None, timeout_seconds: float) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    plan: dict[str, object] | None = None
    if updates is None:
        plan = suggest_schematic_designator_fixes(timeout_seconds)
        raw_updates = plan.get("suggestions", [])
    else:
        raw_updates = updates

    if not isinstance(raw_updates, list):
        raise ValueError("updates must be a list of designator update objects")

    normalized = []
    for item in raw_updates:
        if not isinstance(item, dict):
            raise ValueError("each designator update must be an object")
        old_designator = str(item.get("old_designator", "")).strip()
        new_designator = str(item.get("new_designator", "")).strip()
        comment = str(item.get("comment", "")).strip()
        safe_to_apply = bool(item.get("safe_to_apply", True))
        if not old_designator or not new_designator:
            continue
        normalized.append(
            {
                "old_designator": old_designator,
                "new_designator": new_designator,
                "comment": comment,
                "safe_to_apply": safe_to_apply,
            }
        )

    return normalized, plan


def apply_schematic_designator_fixes(
    updates: object | None,
    confirm: bool,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_updates, plan = normalize_designator_updates(updates, timeout_seconds)
    unsafe_updates = [item for item in normalized_updates if not item.get("safe_to_apply", True)]
    preview = {
        "source": "schematic",
        "operation": "apply_schematic_designator_fixes",
        "confirm_required": True,
        "confirmed": confirm,
        "update_count": len(normalized_updates),
        "updates": normalized_updates,
        "unsafe_updates": unsafe_updates,
        "plan": plan,
        "notes": [
            "This operation changes the open schematic in Altium but does not save the project file automatically.",
            "Run again with confirm=true only after reviewing the proposed updates.",
        ],
    }

    if not confirm:
        return preview
    if not normalized_updates:
        raise ValueError("No designator updates to apply")
    if unsafe_updates:
        raise ValueError("Refusing to apply unsafe/ambiguous designator updates")

    rows = [
        [item["old_designator"], item["new_designator"], item.get("comment", "")]
        for item in normalized_updates
    ]
    updates_file = write_operation_tsv("schematic_designators", rows)
    response = send_bridge_command(
        "apply_sch_designator_updates",
        {"updates_file": str(updates_file), "timeout_seconds": timeout_seconds},
        timeout_seconds,
    )
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error while applying designators: {json.dumps(response, ensure_ascii=False)}")
    result = response.get("result", {})
    return {
        **preview,
        "confirmed": True,
        "updates_file": str(updates_file),
        "result": result,
    }


def normalize_parameter_updates(updates: object | None) -> list[dict[str, str]]:
    if not isinstance(updates, list):
        raise ValueError("updates must be a list of parameter update objects")

    normalized = []
    for item in updates:
        if not isinstance(item, dict):
            raise ValueError("each parameter update must be an object")
        designator = str(item.get("designator", "")).strip()
        parameter = str(item.get("parameter", item.get("parameter_name", ""))).strip()
        value = field_value_as_text(item.get("value", item.get("parameter_value", "")))
        if not designator or not parameter:
            raise ValueError("each parameter update requires designator and parameter")
        normalized.append({"designator": designator, "parameter": parameter, "value": value})
    return normalized


def update_schematic_parameters(
    updates: object | None,
    confirm: bool,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_updates = normalize_parameter_updates(updates)
    preview = {
        "source": "schematic",
        "operation": "update_schematic_parameters",
        "confirm_required": True,
        "confirmed": confirm,
        "update_count": len(normalized_updates),
        "updates": normalized_updates,
        "notes": [
            "This operation updates existing schematic parameters by designator/name and does not create missing parameters.",
            "It changes the open schematic in Altium but does not save the project file automatically.",
        ],
    }

    if not confirm:
        return preview

    rows = [[item["designator"], item["parameter"], item["value"]] for item in normalized_updates]
    updates_file = write_operation_tsv("schematic_parameters", rows)
    response = send_bridge_command(
        "apply_sch_parameter_updates",
        {"updates_file": str(updates_file), "timeout_seconds": timeout_seconds},
        timeout_seconds,
    )
    if not response.get("ok", False):
        raise RuntimeError(f"Altium returned an error while updating parameters: {json.dumps(response, ensure_ascii=False)}")
    result = response.get("result", {})
    return {
        **preview,
        "confirmed": True,
        "updates_file": str(updates_file),
        "result": result,
    }


def list_pcb_nets(timeout_seconds: float) -> dict[str, object]:
    response = send_bridge_command("list_pcb_nets", {"timeout_seconds": timeout_seconds}, timeout_seconds)
    nets = extract_nets(response)
    return {
        "source": "pcb",
        "total_nets": len(nets),
        "nets": sorted(nets, key=net_sort_key),
    }


def check_pcb_nets(timeout_seconds: float) -> dict[str, object]:
    net_list = list_pcb_nets(timeout_seconds)
    nets = [net for net in net_list.get("nets", []) if isinstance(net, dict)]

    blank_names = []
    zero_pin_nets = []
    single_pin_nets = []
    connectively_invalid = []

    for net in nets:
        pin_count = net_int(net, "pin_count")
        if not net_name(net):
            blank_names.append(net)
        if pin_count == 0:
            zero_pin_nets.append(net)
        elif pin_count == 1:
            single_pin_nets.append(net)
        if net_bool(net, "connectively_invalid"):
            connectively_invalid.append(net)

    all_nets_connectivity_flagged = len(nets) > 0 and len(connectively_invalid) == len(nets)
    connectivity_issue_nets = [] if all_nets_connectivity_flagged else connectively_invalid
    issue_count = len(blank_names) + len(zero_pin_nets) + len(connectivity_issue_nets)
    review_count = len(single_pin_nets)
    if all_nets_connectivity_flagged:
        review_count += len(connectively_invalid)

    if not connectively_invalid:
        connectivity_flag_scope = "none"
    elif all_nets_connectivity_flagged:
        connectivity_flag_scope = "all_nets"
    else:
        connectivity_flag_scope = "some_nets"

    sorted_connectively_invalid = sorted(connectively_invalid, key=net_sort_key)
    return {
        "source": "pcb",
        "status": "ok" if issue_count == 0 and review_count == 0 else "needs_attention",
        "total_nets": len(nets),
        "issue_count": issue_count,
        "review_count": review_count,
        "connectivity_flag_count": len(connectively_invalid),
        "connectivity_flag_scope": connectivity_flag_scope,
        "blank_names": sorted(blank_names, key=net_sort_key),
        "zero_pin_nets": sorted(zero_pin_nets, key=net_sort_key),
        "single_pin_nets": sorted(single_pin_nets, key=net_sort_key),
        "connectively_invalid": [] if all_nets_connectivity_flagged else sorted_connectively_invalid,
        "connectivity_flag_sample": sorted_connectively_invalid[:10] if all_nets_connectivity_flagged else [],
        "notes": [
            "single_pin_nets are counted as review items, because some designs intentionally use one-pin nets.",
            "connectively_invalid comes from the PCB net object's connectivity flag.",
            "When all nets have connectively_invalid=true, it is treated as a global connectivity-cache/flag review item rather than 1 issue per net.",
        ],
    }


def generate_simple_bom_from_components(
    components: list[dict[str, object]],
    source: str,
) -> dict[str, object]:
    grouped: dict[str, list[str]] = {}
    unannotated: list[dict[str, object]] = []

    for component in components:
        designator = component_designator(component)
        comment = component_comment(component) or "(blank)"
        grouped.setdefault(comment, []).append(designator or "(blank)")
        if "?" in designator:
            unannotated.append(component)

    items = []
    for comment, designators in grouped.items():
        sorted_designators = sorted(designators, key=designator_sort_key)
        items.append(
            {
                "comment": comment,
                "quantity": len(sorted_designators),
                "designators": sorted_designators,
            }
        )

    items.sort(key=lambda item: (str(item["comment"]).upper(), str(item["designators"][0]).upper()))

    return {
        "source": source,
        "status": "ok" if not unannotated else "needs_attention",
        "total_components": len(components),
        "line_items": len(items),
        "items": items,
        "unannotated": sorted(unannotated, key=component_sort_key),
    }


def generate_simple_bom(source: str, timeout_seconds: float) -> dict[str, object]:
    normalized_source = source.strip().lower()
    if normalized_source in ("sch", "schematic"):
        command = "list_sch_components"
        document_type = "schematic"
    elif normalized_source == "pcb":
        command = "list_pcb_components"
        document_type = "pcb"
    else:
        raise ValueError("source must be 'schematic' or 'pcb'")

    response = send_bridge_command(command, {"timeout_seconds": timeout_seconds}, timeout_seconds)
    components = extract_components(response, document_type)
    return generate_simple_bom_from_components(components, document_type)


def generate_bom_with_fields_from_components(
    components: list[dict[str, object]],
    source: str,
    include_fields: list[str],
) -> dict[str, object]:
    grouped: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    unannotated: list[dict[str, object]] = []

    for component in components:
        designator = component_designator(component)
        comment = component_comment(component) or "(blank)"
        field_values = tuple(component_field(component, field) for field in include_fields)
        grouped.setdefault((comment, field_values), []).append(designator or "(blank)")
        if "?" in designator:
            unannotated.append(component)

    items = []
    for (comment, field_values), designators in grouped.items():
        sorted_designators = sorted(designators, key=designator_sort_key)
        fields = {
            include_fields[index]: field_values[index]
            for index in range(len(include_fields))
        }
        items.append(
            {
                "comment": comment,
                "quantity": len(sorted_designators),
                "designators": sorted_designators,
                "fields": fields,
            }
        )

    items.sort(key=lambda item: (str(item["comment"]).upper(), str(item["designators"][0]).upper()))

    return {
        "source": source,
        "status": "ok" if not unannotated else "needs_attention",
        "total_components": len(components),
        "line_items": len(items),
        "include_fields": include_fields,
        "items": items,
        "unannotated": sorted(unannotated, key=component_sort_key),
    }


def generate_bom_with_fields(
    source: str,
    include_fields: object | None,
    timeout_seconds: float,
) -> dict[str, object]:
    fields = normalize_include_fields(include_fields)
    document_type, components = read_components_for_source(source, timeout_seconds)
    return generate_bom_with_fields_from_components(components, document_type, fields)


def read_components_for_source(source: str, timeout_seconds: float) -> tuple[str, list[dict[str, object]]]:
    normalized_source = source.strip().lower()
    if normalized_source in ("sch", "schematic"):
        command = "list_sch_components"
        document_type = "schematic"
    elif normalized_source == "pcb":
        command = "list_pcb_components"
        document_type = "pcb"
    else:
        raise ValueError("source must be 'schematic' or 'pcb'")

    response = send_bridge_command(command, {"timeout_seconds": timeout_seconds}, timeout_seconds)
    return document_type, extract_components(response, document_type)


def generate_component_list(source: str, timeout_seconds: float) -> dict[str, object]:
    normalized_source = source.strip().lower()
    if normalized_source == "both":
        rows: list[dict[str, object]] = []
        for source_name in ("schematic", "pcb"):
            document_type, components = read_components_for_source(source_name, timeout_seconds)
            rows.extend({**component, "source": document_type} for component in components)
        return {
            "source": "both",
            "total_components": len(rows),
            "available_fields": component_available_fields(rows),
            "components": sorted(rows, key=lambda row: (str(row.get("source", "")),) + component_sort_key(row)),
        }

    document_type, components = read_components_for_source(normalized_source, timeout_seconds)
    rows = [{**component, "source": document_type} for component in components]
    return {
        "source": document_type,
        "total_components": len(rows),
        "available_fields": component_available_fields(rows),
        "components": sorted(rows, key=component_sort_key),
    }


def generate_component_parameter_list(source: str, timeout_seconds: float) -> dict[str, object]:
    normalized_source = source.strip().lower()
    source_names = ["schematic", "pcb"] if normalized_source == "both" else [normalized_source]
    rows: list[dict[str, object]] = []
    component_count = 0

    for source_name in source_names:
        document_type, components = read_components_for_source(source_name, timeout_seconds)
        component_count += len(components)
        for component in components:
            for parameter in component_parameters(component):
                name = str(parameter.get("name", "")).strip()
                if not name:
                    continue
                rows.append(
                    {
                        "source": document_type,
                        "designator": component_designator(component),
                        "comment": component_comment(component),
                        "parameter_name": name,
                        "parameter_value": field_value_as_text(parameter.get("value", "")),
                    }
                )

    if normalized_source not in ("both", "schematic", "sch", "pcb"):
        raise ValueError("source must be 'both', 'schematic', or 'pcb'")

    source_label = "both" if normalized_source == "both" else ("schematic" if normalized_source == "sch" else normalized_source)
    rows.sort(
        key=lambda row: (
            str(row.get("source", "")),
            str(row.get("designator", "")).upper(),
            str(row.get("parameter_name", "")).upper(),
        )
    )
    return {
        "source": source_label,
        "total_components": component_count,
        "parameter_count": len(rows),
        "parameters": rows,
    }


def check_designators_for_components(
    source: str,
    components: list[dict[str, object]],
) -> dict[str, object]:
    blank_designators = []
    unannotated = []
    grouped: dict[str, list[dict[str, object]]] = {}

    for component in components:
        designator = component_designator(component)
        if not designator:
            blank_designators.append(component)
            continue
        if "?" in designator:
            unannotated.append(component)
        grouped.setdefault(designator.upper(), []).append(component)

    duplicates = []
    for designator_key, matching_components in sorted(grouped.items()):
        if len(matching_components) <= 1:
            continue
        duplicates.append(
            {
                "designator": component_designator(matching_components[0]) or designator_key,
                "count": len(matching_components),
                "components": sorted(matching_components, key=component_sort_key),
            }
        )

    issue_count = len(blank_designators) + len(unannotated) + len(duplicates)

    return {
        "source": source,
        "status": "ok" if issue_count == 0 else "needs_attention",
        "total_components": len(components),
        "blank_designators": sorted(blank_designators, key=component_sort_key),
        "unannotated": sorted(unannotated, key=component_sort_key),
        "duplicates": duplicates,
        "issue_count": issue_count,
    }


def check_component_designators(source: str, timeout_seconds: float) -> dict[str, object]:
    normalized_source = source.strip().lower()
    if normalized_source == "both":
        schematic_type, schematic_components = read_components_for_source("schematic", timeout_seconds)
        pcb_type, pcb_components = read_components_for_source("pcb", timeout_seconds)
        schematic_result = check_designators_for_components(schematic_type, schematic_components)
        pcb_result = check_designators_for_components(pcb_type, pcb_components)
        issue_count = int(schematic_result["issue_count"]) + int(pcb_result["issue_count"])
        return {
            "source": "both",
            "status": "ok" if issue_count == 0 else "needs_attention",
            "issue_count": issue_count,
            "results": {
                "schematic": schematic_result,
                "pcb": pcb_result,
            },
        }

    document_type, components = read_components_for_source(normalized_source, timeout_seconds)
    return check_designators_for_components(document_type, components)


def check_fields_for_components(
    source: str,
    components: list[dict[str, object]],
    required_fields: list[str],
) -> dict[str, object]:
    missing = []
    issue_count = 0

    for field in required_fields:
        missing_components = [
            component
            for component in components
            if not component_field(component, field)
        ]
        if not missing_components:
            continue

        issue_count += len(missing_components)
        missing.append(
            {
                "field": field,
                "count": len(missing_components),
                "components": sorted(missing_components, key=component_sort_key),
            }
        )

    return {
        "source": source,
        "status": "ok" if issue_count == 0 else "needs_attention",
        "total_components": len(components),
        "required_fields": required_fields,
        "available_fields": component_available_fields(components),
        "missing": missing,
        "issue_count": issue_count,
    }


def check_component_fields(
    source: str,
    required_fields: object | None,
    timeout_seconds: float,
) -> dict[str, object]:
    fields = normalize_required_fields(required_fields)
    normalized_source = source.strip().lower()
    if normalized_source == "both":
        schematic_type, schematic_components = read_components_for_source("schematic", timeout_seconds)
        pcb_type, pcb_components = read_components_for_source("pcb", timeout_seconds)
        schematic_result = check_fields_for_components(schematic_type, schematic_components, fields)
        pcb_result = check_fields_for_components(pcb_type, pcb_components, fields)
        issue_count = int(schematic_result["issue_count"]) + int(pcb_result["issue_count"])
        return {
            "source": "both",
            "status": "ok" if issue_count == 0 else "needs_attention",
            "required_fields": fields,
            "issue_count": issue_count,
            "results": {
                "schematic": schematic_result,
                "pcb": pcb_result,
            },
        }

    document_type, components = read_components_for_source(normalized_source, timeout_seconds)
    return check_fields_for_components(document_type, components, fields)


def report_status(report: object) -> str:
    if not isinstance(report, dict):
        return "error"
    status = str(report.get("status", "")).strip().lower()
    if status in ("ok", "ready"):
        return "ok"
    if status in ("needs_attention", "needs_outjob", "needs_confirmation"):
        return "needs_attention"
    if status:
        return status
    return "ok"


def report_int(report: object, field: str) -> int:
    if not isinstance(report, dict):
        return 0
    value = report.get(field, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def report_issue_count(report: object) -> int:
    if not isinstance(report, dict):
        return 1
    if "issue_count" in report:
        return report_int(report, "issue_count")
    if report.get("status") == "needs_outjob":
        return 1
    unannotated = report.get("unannotated")
    if isinstance(unannotated, list):
        return len(unannotated)
    return 0 if report_status(report) == "ok" else 1


def health_step(name: str, label: str, runner: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        report = runner()
    except Exception as exc:
        return {
            "name": name,
            "label": label,
            "status": "error",
            "issue_count": 1,
            "review_count": 0,
            "error": str(exc),
        }

    return {
        "name": name,
        "label": label,
        "status": report_status(report),
        "issue_count": report_issue_count(report),
        "review_count": report_int(report, "review_count"),
        "report": report,
    }


def project_health_check(
    required_fields: object | None,
    include_bom: bool,
    include_output_jobs: bool,
    timeout_seconds: float,
) -> dict[str, object]:
    fields = normalize_required_fields(required_fields)
    steps = [
        health_step(
            "designators",
            "Component designators",
            lambda: check_component_designators("both", timeout_seconds),
        ),
        health_step(
            "sch_pcb_comparison",
            "Schematic vs PCB component comparison",
            lambda: compare_sch_pcb_components(timeout_seconds),
        ),
        health_step(
            "component_fields",
            "Required component fields",
            lambda: check_component_fields("both", fields, timeout_seconds),
        ),
        health_step(
            "pcb_nets",
            "PCB net sanity",
            lambda: check_pcb_nets(timeout_seconds),
        ),
    ]

    if include_bom:
        steps.append(
            health_step(
                "schematic_bom",
                "Schematic BOM preview",
                lambda: generate_simple_bom("schematic", timeout_seconds),
            )
        )

    if include_output_jobs:
        steps.append(
            health_step(
                "output_jobs",
                "OutJob readiness",
                lambda: prepare_output_generation_plan(timeout_seconds),
            )
        )

    error_count = sum(1 for step in steps if step.get("status") == "error")
    issue_count = sum(report_int(step, "issue_count") for step in steps)
    review_count = sum(report_int(step, "review_count") for step in steps)
    if error_count:
        overall_status = "error"
    elif issue_count or review_count:
        overall_status = "needs_attention"
    else:
        overall_status = "ok"

    checks = {str(step["name"]): step for step in steps}
    return {
        "source": "project",
        "status": overall_status,
        "generated_at": utc_now(),
        "summary": {
            "check_count": len(steps),
            "error_count": error_count,
            "issue_count": issue_count,
            "review_count": review_count,
            "required_fields": fields,
        },
        "checks": checks,
        "next_actions": [
            "Run altium_run_project_validation with confirm=true to trigger Altium project validation/ERC.",
            "Run altium_open_pcb_drc_dialog with confirm=true to open the PCB Design Rule Checker dialog.",
            "Run altium_run_active_output_container with confirm=true after selecting the intended OutJob output container.",
        ],
        "notes": [
            "This health check is read-only and reuses existing MCP checks.",
            "Project validation, PCB DRC, and production output generation are separate confirmed actions because they change Altium UI state or write output files.",
        ],
    }


def write_project_health_check_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["check", "label", "status", "issue_count", "review_count", "error", "detail"])

        summary = report.get("summary", {})
        writer.writerow(
            [
                "summary",
                "Project health",
                report.get("status", ""),
                report_int(summary, "issue_count"),
                report_int(summary, "review_count"),
                "",
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            ]
        )

        checks = report.get("checks", {})
        if isinstance(checks, dict):
            for name, step in checks.items():
                if not isinstance(step, dict):
                    continue
                detail = step.get("report", step.get("error", ""))
                writer.writerow(
                    [
                        name,
                        step.get("label", ""),
                        step.get("status", ""),
                        step.get("issue_count", ""),
                        step.get("review_count", ""),
                        step.get("error", ""),
                        json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                    ]
                )
    os.replace(tmp_path, path)


def export_project_health_report(
    required_fields: object | None,
    include_bom: bool,
    include_output_jobs: bool,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = project_health_check(required_fields, include_bom, include_output_jobs, timeout_seconds)
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("health_check", "project", normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_project_health_check_csv(output_path, report)

    summary = report.get("summary", {})
    return {
        "ok": True,
        "source": "project",
        "format": normalized_format,
        "path": str(output_path),
        "status": report.get("status"),
        "summary": summary,
    }


def export_filename(source: str, file_format: str, filename: str | None) -> str:
    suffix = "." + file_format
    if filename:
        name = Path(filename).name.strip()
        if not name:
            raise ValueError("filename must not be empty")
        path = Path(name)
        if path.suffix.lower() != suffix:
            name = path.stem + suffix
        return name

    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"altium_{source}_bom_{timestamp}{suffix}"


def write_simple_bom_csv(path: Path, bom: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["comment", "quantity", "designators"])
        for item in bom.get("items", []):
            if not isinstance(item, dict):
                continue
            designators = item.get("designators", [])
            if isinstance(designators, list):
                designator_text = " ".join(str(designator) for designator in designators)
            else:
                designator_text = str(designators)
            writer.writerow([item.get("comment", ""), item.get("quantity", ""), designator_text])
    os.replace(tmp_path, path)


def write_bom_with_fields_csv(path: Path, bom: dict[str, object]) -> None:
    include_fields = bom.get("include_fields", [])
    fields = [str(field) for field in include_fields] if isinstance(include_fields, list) else []

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["comment", "quantity", "designators", *fields])
        for item in bom.get("items", []):
            if not isinstance(item, dict):
                continue
            designators = item.get("designators", [])
            if isinstance(designators, list):
                designator_text = " ".join(str(designator) for designator in designators)
            else:
                designator_text = str(designators)

            item_fields = item.get("fields", {})
            field_values = []
            for field in fields:
                if isinstance(item_fields, dict):
                    field_values.append(field_value_as_text(item_fields.get(field, "")))
                else:
                    field_values.append("")
            writer.writerow([item.get("comment", ""), item.get("quantity", ""), designator_text, *field_values])
    os.replace(tmp_path, path)


def export_simple_bom(
    source: str,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    bom = generate_simple_bom(source, timeout_seconds)
    source_name = str(bom.get("source", source))
    ensure_exports_dir()
    output_path = EXPORTS_DIR / export_filename(source_name, normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, bom)
    else:
        write_simple_bom_csv(output_path, bom)

    return {
        "ok": True,
        "source": source_name,
        "format": normalized_format,
        "path": str(output_path),
        "status": bom.get("status"),
        "total_components": bom.get("total_components"),
        "line_items": bom.get("line_items"),
        "unannotated": bom.get("unannotated", []),
    }


def export_bom_with_fields(
    source: str,
    include_fields: object | None,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    bom = generate_bom_with_fields(source, include_fields, timeout_seconds)
    source_name = str(bom.get("source", source))
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("bom_with_fields", source_name, normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, bom)
    else:
        write_bom_with_fields_csv(output_path, bom)

    return {
        "ok": True,
        "source": source_name,
        "format": normalized_format,
        "path": str(output_path),
        "status": bom.get("status"),
        "total_components": bom.get("total_components"),
        "line_items": bom.get("line_items"),
        "include_fields": bom.get("include_fields", []),
        "unannotated": bom.get("unannotated", []),
    }


def component_list_columns(components: list[dict[str, object]]) -> list[str]:
    fields = component_available_fields(components)
    ordered = ["source", "designator", "comment", "footprint"]
    columns: list[str] = []
    for field in ordered + fields:
        if field == "parameters":
            continue
        if field in columns:
            continue
        if field in fields or field == "source":
            columns.append(field)
    return columns


def write_component_list_csv(path: Path, component_list: dict[str, object]) -> None:
    components = component_list.get("components", [])
    rows = [component for component in components if isinstance(component, dict)] if isinstance(components, list) else []
    columns = component_list_columns(rows)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([component_field(row, column) for column in columns])
    os.replace(tmp_path, path)


def export_component_list(
    source: str,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    component_list = generate_component_list(source, timeout_seconds)
    source_name = str(component_list.get("source", source))
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("component_list", source_name, normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, component_list)
    else:
        write_component_list_csv(output_path, component_list)

    return {
        "ok": True,
        "source": source_name,
        "format": normalized_format,
        "path": str(output_path),
        "total_components": component_list.get("total_components"),
        "available_fields": component_list.get("available_fields", []),
    }


def write_component_parameters_csv(path: Path, report: dict[str, object]) -> None:
    rows = report.get("parameters", [])
    parameters = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "designator", "comment", "parameter_name", "parameter_value"])
        for row in parameters:
            writer.writerow(
                [
                    row.get("source", ""),
                    row.get("designator", ""),
                    row.get("comment", ""),
                    row.get("parameter_name", ""),
                    row.get("parameter_value", ""),
                ]
            )
    os.replace(tmp_path, path)


def export_component_parameters(
    source: str,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = generate_component_parameter_list(source, timeout_seconds)
    source_name = str(report.get("source", source))
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("component_parameters", source_name, normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_component_parameters_csv(output_path, report)

    return {
        "ok": True,
        "source": source_name,
        "format": normalized_format,
        "path": str(output_path),
        "total_components": report.get("total_components"),
        "parameter_count": report.get("parameter_count"),
    }


def report_filename(report_name: str, source: str, file_format: str, filename: str | None) -> str:
    suffix = "." + file_format
    if filename:
        name = Path(filename).name.strip()
        if not name:
            raise ValueError("filename must not be empty")
        path = Path(name)
        if path.suffix.lower() != suffix:
            name = path.stem + suffix
        return name

    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"altium_{source}_{report_name}_{timestamp}{suffix}"


def designator_report_sources(report: dict[str, object]) -> list[dict[str, object]]:
    if report.get("source") == "both":
        results = report.get("results", {})
        if not isinstance(results, dict):
            return []
        return [item for item in results.values() if isinstance(item, dict)]
    return [report]


def write_designator_report_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "issue_type", "designator", "comment", "count", "status", "total_components"])

        for source_report in designator_report_sources(report):
            source = source_report.get("source", "")
            status = source_report.get("status", "")
            total_components = source_report.get("total_components", "")
            writer.writerow([source, "summary", "", "", source_report.get("issue_count", 0), status, total_components])

            for issue_type in ("blank_designators", "unannotated"):
                issues = source_report.get(issue_type, [])
                if not isinstance(issues, list):
                    continue
                for component in issues:
                    if not isinstance(component, dict):
                        continue
                    writer.writerow(
                        [
                            source,
                            issue_type,
                            component_designator(component),
                            component_comment(component),
                            1,
                            status,
                            total_components,
                        ]
                    )

            duplicates = source_report.get("duplicates", [])
            if not isinstance(duplicates, list):
                continue
            for duplicate in duplicates:
                if not isinstance(duplicate, dict):
                    continue
                components = duplicate.get("components", [])
                comments = []
                if isinstance(components, list):
                    comments = [
                        component_comment(component)
                        for component in components
                        if isinstance(component, dict) and component_comment(component)
                    ]
                writer.writerow(
                    [
                        source,
                        "duplicates",
                        duplicate.get("designator", ""),
                        " | ".join(comments),
                        duplicate.get("count", ""),
                        status,
                        total_components,
                    ]
                )
    os.replace(tmp_path, path)


def export_designator_report(
    source: str,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = check_component_designators(source, timeout_seconds)
    source_name = str(report.get("source", source))
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("designators", source_name, normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_designator_report_csv(output_path, report)

    return {
        "ok": True,
        "source": source_name,
        "format": normalized_format,
        "path": str(output_path),
        "status": report.get("status"),
        "issue_count": report.get("issue_count"),
    }


def write_comparison_report_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "issue_type",
                "designator",
                "schematic_comment",
                "pcb_comment",
                "source",
                "comment",
                "status",
                "issue_count",
            ]
        )

        status = report.get("status", "")
        issue_count = report.get("issue_count", "")
        counts = report.get("counts", {})
        writer.writerow(
            [
                "summary",
                "",
                "",
                "",
                "",
                json.dumps(counts, ensure_ascii=False, separators=(",", ":")),
                status,
                issue_count,
            ]
        )

        for issue_type, source_name in (
            ("only_in_schematic", "schematic"),
            ("only_in_pcb", "pcb"),
        ):
            components = report.get(issue_type, [])
            if not isinstance(components, list):
                continue
            for component in components:
                if not isinstance(component, dict):
                    continue
                writer.writerow(
                    [
                        issue_type,
                        component_designator(component),
                        component_comment(component) if source_name == "schematic" else "",
                        component_comment(component) if source_name == "pcb" else "",
                        source_name,
                        component_comment(component),
                        status,
                        issue_count,
                    ]
                )

        unannotated = report.get("unannotated", {})
        if isinstance(unannotated, dict):
            for source_name in ("schematic", "pcb"):
                components = unannotated.get(source_name, [])
                if not isinstance(components, list):
                    continue
                for component in components:
                    if not isinstance(component, dict):
                        continue
                    writer.writerow(
                        [
                            f"unannotated_{source_name}",
                            component_designator(component),
                            component_comment(component) if source_name == "schematic" else "",
                            component_comment(component) if source_name == "pcb" else "",
                            source_name,
                            component_comment(component),
                            status,
                            issue_count,
                        ]
                    )

        mismatches = report.get("comment_mismatches", [])
        if isinstance(mismatches, list):
            for mismatch in mismatches:
                if not isinstance(mismatch, dict):
                    continue
                writer.writerow(
                    [
                        "comment_mismatch",
                        mismatch.get("designator", ""),
                        mismatch.get("schematic_comment", ""),
                        mismatch.get("pcb_comment", ""),
                        "both",
                        "",
                        status,
                        issue_count,
                    ]
                )
    os.replace(tmp_path, path)


def write_pcb_net_report_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "issue_type",
                "name",
                "pin_count",
                "via_count",
                "routed_length",
                "connects_visible",
                "connectively_invalid",
                "status",
                "issue_count",
                "review_count",
                "connectivity_flag_scope",
            ]
        )

        status = report.get("status", "")
        issue_count = report.get("issue_count", "")
        review_count = report.get("review_count", "")
        connectivity_flag_scope = report.get("connectivity_flag_scope", "")
        writer.writerow(
            [
                "summary",
                "",
                report.get("total_nets", ""),
                "",
                "",
                "",
                "",
                status,
                issue_count,
                review_count,
                connectivity_flag_scope,
            ]
        )

        for issue_type in (
            "blank_names",
            "zero_pin_nets",
            "single_pin_nets",
            "connectively_invalid",
            "connectivity_flag_sample",
        ):
            nets = report.get(issue_type, [])
            if not isinstance(nets, list):
                continue
            for net in nets:
                if not isinstance(net, dict):
                    continue
                writer.writerow(
                    [
                        issue_type,
                        net_name(net),
                        net_int(net, "pin_count"),
                        net_int(net, "via_count"),
                        net_int(net, "routed_length"),
                        net_bool(net, "connects_visible"),
                        net_bool(net, "connectively_invalid"),
                        status,
                        issue_count,
                        review_count,
                        connectivity_flag_scope,
                    ]
                )
    os.replace(tmp_path, path)


def write_pcb_net_list_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "pin_count", "via_count", "routed_length", "connects_visible", "connectively_invalid"])
        nets = report.get("nets", [])
        if isinstance(nets, list):
            for net in nets:
                if not isinstance(net, dict):
                    continue
                writer.writerow(
                    [
                        net_name(net),
                        net_int(net, "pin_count"),
                        net_int(net, "via_count"),
                        net_int(net, "routed_length"),
                        net_bool(net, "connects_visible"),
                        net_bool(net, "connectively_invalid"),
                    ]
                )
    os.replace(tmp_path, path)


def write_output_jobs_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["project_file_name", "scope", "kind", "file_name", "full_path", "loaded"])
        documents = report.get("output_documents", [])
        if isinstance(documents, list):
            for document in documents:
                if not isinstance(document, dict):
                    continue
                writer.writerow(
                    [
                        document.get("project_file_name", ""),
                        document.get("scope", ""),
                        document.get("kind", ""),
                        document.get("file_name", ""),
                        document.get("full_path", ""),
                        document.get("loaded", ""),
                    ]
                )
    os.replace(tmp_path, path)


def export_output_jobs_report(
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = find_output_jobs(timeout_seconds)
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("output_jobs", "workspace", normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_output_jobs_csv(output_path, report)

    return {
        "ok": True,
        "source": "workspace",
        "format": normalized_format,
        "path": str(output_path),
        "output_document_count": report.get("output_document_count"),
        "outjob_count": report.get("outjob_count"),
    }


def export_pcb_net_report(
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = check_pcb_nets(timeout_seconds)
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("pcb_nets", "pcb", normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_pcb_net_report_csv(output_path, report)

    return {
        "ok": True,
        "source": "pcb",
        "format": normalized_format,
        "path": str(output_path),
        "status": report.get("status"),
        "total_nets": report.get("total_nets"),
        "issue_count": report.get("issue_count"),
    }


def export_pcb_net_list(
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = list_pcb_nets(timeout_seconds)
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("pcb_net_list", "pcb", normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_pcb_net_list_csv(output_path, report)

    return {
        "ok": True,
        "source": "pcb",
        "format": normalized_format,
        "path": str(output_path),
        "total_nets": report.get("total_nets"),
    }


def export_sch_pcb_comparison_report(
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = compare_sch_pcb_components(timeout_seconds)
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("comparison", "sch_pcb", normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_comparison_report_csv(output_path, report)

    return {
        "ok": True,
        "source": "sch_pcb",
        "format": normalized_format,
        "path": str(output_path),
        "status": report.get("status"),
        "issue_count": report.get("issue_count"),
        "counts": report.get("counts", {}),
    }


def component_field_report_sources(report: dict[str, object]) -> list[dict[str, object]]:
    if report.get("source") == "both":
        results = report.get("results", {})
        if not isinstance(results, dict):
            return []
        return [item for item in results.values() if isinstance(item, dict)]
    return [report]


def write_component_field_report_csv(path: Path, report: dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source",
                "issue_type",
                "field",
                "designator",
                "comment",
                "value",
                "status",
                "total_components",
            ]
        )

        for source_report in component_field_report_sources(report):
            source = source_report.get("source", "")
            status = source_report.get("status", "")
            total_components = source_report.get("total_components", "")
            required_fields = source_report.get("required_fields", [])
            if isinstance(required_fields, list):
                required_text = ", ".join(str(field) for field in required_fields)
            else:
                required_text = str(required_fields)

            writer.writerow(
                [
                    source,
                    "summary",
                    required_text,
                    "",
                    "",
                    source_report.get("issue_count", 0),
                    status,
                    total_components,
                ]
            )

            missing = source_report.get("missing", [])
            if not isinstance(missing, list):
                continue

            for field_group in missing:
                if not isinstance(field_group, dict):
                    continue
                field = str(field_group.get("field", ""))
                components = field_group.get("components", [])
                if not isinstance(components, list):
                    continue

                for component in components:
                    if not isinstance(component, dict):
                        continue
                    writer.writerow(
                        [
                            source,
                            "missing_field",
                            field,
                            component_designator(component),
                            component_comment(component),
                            component_field(component, field),
                            status,
                            total_components,
                        ]
                    )
    os.replace(tmp_path, path)


def export_component_field_report(
    source: str,
    required_fields: object | None,
    file_format: str,
    filename: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    normalized_format = file_format.strip().lower()
    if normalized_format not in ("csv", "json"):
        raise ValueError("format must be 'csv' or 'json'")

    report = check_component_fields(source, required_fields, timeout_seconds)
    source_name = str(report.get("source", source))
    ensure_exports_dir()
    output_path = EXPORTS_DIR / report_filename("component_fields", source_name, normalized_format, filename)

    if normalized_format == "json":
        write_json_atomic(output_path, report)
    else:
        write_component_field_report_csv(output_path, report)

    return {
        "ok": True,
        "source": source_name,
        "format": normalized_format,
        "path": str(output_path),
        "status": report.get("status"),
        "issue_count": report.get("issue_count"),
        "required_fields": report.get("required_fields"),
    }


TOOLS: list[dict[str, object]] = [
    {
        "name": "altium_bridge_status",
        "description": "Report local bridge files and recent Altium heartbeat state. Does not call Altium.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "altium_ping",
        "description": "Ask the running Altium bridge to respond with a simple ping result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for Altium to answer.",
                    "default": 10,
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_get_active_document",
        "description": "Return the active document reported by the running Altium bridge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "default": 10},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_list_workspace_documents",
        "description": "List projects and documents visible to the running Altium workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_find_output_jobs",
        "description": "Find OutJob/BOM output documents visible in the Altium workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_output_jobs_report",
        "description": "Export visible OutJob/BOM output document list to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_prepare_output_generation",
        "description": "Prepare a safe production-output generation plan by checking for visible OutJob documents. Does not run outputs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_project_health_check",
        "description": "Run a read-only project health check across designators, SCH/PCB consistency, required fields, PCB nets, BOM preview, and OutJob readiness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "required_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Component fields or schematic parameter names that must be present and non-blank.",
                    "default": ["footprint"],
                },
                "include_bom": {
                    "type": "boolean",
                    "description": "Include a read-only schematic BOM preview check.",
                    "default": True,
                },
                "include_output_jobs": {
                    "type": "boolean",
                    "description": "Include an OutJob readiness check.",
                    "default": True,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_project_health_report",
        "description": "Export the read-only project health check report to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "required_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Component fields or schematic parameter names that must be present and non-blank.",
                    "default": ["footprint"],
                },
                "include_bom": {
                    "type": "boolean",
                    "description": "Include a read-only schematic BOM preview check.",
                    "default": True,
                },
                "include_output_jobs": {
                    "type": "boolean",
                    "description": "Include an OutJob readiness check.",
                    "default": True,
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_run_project_validation",
        "description": "Trigger Altium project validation/ERC through WorkspaceManager:Compile. Requires confirm=true and does not save the project automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to dispatch project validation in Altium.",
                    "default": False,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_open_pcb_drc_dialog",
        "description": "Open Altium's PCB Design Rule Checker dialog for the current PCB. Requires confirm=true; does not run the dialog automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to open the PCB DRC dialog in Altium.",
                    "default": False,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_run_active_output_container",
        "description": "Generate outputs for the currently selected output container in the active OutJob. Requires confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["folder_structure", "pdf"],
                    "description": "Output container mode selected in the active OutJob.",
                    "default": "folder_structure",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to generate production outputs from the active OutJob container.",
                    "default": False,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 120,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_list_pcb_components",
        "description": "List component designators/comments/footprints from the current PCB document in Altium.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_list_pcb_nets",
        "description": "List PCB nets with pin/via counts and Altium connectivity flags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 30,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_pcb_net_list",
        "description": "Export PCB net list with pin/via counts to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_check_pcb_nets",
        "description": "Check PCB nets for blank names, zero/single-pin nets, and Altium connectivity-invalid flags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_pcb_net_report",
        "description": "Export PCB net check report to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_list_sch_components",
        "description": "List component designators/comments from the current schematic document in Altium.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_compare_sch_pcb_components",
        "description": "Compare schematic and PCB component designators/comments reported by Altium.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_sch_pcb_comparison_report",
        "description": "Export the schematic-vs-PCB comparison report to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_generate_simple_bom",
        "description": "Generate a read-only simple BOM grouped by component comment/value from schematic or PCB data.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["schematic", "pcb"],
                    "description": "Which document source to read.",
                    "default": "schematic",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_simple_bom",
        "description": "Export a simple BOM grouped by component comment/value to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["schematic", "pcb"],
                    "description": "Which document source to read.",
                    "default": "schematic",
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_bom_with_fields",
        "description": "Export a BOM grouped by component comment plus selected fields or schematic parameters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["schematic", "pcb"],
                    "description": "Which document source to read.",
                    "default": "schematic",
                },
                "include_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra component fields or schematic parameter names to include in BOM grouping.",
                    "default": [],
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_component_list",
        "description": "Export a flat component list from schematic, PCB, or both to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["both", "schematic", "pcb"],
                    "description": "Which document source to export.",
                    "default": "both",
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_component_parameters",
        "description": "Export schematic/PCB component parameter rows to exports/ as CSV or JSON. Schematic parameters are reported when the Altium bridge can read them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["both", "schematic", "pcb"],
                    "description": "Which document source to export.",
                    "default": "schematic",
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_check_component_designators",
        "description": "Check schematic and/or PCB component designators for blanks, question-mark placeholders, and duplicates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["both", "schematic", "pcb"],
                    "description": "Which document source to check.",
                    "default": "both",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_suggest_schematic_designator_fixes",
        "description": "Suggest safe next-number fixes for unannotated schematic designators. Does not modify Altium.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for the Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_apply_schematic_designator_fixes",
        "description": "Apply reviewed schematic designator fixes to the open schematic. Requires confirm=true and does not save the project automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "description": "Optional explicit updates. If omitted, current safe suggestions are used.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_designator": {"type": "string"},
                            "new_designator": {"type": "string"},
                            "comment": {"type": "string"},
                            "safe_to_apply": {"type": "boolean"},
                        },
                        "required": ["old_designator", "new_designator"],
                        "additionalProperties": True,
                    },
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to write changes to the open schematic.",
                    "default": False,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_designator_report",
        "description": "Export a designator check report to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["both", "schematic", "pcb"],
                    "description": "Which document source to check.",
                    "default": "both",
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_check_component_fields",
        "description": "Check component fields or schematic parameter names for blank/missing values.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["both", "schematic", "pcb"],
                    "description": "Which document source to check.",
                    "default": "pcb",
                },
                "required_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Component fields or parameter names that must be present and non-blank.",
                    "default": ["footprint"],
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_export_component_field_report",
        "description": "Export a component field/parameter check report to exports/ as CSV or JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["both", "schematic", "pcb"],
                    "description": "Which document source to check.",
                    "default": "pcb",
                },
                "required_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Component fields or parameter names that must be present and non-blank.",
                    "default": ["footprint"],
                },
                "format": {
                    "type": "string",
                    "enum": ["csv", "json"],
                    "description": "Export file format.",
                    "default": "csv",
                },
                "filename": {
                    "type": "string",
                    "description": "Optional output filename. Directory components are ignored; file is always written under exports/.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_update_schematic_parameters",
        "description": "Update existing schematic component parameters by designator/name. Requires confirm=true and does not save the project automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "description": "Parameter updates to apply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "designator": {"type": "string"},
                            "parameter": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["designator", "parameter", "value"],
                        "additionalProperties": True,
                    },
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to write changes to the open schematic.",
                    "default": False,
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "How long to wait for each Altium bridge response.",
                    "default": 60,
                },
            },
            "required": ["updates"],
            "additionalProperties": False,
        },
    },
    {
        "name": "altium_stop_bridge",
        "description": "Create the stop-file consumed by StartMCPBridge so the Altium polling loop exits.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


BRIDGE_TOOL_COMMANDS = {
    "altium_ping": "ping",
    "altium_get_active_document": "get_active_document",
    "altium_list_workspace_documents": "list_workspace_documents",
    "altium_list_pcb_components": "list_pcb_components",
    "altium_list_pcb_nets": "list_pcb_nets",
    "altium_list_sch_components": "list_sch_components",
}


def call_tool(name: str, arguments: object | None) -> dict[str, object]:
    args = arguments if isinstance(arguments, dict) else {}

    if name == "altium_bridge_status":
        return text_result(bridge_status())

    if name == "altium_stop_bridge":
        ensure_shared_dir()
        STOP_FILE.write_text(f"requested_at={utc_now()}\n", encoding="utf-8")
        return text_result({"ok": True, "stop_file": str(STOP_FILE)})

    if name == "altium_compare_sch_pcb_components":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        try:
            return text_result(compare_sch_pcb_components(timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_sch_pcb_comparison_report":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_sch_pcb_comparison_report(file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_find_output_jobs":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        try:
            return text_result(find_output_jobs(timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_output_jobs_report":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_output_jobs_report(file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_prepare_output_generation":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        try:
            return text_result(prepare_output_generation_plan(timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_project_health_check":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        required_fields = args.get("required_fields")
        include_bom = argument_bool(args.get("include_bom"), True)
        include_output_jobs = argument_bool(args.get("include_output_jobs"), True)
        try:
            return text_result(project_health_check(required_fields, include_bom, include_output_jobs, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_project_health_report":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        required_fields = args.get("required_fields")
        include_bom = argument_bool(args.get("include_bom"), True)
        include_output_jobs = argument_bool(args.get("include_output_jobs"), True)
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(
                export_project_health_report(
                    required_fields,
                    include_bom,
                    include_output_jobs,
                    file_format,
                    filename,
                    timeout_seconds,
                )
            )
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_run_project_validation":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        confirm = argument_bool(args.get("confirm"), False)
        try:
            return text_result(run_project_validation(confirm, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_open_pcb_drc_dialog":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        confirm = argument_bool(args.get("confirm"), False)
        try:
            return text_result(open_pcb_drc_dialog(confirm, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_run_active_output_container":
        timeout_seconds = float(args.get("timeout_seconds", 120))
        mode = str(args.get("mode", "folder_structure"))
        confirm = argument_bool(args.get("confirm"), False)
        try:
            return text_result(run_active_output_container(mode, confirm, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_check_pcb_nets":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        try:
            return text_result(check_pcb_nets(timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_pcb_net_report":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_pcb_net_report(file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_pcb_net_list":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_pcb_net_list(file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_generate_simple_bom":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "schematic"))
        try:
            return text_result(generate_simple_bom(source, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_simple_bom":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "schematic"))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_simple_bom(source, file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_bom_with_fields":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "schematic"))
        include_fields = args.get("include_fields")
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_bom_with_fields(source, include_fields, file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_component_list":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "both"))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_component_list(source, file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_component_parameters":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "schematic"))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_component_parameters(source, file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_check_component_designators":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "both"))
        try:
            return text_result(check_component_designators(source, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_suggest_schematic_designator_fixes":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        try:
            return text_result(suggest_schematic_designator_fixes(timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_apply_schematic_designator_fixes":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        updates = args.get("updates")
        confirm = argument_bool(args.get("confirm"), False)
        try:
            return text_result(apply_schematic_designator_fixes(updates, confirm, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_designator_report":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "both"))
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(export_designator_report(source, file_format, filename, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_check_component_fields":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "pcb"))
        required_fields = args.get("required_fields")
        try:
            return text_result(check_component_fields(source, required_fields, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_export_component_field_report":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        source = str(args.get("source", "pcb"))
        required_fields = args.get("required_fields")
        file_format = str(args.get("format", "csv"))
        filename_arg = args.get("filename")
        filename = str(filename_arg) if filename_arg is not None else None
        try:
            return text_result(
                export_component_field_report(source, required_fields, file_format, filename, timeout_seconds)
            )
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    if name == "altium_update_schematic_parameters":
        timeout_seconds = float(args.get("timeout_seconds", 60))
        updates = args.get("updates")
        confirm = argument_bool(args.get("confirm"), False)
        try:
            return text_result(update_schematic_parameters(updates, confirm, timeout_seconds))
        except Exception as exc:
            return text_result(str(exc), is_error=True)

    command = BRIDGE_TOOL_COMMANDS.get(name)
    if command is None:
        return text_result(f"Unknown tool: {name}", is_error=True)

    timeout_seconds = float(args.get("timeout_seconds", 10))
    try:
        response = send_bridge_command(command, args, timeout_seconds)
    except Exception as exc:
        return text_result(str(exc), is_error=True)

    if not response.get("ok", False):
        return text_result(response, is_error=True)

    return text_result(response.get("result", response))


def read_message() -> object | None:
    global _framing

    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None

        stripped = line.strip()
        if not stripped:
            continue

        if stripped.lower().startswith(b"content-length:"):
            _framing = "content-length"
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                header = sys.stdin.buffer.readline()
                if header in (b"\r\n", b"\n", b""):
                    break
            body = sys.stdin.buffer.read(length)
            return json.loads(body.decode("utf-8"))

        _framing = "line"
        return json.loads(stripped.decode("utf-8"))


def write_message(message: object) -> None:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    if _framing == "content-length":
        sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write(raw + b"\n")
        sys.stdout.buffer.flush()


def rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def rpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_rpc(message: object) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return rpc_error(None, -32600, "Invalid JSON-RPC message")

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:
        return None

    if method == "initialize":
        requested_protocol = params.get("protocolVersion") if isinstance(params, dict) else None
        return rpc_result(
            request_id,
            {
                "protocolVersion": requested_protocol or DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return rpc_result(request_id, {})

    if method == "tools/list":
        return rpc_result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        if not isinstance(params, dict):
            return rpc_error(request_id, -32602, "Invalid tools/call params")
        name = params.get("name")
        if not isinstance(name, str):
            return rpc_error(request_id, -32602, "tools/call requires a string name")
        return rpc_result(request_id, call_tool(name, params.get("arguments")))

    if method == "resources/list":
        return rpc_result(request_id, {"resources": []})

    if method == "prompts/list":
        return rpc_result(request_id, {"prompts": []})

    return rpc_error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    ensure_shared_dir()

    while True:
        try:
            message = read_message()
        except Exception as exc:
            print(f"Failed to read MCP message: {exc}", file=sys.stderr)
            continue

        if message is None:
            return 0

        try:
            response = handle_rpc(message)
        except Exception as exc:  # pragma: no cover - hard failure diagnostics
            traceback.print_exc(file=sys.stderr)
            request_id = message.get("id") if isinstance(message, dict) else None
            response = rpc_error(request_id, -32603, str(exc))

        if response is not None:
            write_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
