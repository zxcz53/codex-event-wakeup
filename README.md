# codex-event-wakeup

Event-driven wakeups for long-running Codex jobs.

The goal is the Antigravity-style workflow:

```text
Codex turn
   │
   ├─ cew run -- python train.py
   │       └─ returns job id immediately
   │
   └─ turn can end
           │
           ▼
   detached one-shot watcher
           │
           ├─ waits on process exit (no periodic polling)
           ├─ stores exit code + log tail
           └─ resumes the original Codex thread
                    │
                    ▼
             agent continues work
```

## Why this design

Google Antigravity's public trigger API separates background event sources from delivery through a `TriggerContext.send()` boundary. `codex-event-wakeup` uses the same architectural idea without depending on Antigravity: a trigger produces an event and a `WakeBackend` decides how that event reaches Codex.

The first backend uses the public Codex CLI:

```text
codex exec resume <CODEX_THREAD_ID> -
```

The boundary is intentionally kept separate so a future backend can talk directly to Codex app-server when a stable event-injection contract is available.

## Install

Requires Python 3.10+ and a working `codex` executable.

```sh
pip install git+https://github.com/zxcz53/codex-event-wakeup.git
cew install-skill
cew doctor
```

For development:

```sh
git clone https://github.com/zxcz53/codex-event-wakeup.git
cd codex-event-wakeup
pip install -e .
```

## Use from Codex

From a Codex shell tool call, `CODEX_THREAD_ID` is normally injected by Codex, so no session ID needs to be copied manually:

```sh
cew run -- python train.py --config configs/exp.yaml
```

The command prints a job ID such as:

```text
job-20260911-113000-a1b2c3d4
```

The current Codex turn can then finish. The watcher blocks on the child process itself rather than checking every N minutes. When the process exits, it sends an event back to the original thread containing the exit code, command, log path, duration, and recent log output.

### Custom continuation instruction

```sh
cew run \
  --wake-prompt "Read metrics.json, compare MSE with the previous run, and decide the next ablation." \
  -- python train.py
```

### Status and logs

```sh
cew status
cew status <job-id>
cew logs <job-id> --lines 200
```

If the process finished but Codex was unavailable when delivery was attempted:

```sh
cew retry <job-id>
```

Job metadata and logs live under `~/.codex/event-wakeup/jobs/` by default. Set `CEW_HOME` or pass `--home` to change it.

## Codex skill

The package bundles `skills/codex-event-wakeup/SKILL.md`. Run `cew install-skill` to install it into `$CODEX_HOME/skills/codex-event-wakeup/` (or `~/.codex/skills/codex-event-wakeup/`) so Codex knows to choose event-driven waiting for long and unpredictable jobs instead of periodic polling.

## Current implementation and roadmap

### v0.1 MVP

- Captures `CODEX_THREAD_ID` automatically.
- Runs the long command under a detached one-shot watcher.
- Waits for real process completion; no experiment-duration polling loop.
- Persists job state and logs.
- Resumes the original Codex thread with a structured completion event.
- Works without patching the OpenAI Codex repository.

### Planned

1. **App-server backend** — direct `thread/resume` + turn/event delivery through the shared Codex app-server, if/when that interface is stable enough for external clients.
2. **Persistent TriggerRunner daemon** — useful for webhook/file/process triggers and queued delivery. The v0.1 one-shot watcher deliberately avoids requiring a resident service for the basic experiment use case.
3. **More trigger types** — file creation/change, socket/webhook events, and composite conditions.
4. **Busy-thread delivery queue** — retain an event if the target thread is active and deliver it when the backend can accept it.

## Important platform note

Codex intentionally manages shell child processes and can terminate descendants associated with a shell tool. `cew run` therefore launches a separate watcher process instead of merely appending `&`/`Start-Process` to the training command. On Unix the watcher starts a new session. On Windows it requests a detached process and, when permitted by the parent Job Object, requests breakaway as well.

Run `cew doctor` after installation, especially on Windows. The app-server backend planned above should further reduce platform-specific process-detachment issues.

## Relationship to Antigravity

This project does not depend on or modify Antigravity. Its trigger abstraction is inspired by the public architecture of the Google Antigravity SDK: independent event producers, a small context object for sending events, and a transport/backend boundary. The implementation here is original and targets Codex's public CLI/session interfaces.
