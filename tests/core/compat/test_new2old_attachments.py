# Copyright (C) 2026 Vrije Universiteit Brussel. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Example of command attachments through the new -> old campaign adapter.
"""

from pathlib import Path
from typing import Any, Dict

from benchkit.core.bktypes.callresults import BuildResult, FetchResult, RunResult
from benchkit.core.bktypes.execfn import ExecOutput
from benchkit.core.compat.new2old import CampaignCartesianProduct
from benchkit.utils.dir import get_benches_dir

_SRC_DIR = (get_benches_dir(parent_dir=None) / "tmp_attachments_test").resolve()


class AttachmentBench:
    """New-protocol benchmark running a command through ctx.exec(...)."""

    def fetch(self, ctx) -> FetchResult:
        src = _SRC_DIR
        src.mkdir(parents=True, exist_ok=True)
        return FetchResult(src_dir=src)

    def build(self, ctx) -> BuildResult:
        return BuildResult(build_dir=_SRC_DIR)

    def run(self, ctx) -> RunResult:
        out: ExecOutput = ctx.exec(argv=["sh", "-c", "sleep 0.2; echo attachment bench"])
        return RunResult(outputs=[out])

    def collect(self, ctx) -> Dict[str, Any]:
        return {"stdout": ctx.run_result.outputs[-1].stdout.strip()}


class RecordingAttachment:
    """Command attachment recording the processes it observes."""

    def __init__(self) -> None:
        self.pids = []
        self.record_dirs = []

    def __call__(self, process, record_data_dir) -> None:
        self.pids.append(process.pid)
        self.record_dirs.append(record_data_dir)


def main() -> None:
    recorder = RecordingAttachment()
    campaign = CampaignCartesianProduct(
        benchmark=AttachmentBench(),
        variables={},
        command_attachments=[recorder],
        duration_s=1,
        symlink_latest=True,
    )
    campaign.run()

    assert len(recorder.pids) == 1, f"expected one attached process, got {recorder.pids}"
    assert recorder.record_dirs[0] is not None

    csv_path = Path(campaign.parameters["result_csv_path"])
    latest = Path(str(csv_path).rsplit("_", 3)[0] + "_latest")
    assert latest.is_symlink(), f"expected latest symlink at {latest}"
    print(f"attachment test ok: pid={recorder.pids[0]} record_dir={recorder.record_dirs[0]}")


if __name__ == "__main__":
    main()
