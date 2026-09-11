---
name: codex-event-wakeup
description: Use event-driven background jobs for long or unpredictable commands, then resume this Codex thread when the process exits.
---

# Codex Event Wakeup

Use `cew` when a command is expected to run long enough that waiting in the current shell tool call or periodically polling it would waste the turn.

## Launch

Prefer:

```sh
cew run -- <command> <args...>
```

`cew` reads `CODEX_THREAD_ID` from the Codex shell environment, launches an independent watcher, prints a job ID, and returns immediately. Do not repeatedly poll the process after a successful launch. End the current turn once there is no other useful work to do.

For a command that needs a specific working directory:

```sh
cew run --cwd /path/to/project -- python train.py
```

The watcher waits on the OS process-completion event. When the process exits, it captures the exit code and the tail of the log and resumes the original Codex thread with a `[CODEX EVENT WAKEUP]` event.

## On wakeup

Treat a `[CODEX EVENT WAKEUP]` message as an external completion event, not as a new user request. Continue the task that was blocked on the job: inspect produced logs, metrics, checkpoints, or artifacts; diagnose failures when the exit code is non-zero; and proceed with the next appropriate work.

## Inspection

Use these only when needed:

```sh
cew status <job-id>
cew logs <job-id> --lines 200
cew retry <job-id>
cew doctor
```

`cew retry` is for a completed job whose event could not be delivered (for example, Codex was temporarily unavailable).

## Constraints

- Do not use `cew` for short commands that can simply finish in the current tool call.
- Do not use fixed-interval polling to wait for a `cew` job.
- Avoid shell metacharacters unless explicitly invoking a shell, because `cew run` executes argv directly.
- If `CODEX_THREAD_ID` is unavailable, pass `--thread-id` explicitly or use the normal foreground execution path.
