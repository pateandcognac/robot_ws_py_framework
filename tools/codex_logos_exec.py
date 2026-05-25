#!/usr/bin/env python3
"""
Send a one-shot Python request to the Logos Python worker.

This is a thin debugging bridge for Codex and humans. It publishes a uniquely
tagged ``CognitionOutput`` containing a ``<py>`` block, waits for the matching
``CognitionInput`` result, and prints the worker output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


DEFAULT_REQUEST_TYPE = "codex_tool"
DEFAULT_WORKSPACE = "Logos"
FILE_TAG_RE = re.compile(r'<file\s+path="([^"]+)"[^>]*>(.*?)</file>', re.DOTALL)


@dataclass
class FileTag:
    path: str
    absolute_path: Optional[str]
    meta: str


@dataclass
class WorkerResult:
    msg_type: str
    filename: str
    content: str
    system_hint: str
    loop_cognition: bool
    file_tags: List[FileTag]


class ResultWaiter:
    def __init__(self, request_type: str, filename: str):
        self.request_type = request_type
        self.filename = filename
        self.condition = threading.Condition()
        self.message = None

    def callback(self, msg) -> None:
        if msg.filename != self.filename:
            return
        if msg.type != self.request_type:
            return
        with self.condition:
            self.message = msg
            self.condition.notify_all()

    def wait(self, timeout: float):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.message is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            return self.message


def read_code(args: argparse.Namespace) -> str:
    sources = [args.code is not None, args.file is not None, args.stdin]
    if sum(1 for enabled in sources if enabled) != 1:
        raise SystemExit("Provide exactly one of CODE, --file, or --stdin.")

    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    return args.code


def build_py_block(code: str, reset: bool, timeout: Optional[float], allow_loop: bool) -> str:
    if "</py" in code.lower():
        raise SystemExit("Code contains a closing </py> tag, which would break worker parsing.")

    attrs = []
    if reset:
        attrs.append('reset="true"')
    if timeout is not None:
        attrs.append(f'timeout="{int(timeout)}"')

    if not allow_loop:
        indented = textwrap.indent(code.rstrip() or "pass", "    ")
        code = (
            "try:\n"
            f"{indented}\n"
            "finally:\n"
            "    loop_cognition = False\n"
        )

    attr_text = (" " + " ".join(attrs)) if attrs else ""
    return f"<py{attr_text}>\n{code.rstrip()}\n</py>"


def resolve_workspace_path(workspace: str, workspace_path: Optional[str]) -> Path:
    if workspace_path:
        return Path(workspace_path).expanduser()
    return Path.home() / "robot_workspaces" / workspace


def parse_file_tags(content: str, workspace_path: Path) -> List[FileTag]:
    tags = []
    for match in FILE_TAG_RE.finditer(content):
        raw_path = match.group(1)
        meta = match.group(2).strip()
        path = Path(raw_path).expanduser()
        absolute = path if path.is_absolute() else workspace_path / path
        tags.append(
            FileTag(
                path=raw_path,
                absolute_path=str(absolute),
                meta=meta,
            )
        )
    return tags


def wait_for_subscribers(pub, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pub.get_num_connections() > 0:
            return True
        time.sleep(0.05)
    return pub.get_num_connections() > 0


def run_request(args: argparse.Namespace) -> WorkerResult:
    if args.ros_master_uri:
        os.environ["ROS_MASTER_URI"] = args.ros_master_uri
    ros_log_dir = Path(args.ros_log_dir).expanduser()
    ros_log_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ROS_LOG_DIR", str(ros_log_dir))

    try:
        import rospy
        from logos_framework.msg import CognitionInput, CognitionOutput
    except Exception as exc:
        raise SystemExit(f"Failed to import ROS/Logos Python packages: {exc}") from exc

    code = read_code(args)
    filename = args.filename or f"codex:{uuid.uuid4().hex}"
    workspace_path = resolve_workspace_path(args.workspace, args.workspace_path)
    wait_timeout = args.wait_timeout
    if wait_timeout is None:
        wait_timeout = max((args.timeout or 0) + 5.0, 10.0)

    waiter = ResultWaiter(args.request_type, filename)

    try:
        rospy.init_node("codex_logos_exec", anonymous=True, disable_signals=True)
    except Exception as exc:
        raise SystemExit(
            "Failed to initialize rospy. Check that ROS master is reachable and "
            "that this process is allowed to open ROS XML-RPC sockets."
        ) from exc
    rospy.Subscriber("/cognition/input", CognitionInput, waiter.callback, queue_size=20)
    publisher = rospy.Publisher("/cognition/output", CognitionOutput, queue_size=10)

    if not wait_for_subscribers(publisher, args.publish_wait):
        raise SystemExit(
            "No subscribers appeared on /cognition/output. "
            "Is roslaunch logos_framework start_framework.launch running?"
        )

    msg = CognitionOutput()
    msg.type = args.request_type
    msg.content = build_py_block(
        code=code,
        reset=args.reset,
        timeout=args.timeout,
        allow_loop=args.allow_loop,
    )
    msg.filename = filename

    publisher.publish(msg)
    result = waiter.wait(wait_timeout)
    if result is None:
        raise SystemExit(
            f"Timed out after {wait_timeout:.1f}s waiting for result filename={filename!r}."
        )

    return WorkerResult(
        msg_type=result.type,
        filename=result.filename,
        content=result.content,
        system_hint=result.system_hint,
        loop_cognition=bool(result.loop_cognition),
        file_tags=parse_file_tags(result.content, workspace_path),
    )


def file_tags_as_dicts(file_tags: Iterable[FileTag]) -> List[dict]:
    return [
        {
            "path": tag.path,
            "absolute_path": tag.absolute_path,
            "meta": tag.meta,
        }
        for tag in file_tags
    ]


def print_text_result(result: WorkerResult) -> None:
    print(result.content)
    if result.system_hint:
        print("\n# system_hint")
        print(result.system_hint)
    print(f"\n# result type={result.msg_type} filename={result.filename} loop_cognition={result.loop_cognition}")

    if result.file_tags:
        print("\n# file tags")
        for tag in result.file_tags:
            meta = f" meta={tag.meta!r}" if tag.meta else ""
            print(f"{tag.path} -> {tag.absolute_path}{meta}")


def print_json_result(result: WorkerResult) -> None:
    print(
        json.dumps(
            {
                "type": result.msg_type,
                "filename": result.filename,
                "content": result.content,
                "system_hint": result.system_hint,
                "loop_cognition": result.loop_cognition,
                "file_tags": file_tags_as_dicts(result.file_tags),
            },
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute Python in the live Logos Python worker via ROS topics."
    )
    parser.add_argument("code", nargs="?", help="Python code to execute.")
    parser.add_argument("--file", help="Read Python code from a file.")
    parser.add_argument("--stdin", action="store_true", help="Read Python code from stdin.")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="Workspace name used for resolving file tags.")
    parser.add_argument("--workspace-path", help="Explicit workspace path used for resolving file tags.")
    parser.add_argument("--request-type", default=DEFAULT_REQUEST_TYPE, help="CognitionOutput/Input type to use.")
    parser.add_argument("--filename", help="Explicit request filename/correlation id.")
    parser.add_argument("--timeout", type=float, help="Python worker execution timeout in seconds.")
    parser.add_argument("--wait-timeout", type=float, help="Client result wait timeout in seconds.")
    parser.add_argument("--publish-wait", type=float, default=3.0, help="Seconds to wait for /cognition/output subscribers.")
    parser.add_argument("--reset", action="store_true", help="Reset the persistent interpreter before running.")
    parser.add_argument(
        "--allow-loop",
        action="store_true",
        help="Allow the code to set loop_cognition=True and wake Logos cognition.",
    )
    parser.add_argument("--ros-master-uri", help="Override ROS_MASTER_URI before connecting.")
    parser.add_argument("--ros-log-dir", default="/tmp/codex_ros_logs", help="Directory for rospy logs.")
    parser.add_argument("--json", action="store_true", help="Print structured JSON result.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run_request(args)
    if args.json:
        print_json_result(result)
    else:
        print_text_result(result)


if __name__ == "__main__":
    main()
