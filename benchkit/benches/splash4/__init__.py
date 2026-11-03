# Copyright (C) 2026 Vrije Universiteit Brussel. All rights reserved.
# SPDX-License-Identifier: MIT
"""
SPLASH-4 benchmark suite implementation for benchkit.

This module implements the benchkit protocol for SPLASH-4, a suite of parallel
scientific kernels/applications (FFT, LU, RADIX, OCEAN, BARNES, WATER, ...).
SPLASH-4 is built with the ``m4`` macro processor + ``make``: each benchmark's
``*.c.in`` source is expanded against the shared ``pthread.m4`` macro file into
``*.c`` and compiled. The default build uses ``-D ATOMIC_BARRIERS`` (SPLASH-4's
lock-free sense-reversing barriers).

Structure of this bench (mirrors ``benchkit/benches/npb``):
- fetch:   clone the SPLASH-4 repository
- build:   ``make`` the selected benchmark; the binary lands in its own dir. The
           selected kernel is a *build* variable; the resolved recipe is carried
           to ``run`` via ``BuildResult.other`` (the ``speccpu2017`` pattern).
- run:     execute the binary from *inside* the benchmark dir (inputs are
           resolved by relative path), with the thread count passed on the CLI
- collect: parse the ``TIMING INFORMATION`` block; the primary metric is
           ``Total time without initialization`` (the ROI / parallel region)
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from benchkit.core.bktypes import RecordResult
from benchkit.core.bktypes.callresults import BuildResult, FetchResult, RunResult
from benchkit.core.bktypes.contexts import BuildContext, CollectContext, FetchContext, RunContext
from benchkit.dependencies.packages import PackageDependency
from benchkit.utils.buildtools import make
from benchkit.utils.dir import get_benches_dir
from benchkit.utils.fetchtools import git_clone

SPLASH4_URL = "https://github.com/OdnetninI/Splash-4.git"
# Pinned to the currently-cloned HEAD ("Fix README"); update as needed.
SPLASH4_COMMIT = "Splash-4.0.1"
# Fixes an off-by-one heap overflow in barnes' getparam.c: the shipped
# ``malloc(strlen(buf))`` is one byte too small for the ``strcpy``, which
# aborts the benchmark under glibc's _FORTIFY_SOURCE (default on Ubuntu).
_GETPARAM_PATCH = Path(__file__).parent / "barnes-getparam.patch"


@dataclass(frozen=True)
class Splash4Kernel:
    """
    Static description of one SPLASH-4 benchmark.

    Attributes:
        subdir: directory under ``<repo>/Splash-4/`` holding the Makefile + *.c.in.
        binary: the TARGET produced by ``make`` (differs from the README's
            generic names, e.g. ``LU-NOCONT``, ``OCEAN-CONT``, ``WATER-NSQUARED``).
        args: CLI argument template; ``{n}`` = nb_threads, ``{size}`` = input size.
        size: default value substituted for ``{size}`` (None if unused).
        stdin: input-file template fed on stdin (``{n}``/``{size}``), or None.
    """

    subdir: str
    binary: str
    args: str = ""
    size: Optional[int] = None
    stdin: Optional[str] = None
    # PARSEC-style input size levels: level 1 (the default) is the smallest,
    # larger levels make the run longer (more parallel work and better
    # per-thread perf-stat counters).
    sizes: Optional[Dict[int, int]] = None
    stdins: Optional[Dict[int, str]] = None
    # Missing per-(size, thread count) stdin files are generated at run time
    # from their matching PARSEC-class template: gen_templates[size] names the
    # template and gen_size_field/gen_nproc_field locate the workload-size and
    # NPROC tokens as ((line, token), 0-based).
    gen_templates: Optional[Dict[int, str]] = None
    gen_size_field: Optional[Tuple[int, int]] = None
    gen_nproc_field: Optional[Tuple[int, int]] = None

    def resolve_size(self, level: int) -> Optional[int]:
        """Return the input size for a level (1 is the smallest/default)."""
        if self.sizes is None:
            return self.size
        try:
            return self.sizes[level]
        except KeyError:
            raise ValueError(
                f"input size level {level} is not defined for this benchmark "
                f"(available levels: {sorted(self.sizes)})"
            ) from None

    def resolve_stdin(self, level: int) -> Optional[str]:
        """Return the stdin template for a level (1 is the smallest/default)."""
        if self.stdins is None:
            return self.stdin
        try:
            return self.stdins[level]
        except KeyError:
            raise ValueError(
                f"input size level {level} is not defined for this benchmark "
                f"(available levels: {sorted(self.stdins)})"
            ) from None


# Inputs follow the README "Recommended Inputs"; adjust sizes per campaign
# (smaller inputs exist, e.g. barnes ships n8k in addition to n16384). The
# swept kernels (fft, lu, lu-nc, ocean-nc, radix) are enlarged so each run
# lasts long enough for the per-thread perf-stat attachment to record the
# worker threads; cholesky ships no larger input than tk29.O and therefore
# stays dominated by initialization.
_BENCHES: dict[str, Splash4Kernel] = {
    # --- argument-based inputs ---
    # -m24 keeps the run around a second: the default -m16 finishes in tens of
    # milliseconds, the per-thread perf-stat attachment of the thread profiler
    # misses the worker threads and the derived CPI/frequency components end up
    # empty.
    "fft": Splash4Kernel(
        subdir="fft",
        binary="FFT",
        args="-p{n} -m{size}",
        sizes={1: 24, 2: 26, 3: 28, 4: 30},
    ),
    "radix": Splash4Kernel(
        subdir="radix",
        binary="RADIX",
        args="-p{n} -n{size}",
        sizes={1: 33554432, 2: 67108864, 3: 134217728, 4: 268435456, 5: 536870912},
    ),
    "lu": Splash4Kernel(
        subdir="lu-contiguous_blocks",
        binary="LU-CONT",
        args="-p{n} -n{size}",
        sizes={1: 2560, 2: 3072, 3: 3584, 4: 4096, 5: 8192, 6: 16384},
    ),
    "lu-nc": Splash4Kernel(
        subdir="lu-non_contiguous_blocks",
        binary="LU-NOCONT",
        args="-p{n} -n{size}",
        sizes={1: 2560, 2: 3072, 3: 3584, 4: 4096, 5: 8192, 6: 16384},
    ),
    "ocean": Splash4Kernel(
        subdir="ocean-contiguous_partitions",
        binary="OCEAN-CONT",
        args="-p{n} -n{size}",
        # Splash-4 rejects grids above 4098 (IMAX is 8098; 8194 would abort).
        sizes={1: 2050, 2: 4098},
    ),
    "ocean-nc": Splash4Kernel(
        subdir="ocean-non_contiguous_partitions",
        binary="OCEAN-NOCONT",
        args="-p{n} -n{size}",
        # Splash-4 rejects grids above 4098 (IMAX is 8098; 8194 would abort).
        sizes={1: 2050, 2: 4098},
    ),
    "radiosity": Splash4Kernel(
        subdir="radiosity",
        binary="RADIOSITY",
        args="-p {n} -bf {size} -batch -largeroom",
        sizes={1: "1.5e-2", 5: "6e-4", 6: "1.5e-4"},
    ),
    "raytrace": Splash4Kernel(
        subdir="raytrace",
        binary="RAYTRACE",
        args="-p{n} -a{size} -m64 inputs/car.env",
        sizes={1: 4, 5: 2048, 6: 4096},
    ),
    "volrend": Splash4Kernel(
        subdir="volrend",
        binary="VOLREND",
        args="{n} inputs/head {size}",
        sizes={1: 8, 2: 16, 3: 32, 4: 64, 5: 128, 6: 256, 7: 512, 8: 640},
    ),
    "volrend-npl": Splash4Kernel(
        subdir="volrend-no_print_lock",
        binary="VOLREND-NPL",
        args="{n} inputs/head {size}",
        sizes={1: 8, 2: 16, 3: 32, 4: 64, 5: 384, 6: 768},
    ),
    # --- stdin-based inputs (fed through the exec API's `stdin` parameter) ---
    "cholesky": Splash4Kernel(
        subdir="cholesky",
        binary="CHOLESKY",
        args="-p{n}",
        stdins={1: "inputs/tk29.O"},
    ),
    "barnes": Splash4Kernel(
        subdir="barnes",
        binary="BARNES",
        size=16384,
        stdin="inputs/n{size}-p{n}",
        sizes={1: 8192, 2: 16384, 3: 2000000, 4: 4194304},
        gen_templates={
            8192: "inputs/n16384-p1",
            2000000: "inputs/parsec_native",
            4194304: "inputs/parsec_native",
        },
        gen_size_field=(1, 0),
        gen_nproc_field=(11, 0),
    ),
    "fmm": Splash4Kernel(
        subdir="fmm",
        binary="FMM",
        size=16384,
        stdin="inputs/input.{n}.{size}",
        sizes={1: 16384, 2: 65536, 3: 4194304, 4: 4194304},
        gen_templates={
            65536: "inputs/parsec_native",
            4194304: "inputs/parsec_native",
        },
        gen_size_field=(2, 0),
        gen_nproc_field=(4, 0),
    ),
    # water-nsquared: the molecule count must be a perfect cube (the lattice
    # is NS**3, "NS = pow(NMOL, 1/3)" in initia.c); anything else segfaults.
    "water-nsq": Splash4Kernel(
        subdir="water-nsquared",
        binary="WATER-NSQUARED",
        size=512,
        stdin="inputs/n{size}-p{n}",
        sizes={1: 512, 2: 3375, 3: 10648, 4: 32768, 5: 15625, 6: 19683},
        gen_templates={
            3375: "inputs/parsec_simmedium",
            10648: "inputs/parsec_native",
            15625: "inputs/parsec_native",
            19683: "inputs/parsec_native",
            32768: "inputs/parsec_native",
        },
        gen_size_field=(0, 1),
        gen_nproc_field=(2, 0),
    ),
    "water-sp": Splash4Kernel(
        subdir="water-spatial",
        binary="WATER-SPATIAL",
        size=512,
        stdin="inputs/n{size}-p{n}",
        sizes={1: 512, 2: 8000, 3: 1000000, 4: 1000000},
        gen_templates={
            8000: "inputs/parsec_simmedium",
            1000000: "inputs/parsec_native",
        },
        gen_size_field=(0, 1),
        gen_nproc_field=(2, 0),
    ),
}


class Splash4Bench:
    """
    Benchmark implementation for the SPLASH-4 suite.

    Only ``run`` is strictly required by the benchkit protocol; ``fetch``,
    ``build`` and ``collect`` are provided for a full fetch -> build -> run ->
    collect campaign.

    Variables:
        - ``benchmark`` is a *build* variable (it selects which binary is
          compiled). ``build`` resolves it and hands the recipe to ``run`` via
          ``BuildResult.other["kernel"]`` rather than requiring ``run`` to
          re-declare it.
        - ``nb_threads`` and ``input_size`` are *run* variables (SPLASH-4 takes
          them as CLI arguments at runtime; nothing is compiled in).
    """

    def fetch(
        self,
        ctx: FetchContext,
        parent_dir: Optional[Path] = None,
        commit: str = SPLASH4_COMMIT,
    ) -> FetchResult:
        """Clone the SPLASH-4 repository and apply the bundled fixes."""
        parent_dir = get_benches_dir(parent_dir=parent_dir, comm=ctx.platform.comm)
        repo_dir = git_clone(
            ctx=ctx,
            url=SPLASH4_URL,
            commit=commit,
            parent_dir=parent_dir,
            patches=[_GETPARAM_PATCH],
        )
        return FetchResult(src_dir=repo_dir)

    def build(
        self,
        ctx: BuildContext,
        benchmark: str = "fft",
    ) -> BuildResult:
        """
        Build a single SPLASH-4 benchmark with ``make``.

        ``benchmark`` selects which kernel is compiled (a build-time choice). The
        binary is produced inside ``<repo>/Splash-4/<subdir>`` (also the run cwd,
        since inputs are resolved by relative path). The default build carries
        ``ATOMIC_BARRIERS`` from ``Makefile.config``.

        The resolved :class:`Splash4Kernel` is stored in ``BuildResult.other`` so
        that ``run`` does not need to re-take ``benchmark``.
        """
        kernel = self._kernel(benchmark)
        bench_dir = ctx.fetch_result.src_dir / "Splash-4" / kernel.subdir

        make(
            ctx,
            src_dir=bench_dir,
            targets=[kernel.binary],
            options={},
        )

        return BuildResult(build_dir=bench_dir, other={"kernel": kernel})

    def run(
        self,
        ctx: RunContext,
        nb_threads: int = 2,
        input_size: Optional[int] = None,
    ) -> RunResult:
        """
        Run the SPLASH-4 benchmark selected at build time, from its own directory.

        The kernel recipe is recovered from ``ctx.build_result.other["kernel"]``.
        Threads are passed on the command line (``-p<N>`` or a leading
        positional), not via an environment variable. Benchmarks reading their
        workload from stdin are fed through the exec API's ``stdin`` parameter,
        so the kernel binary is executed directly rather than through a shell
        (attachments such as profilers then monitor the kernel itself, not the
        shell).
        """
        kernel: Splash4Kernel = ctx.build_result.other["kernel"]
        bench_dir = ctx.build_result.build_dir
        level = 1 if input_size is None else int(input_size)
        size = kernel.resolve_size(level)

        argv = [f"./{kernel.binary}"]
        if kernel.args:
            argv += kernel.args.format(n=nb_threads, size=size).split()

        stdin = None
        stdin_pattern = kernel.resolve_stdin(level)
        if stdin_pattern:
            stdin = bench_dir / stdin_pattern.format(n=nb_threads, size=size)
            self._ensure_stdin(
                bench_dir=bench_dir,
                kernel=kernel,
                nb_threads=nb_threads,
                size=size,
                stdin_path=stdin,
            )

        exec_out = ctx.exec(
            argv=argv,
            cwd=bench_dir,
            stdin=stdin,
            output_is_log=True,
        )
        return RunResult(outputs=[exec_out])

    @staticmethod
    def _ensure_stdin(
        bench_dir: Path,
        kernel: Splash4Kernel,
        nb_threads: int,
        size: int,
        stdin_path: Path,
    ) -> None:
        """Generate the stdin file when the repository does not ship it.

        The repository ships per-(size, thread count) input files only for the
        small default sizes; barnes/fmm/water-* inputs encode the thread count
        (NPROC) inside the file, so a missing combination cannot be faked by
        copying another thread count's file. The file is rebuilt from the
        matching PARSEC-class template by rewriting the workload-size and NPROC
        fields.
        """
        if kernel.gen_templates is None or stdin_path.exists():
            return
        template_path = kernel.gen_templates.get(size)
        if template_path is None:
            return
        lines = (bench_dir / template_path).read_text().splitlines()
        fields = ((kernel.gen_size_field, size), (kernel.gen_nproc_field, nb_threads))
        for field, value in fields:
            line_idx, token_idx = field
            tokens = lines[line_idx].split()
            tokens[token_idx] = str(value)
            lines[line_idx] = " ".join(tokens)
        stdin_path.write_text("\n".join(lines) + "\n")

    def collect(self, ctx: CollectContext) -> RecordResult:
        """
        Parse the SPLASH-4 timing output into a record.

        SPLASH-4 kernels do not all print the same timing block:

        - Most (fft, lu*, ocean*, radix, cholesky, fmm, radiosity, raytrace)
          print ``Total time with/without initialization``. The "without
          initialization" value is the timed parallel region (the ROI), matching
          the ROI-only measurement used upstream. The label separator varies
          across kernels: ``:`` (fft/lu), tabs (radiosity) or spaces (raytrace).
        - barnes prints ``TRACKTIME`` (its main tracked compute time).
        - water-{nsquared,spatial} print ``Measured Time (2nd timestep onward)``.
        - volrend prints only per-frame times (no single total).

        Every record carries the **same** keys (uniform schema, so campaigns
        aggregate into a non-ragged CSV):

        - ``primary_time_us``: the kernel's headline timing in microseconds (the
          ROI where available; the bespoke time for barnes/water; None for
          volrend, which prints only per-frame times).
        - ``time_without_init_us`` / ``time_with_init_us``: the standard SPLASH-4
          ROI / whole-run times, or None for kernels that don't emit them.

        This never raises: an unparsed kernel yields all-None rather than failing
        the whole campaign point.
        """
        output = ctx.run_result.outputs[-1].stdout

        def _find(label: str) -> Optional[int]:
            # separator between label and value varies: ':' (fft/lu), tabs
            # (radiosity), spaces (raytrace), or '=' (barnes/water).
            m = re.search(rf"{label}[ \t:=]*(\d+)", output)
            return int(m.group(1)) if m else None

        # Standard timing block (fft, lu*, ocean*, radix, cholesky, fmm,
        # radiosity, raytrace).
        without_init = _find(r"Total time without initialization")
        with_init = _find(r"Total time with initialization")
        primary = without_init if without_init is not None else with_init

        # Bespoke timing output, detected by content, when the standard block is
        # absent: barnes -> TRACKTIME, water-* -> "Measured Time". (volrend has
        # no single total -> primary stays None.)
        if primary is None:
            primary = _find(r"TRACKTIME")
        if primary is None:
            primary = _find(r"Measured Time[^=]*")

        return {
            "primary_time_us": primary,
            "time_without_init_us": without_init,
            "time_with_init_us": with_init,
        }

    @staticmethod
    def _kernel(benchmark: str) -> Splash4Kernel:
        if benchmark not in _BENCHES:
            raise ValueError(
                f"Unknown SPLASH-4 benchmark '{benchmark}'. " f"Supported: {sorted(_BENCHES)}"
            )
        return _BENCHES[benchmark]

    @staticmethod
    def dependencies() -> list[PackageDependency]:
        """System packages required to build/run SPLASH-4 (Debian/Ubuntu names)."""
        return [
            PackageDependency("build-essential"),
            PackageDependency("m4"),
        ]

    @staticmethod
    def supported_benchmarks() -> list[str]:
        """Return the list of selectable SPLASH-4 benchmarks."""
        return sorted(_BENCHES)
