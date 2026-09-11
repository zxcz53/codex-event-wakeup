from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from codex_event_wakeup.core import (
    JobRecord,
    JobStore,
    TriggerContext,
    TriggerRunner,
    run_process_job,
)


class FakeBackend:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, thread_id: str, content: str) -> None:
        self.messages.append((thread_id, content))


class TriggerRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_context_delivers_to_backend(self) -> None:
        backend = FakeBackend()

        async def trigger(ctx: TriggerContext) -> None:
            await ctx.send("done")

        runner = TriggerRunner([trigger], TriggerContext("thread-1", backend))
        await runner.start()
        errors = await runner.wait()

        self.assertEqual(errors, [None])
        self.assertEqual(backend.messages, [("thread-1", "done")])

    async def test_process_job_waits_for_exit_and_sends_completion_event(self) -> None:
        backend = FakeBackend()
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            record = JobRecord.create(
                thread_id="thread-123",
                command=[sys.executable, "-c", "print('metric=0.397')"],
                cwd=tmp,
                job_dir=store.jobs_dir,
                wake_prompt="Analyze the metric and continue.",
                codex_bin="codex",
                tail_lines=20,
            )
            store.save(record)

            await run_process_job(record, store, backend)
            saved = store.load(record.job_id)

            self.assertEqual(saved.status, "finished")
            self.assertEqual(saved.exit_code, 0)
            self.assertEqual(saved.delivery_status, "delivered")
            self.assertEqual(len(backend.messages), 1)
            self.assertIn("metric=0.397", backend.messages[0][1])
            self.assertIn(record.job_id, backend.messages[0][1])


if __name__ == "__main__":
    unittest.main()
