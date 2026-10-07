# benchkit Benchmark Author Guide

This document explains how to author a benchmark using the benchkit
`fetch / build / run / collect` protocol.

The goals of this API are:
- keep benchmark code explicit and readable
- avoid magic and hidden global state
- allow benchmarks to be tested and executed in isolation
- integrate cleanly with benchkit execution engines, wrappers, and attachments

---

## 1. Mental model

A benchmark in benchkit is a four-stage pipeline executed per record:

1. **fetch**   - obtain sources or inputs (git clone, download, locate local path)
2. **build**   - compile binaries, generate datasets, prepare state
3. **run**     - execute the workload (possibly multiple commands)
4. **collect** - parse outputs and return result rows

Each stage receives a Context object giving access to:
- the execution platform
- a safe execution primitive (`ctx.exec`)
- variables for the current record
- results of previous stages

---

## 2. The golden rule

Always execute commands via `ctx.exec(...)`.

Never use:
- subprocess
- raw `platform.comm.shell`
- os.system

Why:
- `ctx.exec` integrates command wrappers (perf, numactl, taskset, etc.)
- it supports command attachments (tracing, profiling, monitoring)
- it captures stdout and stderr correctly
- it enforces consistent execution semantics

---

## 3. Contexts and results

Each pipeline stage has a dedicated context and result type:

| Stage   | Context         | Result        |
|--------|------------------|---------------|
| fetch  | FetchContext     | FetchResult   |
| build  | BuildContext     | BuildResult   |
| run    | RunContext       | RunResult     |
| collect| CollectContext   | RecordResult  |

Contexts are immutable dataclasses and expose:

```python
ctx.platform     # execution platform
ctx.exec(...)    # execution primitive
ctx.vars         # full variable bag for this record
ctx.record_dir   # per-run directory for artifacts (RunContext/CollectContext only)
````

Later contexts also expose previous results:

```python
ctx.fetch_result
ctx.build_result
ctx.run_result
```

---

## 4. Minimal benchmark skeleton

```python
from pathlib import Path
from benchkit.core.types import (
    FetchContext, BuildContext, RunContext, CollectContext,
    FetchResult, BuildResult, RunResult, RecordResult
)
from benchkit.dependencies.packages import PackageDependency


class MyBenchmark:
    def fetch(self, ctx: FetchContext, parent_dir: Path) -> FetchResult:
        return FetchResult(src_dir=parent_dir / "src")

    def build(self, ctx: BuildContext) -> BuildResult:
        return BuildResult(build_dir=ctx.fetch_result.src_dir)

    def run(self, ctx: RunContext, threads: int = 1) -> RunResult:
        out = ctx.exec(
            argv=["./mybench", f"--threads={threads}"],
            cwd=ctx.build_result.build_dir,
        )
        return RunResult(outputs=[out])

    def collect(self, ctx: CollectContext, threads: int = 1) -> RecordResult:
        stdout = ctx.run_result.outputs[0].stdout
        return {"threads": threads, "throughput": parse(stdout)}

    @staticmethod
    def dependencies() -> list[PackageDependency]:
        return [PackageDependency("build-essential")]
```

---

## 5. Parameters and introspection

benchkit automatically introspects function signatures.

Only parameters explicitly declared by a stage are passed.

Example:

```python
def run(self, ctx: RunContext, bench_name: str, nb_threads: int = 2):
    ...
```

Only `bench_name` and `nb_threads` will be injected if available.

Why this matters:

* no string-based variable lists
* no duplicated variable declarations
* unused variables are ignored automatically
* defaults make experimentation ergonomic

---

## 6. Executing a benchmark manually (plumbing-lite)

You can run a benchmark without the engine for debugging or testing.

```python
bench = RocksDBBench()

bench_dir = Path("~/.benchkit/benches").expanduser().resolve()

fc = FetchContext.from_args(fetch_args={"parent_dir": bench_dir, "commit": "v10.7.5"})
fr = fc.call(bench.fetch)

bc = BuildContext.from_fetch(ctx=fc, fetch_result=fr, build_args={})
br = bc.call(bench.build)

rc = RunContext.from_build(
    ctx=bc,
    build_result=br,
    run_args={"bench_name": "readrandom", "nb_threads": 3},
    duration_s=1,
)
rr = rc.call(bench.run)

cc = CollectContext.from_run(ctx=rc, run_result=rr)
result = cc.call(bench.collect)
```

This pattern is ideal for:

* local debugging
* unit tests
* developing parsing logic
* writing documentation examples

---

## 7. If you prefer to call the benchmark methods explicitly

The `.call()` helpers are convenience utilities.
They reduce boilerplate, but they are not mandatory.

If you prefer full explicit control, or want to debug each step manually,
you can call benchmark methods directly and pass arguments yourself.

This can be useful when:
- debugging a specific stage
- stepping through execution in a debugger
- teaching how the pipeline works internally
- writing very explicit tests

Example with explicit calls:

```python
bench = RocksDBBench()

bench_dir = Path("~/.benchkit/benches").expanduser().resolve()

# Fetch
fc = FetchContext.from_args(
    fetch_args={"parent_dir": bench_dir, "commit": "v10.7.5"}
)
fr = bench.fetch(ctx=fc, **fc.fetch_args)

# Build
bc = BuildContext.from_fetch(
    ctx=fc,
    fetch_result=fr,
    build_args={}
)
br = bench.build(ctx=bc, **bc.build_args)

# Run
run_args = {"bench_name": "readrandom", "nb_threads": 3}
rc = RunContext.from_build(
    ctx=bc,
    build_result=br,
    run_args=run_args,
    duration_s=1,
)
rr = bench.run(ctx=rc, **rc.run_args)

# Collect
cc = CollectContext.from_run(ctx=rc, run_result=rr)
result = bench.collect(ctx=cc, bench_name="readrandom")
```

This version makes all data flow explicit:

* which arguments are passed to each stage
* which results are forwarded
* how contexts are constructed

It is more verbose, but also maximally transparent.
However, both styles are fully supported and equivalent.

---

## 8. Multiple commands in run

A benchmark `run()` method may execute multiple commands.

```python
def run(self, ctx: RunContext, bench_name: str) -> RunResult:
    warmup = ctx.exec(argv=["./warmup"], cwd=ctx.build_result.build_dir)
    main = ctx.exec(argv=["./bench", f"--name={bench_name}"], cwd=ctx.build_result.build_dir)
    return RunResult(outputs=[warmup, main])
```

The collect step can then choose which outputs to parse.

---

## 9. Record artifacts and record_dir

If `ctx.record_dir` is provided, it is safe to write files there:

* generated inputs
* logs
* configuration snapshots
* traces

```python
if ctx.record_dir:
    ctx.exec(argv=["cp", "config.ini", ctx.record_dir / "config.ini"])
```

This field is only available on `RunContext` and `CollectContext`,
pointing to the specific per-run data directory.

---

## 10. Build vs source directories

benchkit does not assume any build system.

You decide:

* where binaries live
* where object files go
* whether source and build directories coincide

Example for Makefile-based projects:

```python
BuildResult(
    build_dir=src_dir,
    other={"obj_dir": obj_dir}
)
```

The meaning of `build_dir` is benchmark-defined.

---

## 11. Dependencies

Declare system dependencies once:

```python
@staticmethod
def dependencies() -> list[PackageDependency]:
    return [
        PackageDependency("build-essential"),
        PackageDependency("libsnappy-dev"),
    ]
```

The engine may later ensure they are installed.

---

## 12. Command attachments

Attachments observe the benchmark process **while it is running**: profilers,
tracers, or monitors that need a live process and a place to store their
artifacts. Attachments are not part of the benchmark class; they are supplied
to the engine (or to the compat campaign factories) and are attached to every
command executed through `ctx.exec(...)`:

```python
from benchkit.engine.attachments import Attachments

attachments = Attachments(attachments=[MyAttachment()])
result = run_once(bench=MyBenchmark(), args={"threads": 4}, attachments=attachments)
```

An attachment is any callable with the following shape:

```python
def __call__(self, process: AsyncProcess, record_data_dir: Path | None) -> None: ...
```

It receives the live process — with its `pid`, and signals available through
`process.send_signal(...)` — and the per-run record directory where it should
store its outputs. The engine spawns the benchmark command once, calls all
attachments in the order they are given, and then waits for the benchmark to
finish. Attachments typically spawn their own side-processes, so their
monitoring runs concurrently with the benchmark while the ordered installation
is preserved.

When the same benchmark is run through the legacy campaign engine, pass the
attachments to the `benchkit.core.compat.new2old` factories:

```python
from benchkit.core.compat.new2old import CampaignCartesianProduct

campaign = CampaignCartesianProduct(
    benchmark=MyBenchmark(),
    variables={"threads": [1, 2, 4]},
    command_attachments=[MyAttachment()],
)
campaign.run()
```

Attachments compose with command wrappers and shared libraries: the wrapped
command is what gets instrumented, so an attachment always observes the same
command line as the benchmark. As with wrappers, this only works for commands
that go through `ctx.exec(...)` (see the golden rule in §2).

Commands can also be fed standard input by passing a file path:

```python
out = ctx.exec(argv=["./solver", "config.txt"], stdin=Path("input.txt"))
```

The content of the file is read and forwarded to the command's standard input.
This works on the synchronous path as well as for attachment-instrumented
commands (local and SSH platforms).

---

## 13. Design philosophy

* explicit over implicit
* readable over clever
* testable without the engine
* minimal obligations for benchmark authors
* maximum flexibility for the execution engine

This API deliberately avoids:

* abstract base classes
* inheritance requirements
* DSLs
* hidden global state

A benchmark is just Python.

---

## 14. Recommended next steps

* start from an existing benchmark and port it step by step
* test stages independently using `.call()`
* add wrappers and attachments later, benchmarks do not need to care (see §12)
* keep parsing logic in collect, not in run

Happy benchmarking 🚀
