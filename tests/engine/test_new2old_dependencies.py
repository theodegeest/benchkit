# Copyright (C) 2026 Vrije Universiteit Brussel. All rights reserved.
# SPDX-License-Identifier: MIT
"""
Tests for dependency forwarding in the new->old campaign adapter.
"""

import unittest
from typing import List

from benchkit.core.compat.new2old import Adapted
from benchkit.dependencies.packages import PackageDependency


class _DepBench:
    """Minimal new-protocol benchmark exposing static dependencies."""

    def run(self, ctx) -> None:
        pass

    @staticmethod
    def dependencies() -> List[PackageDependency]:
        return [PackageDependency("openjdk-21-jdk")]


class _NoDepBench:
    """Minimal new-protocol benchmark without dependencies."""

    def run(self, ctx) -> None:
        pass


class TestAdaptedDependencies(unittest.TestCase):
    def test_wrapped_benchmark_dependencies_are_forwarded(self) -> None:
        adapted = Adapted(benchmark=_DepBench())

        dependency_names = [dependency.name for dependency in adapted.dependencies()]

        self.assertIn("openjdk-21-jdk", dependency_names)

    def test_benchmark_without_dependencies_still_works(self) -> None:
        adapted = Adapted(benchmark=_NoDepBench())

        self.assertEqual([], adapted.dependencies())


if __name__ == "__main__":
    unittest.main()
