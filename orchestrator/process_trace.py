"""Record Windows descendant-process lifecycle events for one command.

Windows does not keep a queryable history after a process exits unless process
auditing was enabled beforehand. This wrapper supplies that missing evidence for
an orchestrator reproduction: it launches one command, follows its descendants,
keeps query handles open, and writes process starts, exits, exit statuses, and
surviving orphan candidates to JSONL.

The tracer is diagnostic only. It never terminates a process.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TH32CS_SNAPPROCESS = 0x00000002


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str


class DescendantTracker:
    """Reconstruct a process tree across snapshots, including departed parents."""

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self.known_pids = {root_pid}
        self.processes: dict[int, ProcessInfo] = {}
        self.exit_codes: dict[int, int | None] = {}

    def observe(self, processes: Iterable[ProcessInfo]) -> list[ProcessInfo]:
        """Add every visible generation connected to the root process."""
        visible = {process.pid: process for process in processes}
        discovered: list[ProcessInfo] = []

        root = visible.get(self.root_pid)
        if root is not None and root.pid not in self.processes:
            self.processes[root.pid] = root
            discovered.append(root)

        changed = True
        while changed:
            changed = False
            for process in visible.values():
                if process.pid in self.known_pids:
                    continue
                if process.ppid not in self.known_pids:
                    continue
                self.known_pids.add(process.pid)
                self.processes[process.pid] = process
                discovered.append(process)
                changed = True
        return discovered

    def record_exit(self, pid: int, exit_code: int | None) -> bool:
        if pid in self.exit_codes:
            return False
        self.exit_codes[pid] = exit_code
        return True

    def orphan_candidates(self, active_pids: set[int]) -> list[ProcessInfo]:
        """Return live descendants whose tracked immediate parent is no longer live."""
        return [
            process
            for process in self.processes.values()
            if process.pid != self.root_pid
            and process.pid in active_pids
            and process.ppid in self.known_pids
            and process.ppid not in active_pids
        ]


def describe_exit_status(exit_code: int | None) -> str:
    """Give an exit status a useful, deliberately conservative label."""
    if exit_code is None:
        return "unknown"
    unsigned = exit_code & 0xFFFFFFFF
    known = {
        0: "normal",
        0xC0000005: "access_violation",
        0xC000013A: "interrupted",
        0xC0000409: "stack_buffer_overrun",
    }
    return known.get(unsigned, "nonzero_error_or_forced")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: object) -> None:
        record = {"timestamp_utc": _utc_now(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


if os.name == "nt":
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]


class WindowsProcessSampler:
    """Thin Toolhelp/GetExitCodeProcess boundary kept separate for testing."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("process_trace.py supports Windows only")

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        self.kernel32.Process32FirstW.restype = wintypes.BOOL
        self.kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        self.kernel32.Process32NextW.restype = wintypes.BOOL
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def snapshot(self) -> dict[int, ProcessInfo]:
        handle = self.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())

        processes: dict[int, ProcessInfo] = {}
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        try:
            success = self.kernel32.Process32FirstW(handle, ctypes.byref(entry))
            while success:
                process = ProcessInfo(
                    pid=int(entry.th32ProcessID),
                    ppid=int(entry.th32ParentProcessID),
                    name=entry.szExeFile,
                )
                processes[process.pid] = process
                success = self.kernel32.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.kernel32.CloseHandle(handle)
        return processes

    def open_query_handle(self, pid: int) -> int | None:
        handle = self.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
            False,
            pid,
        )
        return handle or None

    def exit_code(self, handle: int) -> int | None:
        code = wintypes.DWORD()
        if not self.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return int(code.value)

    def image_path(self, handle: int | None) -> str:
        if handle is None:
            return ""
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value

    def close(self, handle: int | None) -> None:
        if handle is not None:
            self.kernel32.CloseHandle(handle)


class ProcessTrace:
    def __init__(
        self,
        *,
        root_pid: int,
        output: Path,
        poll_seconds: float,
    ) -> None:
        self.root_pid = root_pid
        self.writer = JsonlWriter(output)
        self.poll_seconds = poll_seconds
        self.tracker = DescendantTracker(root_pid)
        self.sampler = WindowsProcessSampler()
        self.handles: dict[int, int | None] = {}
        self.last_snapshot: dict[int, ProcessInfo] = {}

    def sample(self) -> None:
        snapshot = self.sampler.snapshot()
        self.last_snapshot = snapshot
        for process in self.tracker.observe(snapshot.values()):
            handle = self.sampler.open_query_handle(process.pid)
            self.handles[process.pid] = handle
            self.writer.write(
                "process_observed",
                root_pid=self.root_pid,
                pid=process.pid,
                ppid=process.ppid,
                name=process.name,
                image=self.sampler.image_path(handle),
            )

        for pid in tuple(self.tracker.known_pids):
            if pid in self.tracker.exit_codes:
                continue
            handle = self.handles.get(pid)
            code = self.sampler.exit_code(handle) if handle is not None else None
            if code == STILL_ACTIVE:
                continue
            if code is None and pid in snapshot:
                continue
            if self.tracker.record_exit(pid, code):
                self.writer.write(
                    "process_exited",
                    root_pid=self.root_pid,
                    pid=pid,
                    exit_code=code,
                    exit_code_hex=(f"0x{code & 0xFFFFFFFF:08X}" if code is not None else ""),
                    exit_reason=describe_exit_status(code),
                )

    def finish(self, root_exit_code: int, grace_seconds: float) -> None:
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            self.sample()
            time.sleep(self.poll_seconds)
        self.sample()

        active_pids = set(self.last_snapshot)
        orphans = self.tracker.orphan_candidates(active_pids)
        for process in orphans:
            self.writer.write(
                "orphan_candidate",
                root_pid=self.root_pid,
                pid=process.pid,
                ppid=process.ppid,
                name=process.name,
                parent_exit_code=self.tracker.exit_codes.get(process.ppid),
                parent_exit_reason=describe_exit_status(
                    self.tracker.exit_codes.get(process.ppid)
                ),
            )
        self.writer.write(
            "trace_finished",
            root_pid=self.root_pid,
            root_exit_code=root_exit_code,
            observed_processes=len(self.tracker.processes),
            orphan_candidates=len(orphans),
        )
        for handle in self.handles.values():
            self.sampler.close(handle)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a command while recording its Windows descendant processes and exit statuses."
        )
    )
    parser.add_argument("--output", required=True, type=Path, help="JSONL trace path.")
    parser.add_argument(
        "--poll-ms",
        type=int,
        default=50,
        help="Process snapshot interval in milliseconds (default: 50).",
    )
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=2.0,
        help="How long to observe descendants after the root exits (default: 2).",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --.")
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.poll_ms < 10:
        parser.error("--poll-ms must be at least 10")
    if args.grace_seconds < 0:
        parser.error("--grace-seconds must not be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if os.name != "nt":
        print("process_trace.py supports Windows only", file=sys.stderr)
        return 2

    process = subprocess.Popen(args.command)
    trace = ProcessTrace(
        root_pid=process.pid,
        output=args.output,
        poll_seconds=args.poll_ms / 1000,
    )
    trace.writer.write(
        "trace_started",
        root_pid=process.pid,
        executable=args.command[0],
        cwd=os.getcwd(),
    )
    while process.poll() is None:
        trace.sample()
        time.sleep(trace.poll_seconds)
    root_exit_code = int(process.returncode or 0)
    trace.finish(root_exit_code, args.grace_seconds)
    print(f"process trace written to {trace.writer.path}")
    return root_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
