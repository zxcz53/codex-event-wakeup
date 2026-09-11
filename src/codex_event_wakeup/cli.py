from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .core import CodexExecWakeBackend, JobRecord, JobStore, run_process_job, tail_text

DEFAULT_WAKE_PROMPT = (
    "Continue the task that was waiting on this background process. "
    "Inspect the produced logs, metrics, checkpoints, or artifacts as needed; "
    "do not merely report that the process finished."
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cew", description="Event-driven wakeups for Codex"
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="State directory (default: ~/.codex/event-wakeup)",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    run = sub.add_parser(
        "run",
        help="Run a long command in a detached watcher and wake this Codex thread on exit",
    )
    run.add_argument(
        "--thread-id", default=None, help="Codex thread ID; defaults to CODEX_THREAD_ID"
    )
    run.add_argument("--cwd", type=Path, default=Path.cwd())
    run.add_argument("--codex-bin", default="codex")
    run.add_argument("--tail-lines", type=int, default=80)
    run.add_argument("--wake-prompt", default=DEFAULT_WAKE_PROMPT)
    run.add_argument("command", nargs=argparse.REMAINDER)

    status = sub.add_parser(
        "status", help="Show one job, or recent jobs when no ID is supplied"
    )
    status.add_argument("job_id", nargs="?")

    logs = sub.add_parser("logs", help="Show the tail of a job log")
    logs.add_argument("job_id")
    logs.add_argument("--lines", type=int, default=120)

    retry = sub.add_parser(
        "retry", help="Retry delivery of an already-finished wake event"
    )
    retry.add_argument("job_id")

    install_skill = sub.add_parser(
        "install-skill", help="Install the bundled Codex skill into CODEX_HOME"
    )
    install_skill.add_argument("--force", action="store_true")

    sub.add_parser("doctor", help="Check Codex/wakeup prerequisites")

    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("job_id")

    return parser


def _normalize_command(parts: list[str]) -> list[str]:
    if parts and parts[0] == "--":
        parts = parts[1:]
    return parts


def _spawn_detached_worker(job_id: str, store: JobStore) -> int:
    args = [
        sys.executable,
        "-m",
        "codex_event_wakeup.cli",
        "--home",
        str(store.home),
        "_worker",
        job_id,
    ]
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }

    if os.name == "nt":
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        try:
            process = subprocess.Popen(
                args, creationflags=creationflags | breakaway, **kwargs
            )
        except OSError:
            # Some parent Job Objects disallow breakaway. The fallback still works
            # in ordinary terminals; `cew doctor` documents this limitation.
            process = subprocess.Popen(args, creationflags=creationflags, **kwargs)
    else:
        process = subprocess.Popen(args, start_new_session=True, **kwargs)
    return process.pid


def _cmd_run(args: argparse.Namespace, store: JobStore) -> int:
    thread_id = args.thread_id or os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        print(
            "error: no Codex thread ID; run from a Codex shell tool or pass --thread-id",
            file=sys.stderr,
        )
        return 2
    command = _normalize_command(args.command)
    if not command:
        print("error: missing command after `cew run --`", file=sys.stderr)
        return 2

    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: cwd is not a directory: {cwd}", file=sys.stderr)
        return 2

    record = JobRecord.create(
        thread_id=thread_id,
        command=command,
        cwd=str(cwd),
        job_dir=store.jobs_dir,
        wake_prompt=args.wake_prompt,
        codex_bin=args.codex_bin,
        tail_lines=max(0, args.tail_lines),
    )
    store.save(record)
    try:
        record.worker_pid = _spawn_detached_worker(record.job_id, store)
        store.save(record)
    except Exception as exc:
        record.status = "worker_spawn_failed"
        record.delivery_error = str(exc)
        store.save(record)
        print(f"error: could not spawn detached watcher: {exc}", file=sys.stderr)
        return 1

    print(record.job_id)
    return 0


def _cmd_status(args: argparse.Namespace, store: JobStore) -> int:
    if args.job_id:
        try:
            print(store.load(args.job_id).to_json())
        except FileNotFoundError:
            print(f"error: unknown job: {args.job_id}", file=sys.stderr)
            return 1
        return 0

    rows = store.list()[:20]
    if not rows:
        print("No jobs.")
        return 0
    for item in rows:
        exit_text = "-" if item.exit_code is None else str(item.exit_code)
        print(
            f"{item.job_id}\t{item.status}\texit={exit_text}\tdelivery={item.delivery_status}"
        )
    return 0


def _cmd_logs(args: argparse.Namespace, store: JobStore) -> int:
    try:
        record = store.load(args.job_id)
    except FileNotFoundError:
        print(f"error: unknown job: {args.job_id}", file=sys.stderr)
        return 1
    print(tail_text(Path(record.log_path), max(0, args.lines)))
    return 0


async def _retry(record: JobRecord, store: JobStore) -> int:
    if not record.completion_message:
        print("error: job has no completion event yet", file=sys.stderr)
        return 2
    backend = CodexExecWakeBackend(record.codex_bin)
    try:
        await backend.send(record.thread_id, record.completion_message)
    except Exception as exc:
        record.delivery_status = "failed"
        record.delivery_error = str(exc)
        store.save(record)
        print(f"error: wake delivery failed: {exc}", file=sys.stderr)
        return 1
    record.delivery_status = "delivered"
    record.delivery_error = None
    store.save(record)
    return 0


async def _worker(job_id: str, store: JobStore) -> int:
    record = store.load(job_id)
    backend = CodexExecWakeBackend(record.codex_bin)
    await run_process_job(record, store, backend)
    return 0


def _cmd_install_skill(force: bool) -> int:
    codex_home = Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    ).expanduser().resolve()
    destination = codex_home / "skills" / "codex-event-wakeup" / "SKILL.md"
    if destination.exists() and not force:
        print(
            f"error: skill already exists: {destination} (use --force to replace)",
            file=sys.stderr,
        )
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = importlib.resources.files("codex_event_wakeup").joinpath("assets/SKILL.md")
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    print(destination)
    return 0


def _cmd_doctor() -> int:
    codex = shutil.which("codex")
    thread_id = os.environ.get("CODEX_THREAD_ID")
    report = {
        "codex_executable": codex,
        "codex_thread_id_visible": bool(thread_id),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "windows_breakaway_supported_by_python": (
            hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB") if os.name == "nt" else None
        ),
    }
    print(json.dumps(report, indent=2))
    return 0 if codex else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    store = JobStore(args.home)

    if args.command_name == "run":
        return _cmd_run(args, store)
    if args.command_name == "status":
        return _cmd_status(args, store)
    if args.command_name == "logs":
        return _cmd_logs(args, store)
    if args.command_name == "retry":
        try:
            record = store.load(args.job_id)
        except FileNotFoundError:
            print(f"error: unknown job: {args.job_id}", file=sys.stderr)
            return 1
        return asyncio.run(_retry(record, store))
    if args.command_name == "install-skill":
        return _cmd_install_skill(args.force)
    if args.command_name == "doctor":
        return _cmd_doctor()
    if args.command_name == "_worker":
        return asyncio.run(_worker(args.job_id, store))
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
