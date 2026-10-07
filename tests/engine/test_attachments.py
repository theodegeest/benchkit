# Copyright (C) 2026 Vrije Universiteit Brussel. All rights reserved.
# SPDX-License-Identifier: MIT

"""
Tests for command attachments in the new API.
"""

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from benchkit.core.bktypes.callresults import BuildResult, FetchResult, RunResult
from benchkit.core.bktypes.contexts import BuildContext, FetchContext, RunContext
from benchkit.core.bktypes.execfn import ExecOutput
from benchkit.core.compat.new2old import CampaignCartesianProduct
from benchkit.engine.attachments import Attachments
from benchkit.engine.runonce import run_once
from benchkit.shell.shellasync import AsyncProcess


class _CmdBench:
    """Minimal new-protocol benchmark running one command through ctx.exec."""

    def __init__(
        self,
        command: List[str],
        ignore_any_error_code: bool = False,
        timeout_s: Optional[int] = None,
    ) -> None:
        self._command = command
        self._ignore_any_error_code = ignore_any_error_code
        self._timeout_s = timeout_s

    def fetch(self, ctx) -> FetchResult:
        return FetchResult(src_dir=Path.cwd())

    def build(self, ctx) -> BuildResult:
        return BuildResult(build_dir=Path.cwd())

    def run(self, ctx: RunContext) -> RunResult:
        out: ExecOutput = ctx.exec(
            argv=self._command,
            ignore_any_error_code=self._ignore_any_error_code,
            timeout_s=self._timeout_s,
        )
        return RunResult(outputs=[out])

    def collect(self, ctx) -> Dict[str, Any]:
        out: ExecOutput = ctx.run_result.outputs[-1]
        return {
            "stdout": out.stdout.strip(),
            "stderr": out.stderr.strip(),
            "returncode": out.returncode,
        }


class _RecordingAttachment:
    """Attachment recording the processes and record dirs it is attached to."""

    def __init__(self, calls: Optional[List[str]] = None, tag: str = "") -> None:
        self.processes: List[AsyncProcess] = []
        self.record_dirs: List[Optional[Path]] = []
        self.alive: List[bool] = []
        self._calls = calls
        self._tag = tag

    def __call__(self, process: AsyncProcess, record_data_dir: Optional[Path]) -> None:
        self.processes.append(process)
        self.record_dirs.append(record_data_dir)
        self.alive.append(not process.is_finished())
        if self._calls is not None:
            self._calls.append(self._tag)


class _EchoBench:
    """New-protocol benchmark with a run argument, for the new2old factory."""

    def fetch(self, ctx) -> FetchResult:
        return FetchResult(src_dir=Path.cwd())

    def build(self, ctx) -> BuildResult:
        return BuildResult(build_dir=Path.cwd())

    def run(self, ctx: RunContext, message: str = "hello") -> RunResult:
        out: ExecOutput = ctx.exec(argv=["sh", "-c", f"echo {message}"])
        return RunResult(outputs=[out])

    def collect(self, ctx) -> Dict[str, Any]:
        return {"stdout": ctx.run_result.outputs[-1].stdout.strip()}


class TestAttachments(unittest.TestCase):
    def test_no_attachments_is_noop(self) -> None:
        fetch_ctx = FetchContext.from_args(fetch_args={})
        build_ctx = BuildContext.from_fetch(
            ctx=fetch_ctx,
            fetch_result=FetchResult(src_dir=Path.cwd()),
            build_args={},
        )
        run_ctx = RunContext.from_build(
            ctx=build_ctx,
            build_result=BuildResult(build_dir=Path.cwd()),
            run_args={},
        )
        transformed = Attachments().attach(run_ctx=run_ctx)
        self.assertIs(run_ctx, transformed)

    def test_attachments_observe_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record_dir = Path(tmp)
            first = _RecordingAttachment()
            second = _RecordingAttachment()

            result = run_once(
                bench=_CmdBench(["sh", "-c", "sleep 0.2; echo hello; echo oops >&2"]),
                args={},
                record_dir=record_dir,
                attachments=Attachments(attachments=(first, second)),
            )

            self.assertEqual("hello", result["stdout"])
            self.assertEqual("oops", result["stderr"])
            self.assertEqual(0, result["returncode"])

            self.assertEqual(1, len(first.processes))
            self.assertEqual(1, len(second.processes))
            self.assertEqual(first.processes[0].pid, second.processes[0].pid)
            self.assertTrue(first.alive[0])
            self.assertTrue(second.alive[0])

            self.assertEqual(record_dir, first.record_dirs[0])
            self.assertEqual(record_dir, second.record_dirs[0])

            self.assertEqual("hello\n", (record_dir / "cmd_stdout.txt").read_text())
            self.assertEqual("oops\n", (record_dir / "cmd_stderr.txt").read_text())

    def test_attachments_are_installed_in_order(self) -> None:
        calls: List[str] = []
        attachments = tuple(_RecordingAttachment(calls=calls, tag=tag) for tag in "abc")

        with tempfile.TemporaryDirectory() as tmp:
            run_once(
                bench=_CmdBench(["sh", "-c", "true"]),
                args={},
                record_dir=Path(tmp),
                attachments=Attachments(attachments=attachments),
            )

        self.assertEqual(["a", "b", "c"], calls)

    def test_all_attachments_see_the_same_process(self) -> None:
        attachments = tuple(_RecordingAttachment() for _ in range(3))

        with tempfile.TemporaryDirectory() as tmp:
            run_once(
                bench=_CmdBench(["sh", "-c", "sleep 0.2"]),
                args={},
                record_dir=Path(tmp),
                attachments=Attachments(attachments=attachments),
            )

        pids = {a.processes[0].pid for a in attachments}
        self.assertEqual(1, len(pids))

    def test_failing_command_raises_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AsyncProcess.AsyncProcessError):
                run_once(
                    bench=_CmdBench(["sh", "-c", "exit 3"]),
                    args={},
                    record_dir=Path(tmp),
                    attachments=Attachments(attachments=(_RecordingAttachment(),)),
                )

    def test_failing_command_tolerated_when_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_once(
                bench=_CmdBench(["sh", "-c", "exit 3"], ignore_any_error_code=True),
                args={},
                record_dir=Path(tmp),
                attachments=Attachments(attachments=(_RecordingAttachment(),)),
            )

        self.assertEqual(3, result["returncode"])

    def test_timeout_is_not_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(NotImplementedError):
                run_once(
                    bench=_CmdBench(["sh", "-c", "true"], timeout_s=10),
                    args={},
                    record_dir=Path(tmp),
                    attachments=Attachments(attachments=(_RecordingAttachment(),)),
                )


class TestNew2OldAttachments(unittest.TestCase):
    def test_attachments_through_campaign_factory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results_dir = Path(tmp) / "results"
            results_dir.mkdir()
            recorder = _RecordingAttachment()

            campaign = CampaignCartesianProduct(
                benchmark=_EchoBench(),
                variables={"message": ["hello"]},
                command_attachments=[recorder],
                name="attachments_test",
                results_dir=results_dir,
                duration_s=1,
                symlink_latest=True,
            )
            campaign.run()

            csv_path = Path(campaign.parameters["result_csv_path"])
            self.assertTrue(csv_path.is_file())

            self.assertEqual(1, len(recorder.processes))
            self.assertIsNotNone(recorder.record_dirs[0])

            latest = Path(str(csv_path).rsplit("_", 3)[0] + "_latest")
            self.assertTrue(latest.is_symlink())
            self.assertTrue((csv_path.with_suffix("") / "results.csv").is_symlink())


if __name__ == "__main__":
    unittest.main()
