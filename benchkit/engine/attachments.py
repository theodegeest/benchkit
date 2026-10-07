# Copyright (C) 2025 Vrije Universiteit Brussel. All rights reserved.
# SPDX-License-Identifier: MIT

"""
Command attachments for the execution engine.

An attachment observes a benchmark command while it runs: it receives the live
process handle together with the directory where the results of the record are
stored, and can start side monitors (tracing, profiling, signal injection, ...)
that are expected to terminate with the benchmark process.

Attachments are engine-agnostic: :class:`Attachments` instruments a
``RunContext`` by replacing its ``exec`` function with one that spawns the
command asynchronously, invokes every attachment in order on the live process,
and waits for the process to finish before returning an :class:`ExecOutput`.

The command is spawned once per ``ctx.exec(...)`` call, so all the attachments
of a record monitor the same live process concurrently, in the order in which
they are listed.
"""

from __future__ import annotations

import dataclasses
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from benchkit.core.bktypes import Argv, Env
from benchkit.core.bktypes.contexts import RunContext
from benchkit.core.bktypes.execfn import ExecFn, ExecOutput
from benchkit.platforms import Platform, get_current_platform
from benchkit.shell.shellasync import AsyncProcess, shell_async
from benchkit.utils.misc import TimeMeasure, get_benchkit_temp_folder_str


class CommandAttachment(Protocol):
    """
    Hook called with the live benchmark process and the record directory.

    It is expected to start monitoring the given process (for instance an
    eBPF program or a perf subprocess) and to return immediately, so that the
    next attachment is installed while the process keeps running.
    """

    def __call__(self, process: AsyncProcess, record_data_dir: Path | None) -> None:  # noqa: E704
        ...


@dataclass(frozen=True)
class Attachments:
    """
    Set of command attachments applied to every command executed by a record.

    An empty set is a no-op: the ``RunContext`` is returned unchanged.
    """

    attachments: Sequence[CommandAttachment] = ()

    def attach(self, *, run_ctx: RunContext, platform: Platform | None = None) -> RunContext:
        """
        Return a ``RunContext`` whose ``exec`` runs commands asynchronously and
        installs the attachments on the live process.

        Args:
            run_ctx: the run context to instrument.
            platform: platform on which the commands will be executed.

        Returns:
            The instrumented run context, or the original one if no attachment
            is configured.
        """
        if not self.attachments:
            return run_ctx

        if platform is None:
            platform = get_current_platform()

        instrumented_exec = self._make_instrumented_exec(
            platform=platform,
            default_record_dir=run_ctx.record_dir,
        )
        return dataclasses.replace(run_ctx, exec=instrumented_exec)

    def _make_instrumented_exec(
        self,
        *,
        platform: Platform,
        default_record_dir: Path | None,
    ) -> ExecFn:
        attachments = tuple(self.attachments)

        def attached_exec(
            *,
            argv: Argv,
            cwd: Path | None = None,
            env: Env | None = None,
            timeout_s: int | None = None,
            record_dir: Path | None = None,
            print_output: bool = False,
            output_is_log: bool = False,
            ignore_ret_codes: tuple[int, ...] = (),
            ignore_any_error_code: bool = False,
        ) -> ExecOutput:
            if timeout_s is not None:
                raise NotImplementedError(
                    "timeouts are not supported for commands instrumented with attachments"
                )

            run_command: list[str]
            if isinstance(argv, str):
                run_command = shlex.split(argv)
            else:
                run_command = list(argv)

            record_dir_path = Path(record_dir) if record_dir is not None else default_record_dir
            if record_dir_path is not None:
                record_dir_path.mkdir(parents=True, exist_ok=True)
                stdout_path = record_dir_path / "cmd_stdout.txt"
                stderr_path = record_dir_path / "cmd_stderr.txt"
            else:
                stdout_path = Path(f"{get_benchkit_temp_folder_str()}/benchkit_lastcmd_stdout.txt")
                stderr_path = Path(f"{get_benchkit_temp_folder_str()}/benchkit_lastcmd_stderr.txt")

            tm = TimeMeasure()
            with tm:
                process = shell_async(
                    command=run_command,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    platform=platform,
                    current_dir=cwd,
                    environment=env,
                    ignore_ret_codes=tuple(ignore_ret_codes),
                )
                for attachment in attachments:
                    attachment(process=process, record_data_dir=record_dir_path)

                returncode = 0
                try:
                    process.wait()
                except AsyncProcess.AsyncProcessError as error:
                    if not ignore_any_error_code:
                        raise
                    returncode = error.returncode

            stdout = stdout_path.read_text()
            stderr = stderr_path.read_text()
            if print_output:
                if stdout:
                    print(stdout, end="")
                if stderr:
                    print(stderr, end="", flush=True)

            return ExecOutput(
                argv=run_command,
                cwd=cwd,
                env=env,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                duration_s=tm.duration_seconds,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )

        return attached_exec
