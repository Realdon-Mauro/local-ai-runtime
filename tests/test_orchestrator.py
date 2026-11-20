from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from lar.evaluation.orchestrator import BenchmarkOrchestrator


class FakeModelManager:
    def __init__(self):
        self.active_leases = 0
        self.lease_count = 0
        self.unload_calls = []

    async def unload(self, model):
        self.unload_calls.append(model)

    @asynccontextmanager
    async def lease(self, model):
        self.active_leases += 1
        self.lease_count += 1

        try:
            yield SimpleNamespace(name=model)
        finally:
            self.active_leases -= 1


class FakeRunner:
    def __init__(self, manager=None, error=None):
        self.manager = manager
        self.error = error
        self.calls = []
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0

    async def run(self, model, benchmark):
        self.calls.append((model, benchmark))
        self.concurrent_calls += 1
        self.max_concurrent_calls = max(
            self.max_concurrent_calls,
            self.concurrent_calls,
        )

        try:
            if self.manager is not None:
                assert self.manager.active_leases == 1

            if self.error:
                raise self.error

            return SimpleNamespace(
                model=model,
                benchmark_id=benchmark.id,
                status="passed",
            )
        finally:
            self.concurrent_calls -= 1


@pytest.mark.asyncio
async def test_orchestrator_runs_benchmark_inside_model_lease():
    manager = FakeModelManager()
    runner = FakeRunner(manager)
    orchestrator = BenchmarkOrchestrator(manager, runner)
    benchmark = SimpleNamespace(id="coding.test")

    result = await orchestrator.run("test-model", benchmark)

    assert result.status == "passed"
    assert manager.lease_count == 1
    assert manager.active_leases == 0
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_orchestrator_releases_lease_after_runner_error():
    manager = FakeModelManager()
    runner = FakeRunner(
        manager,
        error=RuntimeError("Errore simulato"),
    )
    orchestrator = BenchmarkOrchestrator(manager, runner)
    benchmark = SimpleNamespace(id="coding.test")

    with pytest.raises(RuntimeError, match="Errore simulato"):
        await orchestrator.run("test-model", benchmark)

    assert manager.active_leases == 0


@pytest.mark.asyncio
async def test_orchestrator_runs_many_benchmarks_sequentially():
    manager = FakeModelManager()
    runner = FakeRunner(manager)
    orchestrator = BenchmarkOrchestrator(manager, runner)

    benchmarks = [
        SimpleNamespace(id="coding.one"),
        SimpleNamespace(id="coding.two"),
        SimpleNamespace(id="coding.three"),
    ]

    results = await orchestrator.run_many("test-model", benchmarks)

    assert len(results) == 3
    assert [result.benchmark_id for result in results] == [
        "coding.one",
        "coding.two",
        "coding.three",
    ]
    assert manager.lease_count == 3
    assert manager.active_leases == 0
    assert runner.max_concurrent_calls == 1


class FakeStore:
    def __init__(self, manager=None):
        self.manager = manager
        self.saved = []

    def save_result(self, result):
        if self.manager is not None:
            assert self.manager.active_leases == 0

        self.saved.append(result)
        return len(self.saved)


@pytest.mark.asyncio
async def test_orchestrator_saves_result_after_releasing_lease():
    manager = FakeModelManager()
    runner = FakeRunner(manager)
    store = FakeStore(manager)
    orchestrator = BenchmarkOrchestrator(manager, runner, store)
    benchmark = SimpleNamespace(id="coding.test")

    result = await orchestrator.run("test-model", benchmark)

    assert manager.active_leases == 0
    assert len(store.saved) == 1
    assert store.saved[0] is result


@pytest.mark.asyncio
async def test_orchestrator_saves_each_result_in_run_many():
    manager = FakeModelManager()
    runner = FakeRunner(manager)
    store = FakeStore(manager)
    orchestrator = BenchmarkOrchestrator(manager, runner, store)

    benchmarks = [
        SimpleNamespace(id="coding.one"),
        SimpleNamespace(id="coding.two"),
        SimpleNamespace(id="coding.three"),
    ]

    results = await orchestrator.run_many("test-model", benchmarks)

    assert len(results) == 3
    assert len(store.saved) == 3
    assert store.saved == results


@pytest.mark.asyncio
async def test_orchestrator_works_without_store():
    manager = FakeModelManager()
    runner = FakeRunner(manager)
    orchestrator = BenchmarkOrchestrator(manager, runner)
    benchmark = SimpleNamespace(id="coding.test")

    result = await orchestrator.run("test-model", benchmark)

    assert result.status == "passed"
    assert manager.active_leases == 0


class FakeBatchStore:
    def __init__(self, manager):
        self.manager = manager
        self.saved = []

    def save_result(self, result):
        assert self.manager.active_leases == 1
        self.saved.append(result)
        return len(self.saved)


@pytest.mark.asyncio
async def test_batch_uses_one_lease_and_saves_each_result():
    manager = FakeModelManager()
    runner = FakeRunner(manager)
    store = FakeBatchStore(manager)
    orchestrator = BenchmarkOrchestrator(manager, runner, store)

    benchmarks = [
        SimpleNamespace(id="coding.one"),
        SimpleNamespace(id="coding.two"),
        SimpleNamespace(id="coding.three"),
    ]

    results = await orchestrator.run_batch_for_model(
        "test-model",
        benchmarks,
    )

    assert len(results) == 3
    assert manager.lease_count == 1
    assert manager.active_leases == 0
    assert manager.unload_calls == ["test-model"]
    assert store.saved == results
    assert runner.max_concurrent_calls == 1


@pytest.mark.asyncio
async def test_batch_releases_and_unloads_after_runner_error():
    manager = FakeModelManager()
    runner = FakeRunner(
        manager,
        error=RuntimeError("Errore simulato"),
    )
    orchestrator = BenchmarkOrchestrator(manager, runner)

    benchmarks = [
        SimpleNamespace(id="coding.one"),
        SimpleNamespace(id="coding.two"),
    ]

    with pytest.raises(RuntimeError, match="Errore simulato"):
        await orchestrator.run_batch_for_model("test-model", benchmarks)

    assert manager.active_leases == 0
    assert manager.unload_calls == ["test-model"]
    assert len(runner.calls) == 1
