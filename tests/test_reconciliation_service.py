import asyncio

import pytest

from lar.core.model_manager import ReconciliationResult
from lar.core.reconciliation_service import ReconciliationService


class FakeModelManager:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.entered = asyncio.Event()
        self.gate = None
        self.lock = asyncio.Lock()

    async def reconcile(self):
        async with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()
            try:
                if self.gate is not None:
                    await self.gate.wait()
                outcome = (
                    self.outcomes.pop(0)
                    if self.outcomes
                    else ReconciliationResult((), (), ())
                )
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            finally:
                self.active -= 1


class ControlledWait:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.intervals = []

    async def __call__(self, stop_event, interval):
        self.intervals.append(interval)
        self.entered.set()
        await self.release.wait()
        self.release.clear()
        self.entered.clear()


async def wait_for_event(event):
    await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.asyncio
async def test_start_runs_immediately_and_exposes_minimal_status():
    manager = FakeModelManager()
    service = ReconciliationService(manager)

    await service.start()
    await wait_for_event(manager.entered)
    await service.stop()

    assert manager.calls == 1
    assert service.running is False
    assert service.last_error is None


@pytest.mark.asyncio
async def test_repeats_on_configurable_interval_without_real_waits():
    manager = FakeModelManager()
    wait = ControlledWait()
    service = ReconciliationService(manager, 17, wait=wait)

    await service.start()
    await wait_for_event(wait.entered)
    assert manager.calls == 1
    wait.release.set()
    await wait_for_event(manager.entered)
    # The event remains set; yield until the second call is observed.
    for _ in range(10):
        if manager.calls >= 2:
            break
        await asyncio.sleep(0)
    await service.stop()

    assert manager.calls == 2
    assert wait.intervals == [17, 17]


@pytest.mark.asyncio
async def test_duplicate_start_does_not_create_another_loop():
    manager = FakeModelManager()
    wait = ControlledWait()
    service = ReconciliationService(manager, wait=wait)

    await service.start()
    await wait_for_event(wait.entered)
    task = service._task
    await service.start()
    await asyncio.sleep(0)
    await service.stop()

    assert service._task is None
    assert manager.calls == 1
    assert task.done()


@pytest.mark.asyncio
async def test_stop_during_wait_joins_task_and_allows_restart():
    manager = FakeModelManager()
    wait = ControlledWait()
    service = ReconciliationService(manager, wait=wait)

    await service.start()
    await wait_for_event(wait.entered)
    await service.stop()
    assert service.running is False
    assert service._task is None

    manager.entered.clear()
    await service.start()
    await wait_for_event(manager.entered)
    await service.stop()
    assert manager.calls == 2


@pytest.mark.asyncio
async def test_stop_during_reconciliation_cancels_and_joins_without_orphan():
    manager = FakeModelManager()
    manager.gate = asyncio.Event()
    service = ReconciliationService(manager)

    await service.start()
    await wait_for_event(manager.entered)
    task = service._task
    await service.stop()

    assert task.done()
    assert task.cancelled()
    assert service._task is None
    assert manager.active == 0


@pytest.mark.asyncio
async def test_error_does_not_end_loop_and_is_cleared_after_success():
    manager = FakeModelManager(
        [RuntimeError("sensitive detail"), ReconciliationResult((), (), ())]
    )
    wait = ControlledWait()
    service = ReconciliationService(manager, wait=wait)

    await service.start()
    await wait_for_event(wait.entered)
    for _ in range(10):
        if service.last_error:
            break
        await asyncio.sleep(0)
    assert service.last_error == "RuntimeError"
    assert "sensitive" not in service.last_error
    wait.release.set()
    # Wait for the second execution and stop it cleanly during the next wait.
    for _ in range(20):
        if manager.calls >= 2 and service.last_error is None:
            break
        await asyncio.sleep(0)
    await service.stop()

    assert manager.calls == 2
    assert service.last_error is None


@pytest.mark.asyncio
async def test_reconciliation_calls_never_overlap_and_result_errors_are_observable():
    manager = FakeModelManager(
        [ReconciliationResult((), (), (), errors=("secret",))]
    )
    wait = ControlledWait()
    service = ReconciliationService(manager, wait=wait)

    await service.start()
    await wait_for_event(wait.entered)
    for _ in range(10):
        if service.last_error:
            break
        await asyncio.sleep(0)
    await service.stop()

    assert manager.max_active == 1
    assert service.last_error == "Reconciliation reported errors"
    assert "secret" not in service.last_error


@pytest.mark.asyncio
async def test_cancellation_of_service_task_propagates_from_task():
    manager = FakeModelManager()
    manager.gate = asyncio.Event()
    service = ReconciliationService(manager)

    await service.start()
    await wait_for_event(manager.entered)
    task = service._task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Join and clear the already-cancelled task.
    await service.stop()
    assert service._task is None
    assert manager.active == 0


@pytest.mark.asyncio
async def test_on_demand_calls_share_manager_and_service_never_unloads():
    manager = FakeModelManager()
    wait = ControlledWait()
    service = ReconciliationService(manager, wait=wait)
    manager.gate = asyncio.Event()

    await service.start()
    await wait_for_event(manager.entered)
    on_demand = asyncio.create_task(manager.reconcile())
    await asyncio.sleep(0)
    assert manager.calls == 1
    manager.gate.set()
    await on_demand
    await service.stop()

    assert manager.calls == 2
    assert manager.max_active == 1
