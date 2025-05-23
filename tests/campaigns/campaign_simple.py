#!/usr/bin/env python3
# Copyright (C) 2024 Vrije Universiteit Brussel. All rights reserved.
# SPDX-License-Identifier: MIT


from pathlib import Path
from typing import Any, Dict, Iterable, List

from benchkit.benchmark import (
    Benchmark,
    CommandAttachment,
    CommandWrapper,
    PostRunHook,
    PreRunHook,
    SharedLib,
)
from benchkit.campaign import CampaignCartesianProduct, CampaignSuite
from benchkit.platforms import Platform
from benchkit.utils.dir import caller_dir


class SleepBench(Benchmark):
    def __init__(
        self,
        command_wrappers: Iterable[CommandWrapper] = (),
        command_attachments: Iterable[CommandAttachment] = (),
        shared_libs: Iterable[SharedLib] = (),
        pre_run_hooks: Iterable[PreRunHook] = (),
        post_run_hooks: Iterable[PostRunHook] = (),
        platform: Platform = None,
    ) -> None:
        super().__init__(
            command_wrappers=command_wrappers,
            command_attachments=command_attachments,
            shared_libs=shared_libs,
            pre_run_hooks=pre_run_hooks,
            post_run_hooks=post_run_hooks,
        )
        if platform is not None:
            self.platform = platform

    @property
    def bench_src_path(self) -> Path:
        return caller_dir()

    @staticmethod
    def get_build_var_names() -> List[str]:
        return []

    @staticmethod
    def get_run_var_names() -> List[str]:
        return ["duration_seconds"]

    def clean_bench(self) -> None:
        pass

    def prebuild_bench(
        self,
        **kwargs,
    ) -> int:
        return 0

    def build_bench(
        self,
        **kwargs,
    ) -> None:
        pass

    def single_run(
        self,
        duration_seconds: int,
        **kwargs,
    ) -> str:
        current_dir = self.bench_src_path
        environment = self._preload_env(
            duration_seconds=duration_seconds,
            **kwargs,
        )

        run_command = [
            "/usr/bin/sleep",
            f"{duration_seconds}s",
        ]

        wrapped_run_command, wrapped_environment = self._wrap_command(
            run_command=run_command,
            environment=environment,
            duration_seconds=duration_seconds,
            **kwargs,
        )

        output = self.run_bench_command(
            run_command=run_command,
            wrapped_run_command=wrapped_run_command,
            current_dir=current_dir,
            environment=environment,
            wrapped_environment=wrapped_environment,
            print_output=False,
            ignore_ret_codes=(1,),
        )
        return output

    def parse_output_to_results(  # pylint: disable=arguments-differ
        self,
        command_output: str,
        **_kwargs,
    ) -> Dict[str, Any]:
        result_dict = {}
        return result_dict


def main() -> None:
    campaign = CampaignCartesianProduct(
        name="charts",
        benchmark=SleepBench(),
        nb_runs=2,
        variables={"duration_seconds": [1, 2]},
        constants=None,
        debug=False,
        gdb=False,
        enable_data_dir=True,
    )
    campaign.run()

    campaign_suite = CampaignSuite(campaigns=[campaign])
    campaign_suite.generate_global_csv()
    campaign_suite.generate_graph(
        plot_name="lineplot",
        x="duration_seconds",
        y="duration_seconds",
        hue="rep",
        marker="o",
        markers=True,
        dashes=False,
    )


if __name__ == "__main__":
    main()
