#!/usr/bin/env python3
"""
MCP server exposing a few Codex-friendly Logos debugging tools.

Run with:
    /home/robot/robot_ws/.venv/bin/python3 tools/logos_mcp_server.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP


REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE = REPO_ROOT / "tools" / "codex_logos_exec.py"
DEFAULT_WORKSPACE = "Logos"
DEFAULT_WEB_URL = "http://127.0.0.1:5000"
ROS_PYTHON = os.environ.get("LOGOS_ROS_PYTHON", "/usr/bin/python3")
DEFAULT_ROS_DIST_PACKAGES = [
    REPO_ROOT / "devel" / "lib" / "python3" / "dist-packages",
    Path.home() / "tb2_ws" / "devel" / "lib" / "python3" / "dist-packages",
    Path("/opt/ros/noetic/lib/python3/dist-packages"),
]
DEFAULT_ROS_PACKAGE_PATHS = [
    REPO_ROOT / "src",
    Path.home() / "tb2_ws" / "src",
    Path("/opt/ros/noetic/share"),
]

mcp = FastMCP(
    "logos",
    instructions=(
        "Debug and test a live Logos robot framework by executing Python in "
        "the Logos Python worker, reading recent IO buffer entries, and "
        "fetching debug vision frames."
    ),
)


def workspace_path(workspace: str = DEFAULT_WORKSPACE, workspace_path: Optional[str] = None) -> Path:
    if workspace_path:
        return Path(workspace_path).expanduser()
    return Path.home() / "robot_workspaces" / workspace


def load_jsonl_tail(path: Path, count: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    entries: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines[-count:], start=max(len(lines) - count + 1, 1)):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            data = {"parse_error": str(exc), "raw": line}
        entries.append({"line": line_number, "data": data})
    return entries


def prepend_env_paths(env: Dict[str, str], name: str, paths: List[Path]) -> None:
    existing = env.get(name, "")
    existing_parts = [part for part in existing.split(os.pathsep) if part]
    new_parts = [str(path) for path in paths if path.exists()]
    env[name] = os.pathsep.join(new_parts + existing_parts)


def bridge_env() -> Dict[str, str]:
    env = os.environ.copy()
    prepend_env_paths(env, "PYTHONPATH", DEFAULT_ROS_DIST_PACKAGES)
    prepend_env_paths(env, "ROS_PACKAGE_PATH", DEFAULT_ROS_PACKAGE_PATHS)
    env.setdefault("ROS_MASTER_URI", "http://127.0.0.1:11311")
    env.setdefault("ROS_IP", "127.0.0.1")
    env.setdefault("ROS_VERSION", "1")
    env.setdefault("ROS_PYTHON_VERSION", "3")
    env.setdefault("ROS_DISTRO", "noetic")
    return env


@mcp.tool()
def logos_python(
    code: str,
    timeout: Optional[float] = None,
    reset: bool = False,
    allow_loop: bool = False,
    workspace: str = DEFAULT_WORKSPACE,
    workspace_path_override: Optional[str] = None,
    wait_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Execute Python code in the live Logos Python worker and return the result.

    By default, loop_cognition is suppressed so the Logos LLM is not awakened.
    Pass allow_loop=True only when you intentionally want worker code to be
    able to set loop_cognition=True.
    """
    command = [
        ROS_PYTHON,
        str(BRIDGE),
        "--json",
        "--stdin",
        "--workspace",
        workspace,
    ]
    if workspace_path_override:
        command.extend(["--workspace-path", workspace_path_override])
    if timeout is not None:
        command.extend(["--timeout", str(timeout)])
    if wait_timeout is not None:
        command.extend(["--wait-timeout", str(wait_timeout)])
    if reset:
        command.append("--reset")
    if allow_loop:
        command.append("--allow-loop")

    subprocess_timeout = max((wait_timeout or 0), (timeout or 0) + 7.0, 12.0)
    started = time.time()
    completed = subprocess.run(
        command,
        input=code,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=bridge_env(),
        timeout=subprocess_timeout,
        check=False,
    )
    elapsed = time.time() - started

    if completed.returncode != 0:
        return {
            "ok": False,
            "elapsed_sec": elapsed,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "elapsed_sec": elapsed,
            "parse_error": str(exc),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    payload["ok"] = True
    payload["elapsed_sec"] = elapsed
    return payload


@mcp.tool()
def logos_io_tail(
    count: int = 20,
    workspace: str = DEFAULT_WORKSPACE,
    workspace_path_override: Optional[str] = None,
    file_name: str = "io_buffer.jsonl",
) -> Dict[str, Any]:
    """Return recent entries from a Logos workspace state JSONL file."""
    if file_name not in {"io_buffer.jsonl", "io_history.jsonl", "summaries.jsonl"}:
        return {"ok": False, "error": "Unsupported file_name."}

    count = max(1, min(int(count), 200))
    state_file = workspace_path(workspace, workspace_path_override) / "state" / file_name
    return {
        "ok": True,
        "workspace_path": str(workspace_path(workspace, workspace_path_override)),
        "file": str(state_file),
        "entries": load_jsonl_tail(state_file, count),
    }


@mcp.tool()
def logos_debug_vision(
    topic: Optional[str] = None,
    web_url: str = DEFAULT_WEB_URL,
    save_dir: str = "/tmp/logos_debug_vision",
) -> Dict[str, Any]:
    """
    Fetch the latest Logos debug-vision frame and save it to a local image file.

    If topic is omitted, the newest frame across all debug-vision topics is used.
    """
    endpoint = web_url.rstrip("/") + "/api/debug-vision"
    try:
        with urllib.request.urlopen(endpoint, timeout=5) as response:
            snapshot = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "endpoint": endpoint}

    frames = snapshot.get("frames") or []
    if topic:
        frames = [frame for frame in frames if frame.get("topic") == topic or frame.get("name") == topic]
    if not frames:
        return {
            "ok": False,
            "error": "No matching debug-vision frames.",
            "topics": snapshot.get("topics", []),
            "endpoint": endpoint,
        }

    frame = max(frames, key=lambda item: item.get("received_time") or 0)
    src = frame.get("src", "")
    if not src.startswith("data:") or "," not in src:
        return {"ok": False, "error": "Frame has no data URL.", "frame": frame}

    header, encoded = src.split(",", 1)
    media_type = header[5:].split(";", 1)[0]
    extension = "jpg" if media_type == "image/jpeg" else "png"
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in frame.get("name", "frame"))
    output_dir = Path(save_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_name}_{int(time.time() * 1000)}.{extension}"
    output_path.write_bytes(base64.b64decode(encoded))

    frame_summary = {key: value for key, value in frame.items() if key != "src"}
    return {
        "ok": True,
        "path": str(output_path),
        "media_type": media_type,
        "frame": frame_summary,
        "topics": snapshot.get("topics", []),
    }


if __name__ == "__main__":
    mcp.run()
