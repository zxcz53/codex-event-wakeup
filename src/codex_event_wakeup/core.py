from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence


class WakeError(RuntimeError):
    """Raised when a wake event cannot be delivered to Codex."""


class WakeBackend(Protocol):
    async def send(self, thread_id: str, content: str) -> None:
        """Inject an event into a Codex thread and start/continue agent work."""


@dataclass(frozen=True)
class TriggerContext:
    """Capability passed to a trigger.

    This mirrors the useful boundary from Antigravity's TriggerContext: triggers
    know how to emit an event, but do not know how Codex receives that event.
    """

    thread_id: str
    backend: WakeBackend

    async def send(self, content: str) -> None:
        await self.backend.send(self.thread_id, content)


Trigger = Callable[[TriggerContext], Awaitable[None]]


class TriggerRunner:
    """Run independent triggers concurrently and isolate trigger failures."""

    def __init__(self, triggers: Sequence[Trigger], context: TriggerContext) -> None:
        self._triggers = list(triggers)
        self._context = context
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def is_running(self) -> bool:
        return any(not task.done() for task in self._tasks)

    async def start(self) -> None:
        if self._tasks:
            raise RuntimeError("TriggerRunner is already started")
        self._tasks = [
            asyncio.create_task(self._run_one(trigger), name=f"cew-trigger-{index}")
            for index, trigger in enumerate(self._triggers)
        ]

    async def wait(self) -> list[BaseException | None]:
        if not self._tasks:
            return []
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        return [item if isinstance(item, BaseException) else None for item in results]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_one(self, trigger: Trigger) -> None:
        try:
            await trigger(self._context)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Trigger isolation is intentional. Persistent job state records the
            # concrete failure; one failed trigger must not take down siblings.
            return


class CodexExecWakeBackend:
    """Wake backend implemented with the public Codex CLI resume command."""

    def __init__(self, codex_bin: str = "codex", timeout_seconds: float | None = None) -> None:
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds

    async def send(self, thread_id: str, content: str) -> None:
        executable = (
            shutil.which(self.codex_bin)
            if os.path.basename(self.codex_bin) == self.codex_bin
            else self.codex_bin
        )
        if not executable:
            raise WakeError(f"Codex executable not found: {self.codex_bin}")

        process = await asyncio.create_subprocess_exec(
            executable,
            "exec",
            "resume",
            thread_id,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        communicate = process.communicate(content.encode("utf-8"))
        try:
            if self.timeout_seconds is None:
                stdout, stderr = await communicate
            else:
                stdout, stderr = await asyncio.wait_for(
                    communicate, timeout=self.timeout_seconds
                )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WakeError("Codex wake command timed out") from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = stdout.decode("utf-8", errors="replace").strip()
            raise WakeError(
                f"Codex wake command failed ({process.returncode}): {detail}"
            )


@dataclass
class JobRecord:
    job_id: str
    thread_id: str
    command: list[str]
    cwd: str
    log_path: str
    created_at: float
    wake_prompt: str
    codex_bin: str = "codex"
    tail_lines: int = 80
    status: str = "queued"
    worker_pid: int | None = None
    process_pid: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    delivery_status: str = "pending"
    delivery_error: str | None = None
    completion_message: str | None = None

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        command: Sequence[str],
        cwd: str,
        job_dir: Path,
        wake_prompt: str,
        codex_bin: str,
        tail_lines: int,
    ) -> "JobRecord":
        job_id = f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        return cls(
            job_id=job_id,
            thread_id=thread_id,
            command=list(command),
            cwd=cwd,
            log_path=str(job_dir / f"{job_id}.log"),
            created_at=time.time(),
            wake_prompt=wake_prompt,
            codex_bin=codex_bin,
            tail_lines=tail_lines,
        )

    @classmethod
    def from_json(cls, payload: str) -> "JobRecord":
        return cls(**json.loads(payload))

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


class JobStore:
    def __init__(self, home: Path | None = None) -> None:
        base = home or Path(
            os.environ.get("CEW_HOME", Path.home() / ".codex" / "event-wakeup")
        )
        self.home = Path(base).expanduser().resolve()
        self.jobs_dir = self.home / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def save(self, record: JobRecord) -> None:
        path = self.path_for(record.job_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, job_id: str) -> JobRecord:
        return JobRecord.from_json(
            self.path_for(job_id).read_text(encoding="utf-8")
        )

    def list(self) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in sorted(self.jobs_dir.glob("job-*.json"), reverse=True):
            try:
                records.append(
                    JobRecord.from_json(path.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
        return records


def format_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess_list2cmdline(command)
    return shlex.join(command)


def subprocess_list2cmdline(command: Sequence[str]) -> str:
    # Keep Windows formatting available without importing subprocess at module
    # import time solely for formatting.
    import subprocess

    return subprocess.list2cmdline(list(command))


def tail_text(path: Path, lines: int) -> str:
    if lines <= 0 or not path.exists():
        return ""
    with path.open("rb") as handle:
        data = handle.read()
    decoded = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(decoded[-lines:])


def build_completion_message(record: JobRecord, tail: str) -> str:
    duration = None
    if record.started_at is not None and record.finished_at is not None:
        duration = max(0.0, record.finished_at - record.started_at)

    header = [
        "[CODEX EVENT WAKEUP] background process finished",
        f"job_id: {record.job_id}",
        f"exit_code: {record.exit_code}",
        f"command: {format_command(record.command)}",
        f"cwd: {record.cwd}",
        f"log: {record.log_path}",
    ]
    if duration is not None:
        header.append(f"duration_seconds: {duration:.1f}")
    if tail:
        header.extend(
            ["", f"last_{record.tail_lines}_log_lines:", "```text", tail, "```"]
        )
    if record.wake_prompt:
        header.extend(["", "continuation_instruction:", record.wake_prompt])
    return "\n".join(header)


async def run_process_job(
    record: JobRecord, store: JobStore, backend: WakeBackend
) -> None:
    """One-shot process trigger: wait on process completion, then emit an event."""

    record.status = "running"
    record.started_at = time.time()
    store.save(record)

    log_path = Path(record.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with log_path.open("ab", buffering=0) as log_handle:
            process = await asyncio.create_subprocess_exec(
                *record.command,
                cwd=record.cwd,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "CEW_JOB_ID": record.job_id},
            )
            record.process_pid = process.pid
            store.save(record)
            record.exit_code = await process.wait()
            record.status = "finished"
    except Exception as exc:
        record.status = "failed_to_start"
        record.exit_code = None
        with log_path.open("a", encoding="utf-8", errors="replace") as log_handle:
            log_handle.write(f"\n[cew] failed to run command: {exc!r}\n")
    finally:
        record.finished_at = time.time()
        record.process_pid = None
        tail = tail_text(log_path, record.tail_lines)
        record.completion_message = build_completion_message(record, tail)
        store.save(record)

    context = TriggerContext(thread_id=record.thread_id, backend=backend)
    try:
        await context.send(
            record.completion_message or build_completion_message(record, "")
        )
        record.delivery_status = "delivered"
        record.delivery_error = None
    except Exception as exc:
        record.delivery_status = "failed"
        record.delivery_error = str(exc)
    finally:
        store.save(record)
