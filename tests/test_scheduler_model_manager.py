import asyncio
from uuid import UUID

import pytest

from lar.core.model_manager import ModelLease, ModelManager
from lar.core.scheduler import (
    QueueTimeoutError,
    RequestCancelledError,
    RequestStatus,
    RequestTimeoutError,
    Scheduler,
)
from lar.providers.base import GenerationResponse, GenerationUsage


async def wait_event(event):
    await asyncio.wait_for(event.wait(), timeout=1)


class FakeOllama:
    def __init__(self, names=("model-a",)):
        self.running = list(names)
        self.unload_calls = []
        self.list_entered = None
        self.list_gate = None

    async def list_running(self):
        if self.list_entered is not None:
            self.list_entered.set()
        if self.list_gate is not None:
            await self.list_gate.wait()
        return [{"name": name} for name in self.running]

    async def load_model(self, model):
        self.running.append(model)
        return {"done": True}

    async def unload_model(self, model):
        self.unload_calls.append(model)


class TrackingManager:
    def __init__(self):
        self.acquired = []
        self.released = []
        self.release_entered = asyncio.Event()
        self.release_gate = None
        self.acquire_gate = None
        self.acquire_entered = asyncio.Event()
        self.acquire_error = None

    async def acquire(self, model):
        self.acquire_entered.set()
        if self.acquire_gate is not None:
            await self.acquire_gate.wait()
        if self.acquire_error:
            raise self.acquire_error
        lease = ModelLease(UUID(int=len(self.acquired) + 1), model)
        self.acquired.append(lease)
        return lease

    async def release(self, lease):
        self.release_entered.set()
        if self.release_gate is not None:
            await self.release_gate.wait()
        self.released.append(lease)


def build_scheduler(manager, *, selector=None, **kwargs):
    async def confirm_stopped(_model):
        return True

    return Scheduler(
        model_manager=manager,
        model_selector=selector or (lambda _: "model-a"),
        stop_verifier=confirm_stopped,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_real_model_manager_acquires_and_releases_identifiable_lease():
    provider = FakeOllama()
    manager = ModelManager(provider)
    scheduler = build_scheduler(manager, selector=lambda payload: payload["model"])
    await scheduler.start()

    async def callback(payload):
        status = await scheduler.queue_status()
        assert status.active_request_id == "request-42"
        state = manager.get_states()[0]
        assert state.active_requests == 1
        return payload["value"]

    handle = await scheduler.submit(
        {"model": "model-a", "value": "ok"}, callback, request_id="request-42"
    )
    assert await handle.result() == "ok"
    assert handle.state.status is RequestStatus.COMPLETED
    assert handle.state.selected_model == "model-a"
    assert handle.state.lease_id is not None
    assert manager.get_states()[0].active_requests == 0
    assert provider.unload_calls == []
    await scheduler.close()


@pytest.mark.asyncio
async def test_callback_error_releases_lease_once_then_next_runs():
    manager = TrackingManager()
    scheduler = build_scheduler(manager)
    await scheduler.start()

    async def fail(_):
        raise ValueError("callback failed")

    first = await scheduler.submit(1, fail)
    second = await scheduler.submit(2, _return)
    with pytest.raises(ValueError, match="callback failed"):
        await first.result()
    assert await second.result() == 2
    assert first.state.status is RequestStatus.FAILED
    assert len(manager.acquired) == len(manager.released) == 2
    assert manager.released[0] == manager.acquired[0]
    await scheduler.close()


@pytest.mark.asyncio
async def test_provider_generates_selected_model_under_lease_and_cleans_before_fifo_next():
    manager = TrackingManager()
    manager.release_gate = asyncio.Event()
    calls = []
    first_entered = asyncio.Event()

    class FakeGenerationProvider:
        async def generate(self, model, prompt, system=None, options=None):
            calls.append((model, prompt, system, options))
            assert len(manager.acquired) == 1
            assert manager.released == []
            first_entered.set()
            return GenerationResponse(prompt.upper(), GenerationUsage(), {})

    scheduler = Scheduler(
        model_manager=manager,
        model_selector=lambda payload: payload["selected"],
        provider=FakeGenerationProvider(),
    )
    await scheduler.start()
    first = await scheduler.submit({
        "selected": "chosen-model", "prompt": "hello", "system": "sys",
        "options": {"temperature": 0},
    })
    await wait_event(first_entered)
    second_started = asyncio.Event()

    async def second_callback(_):
        second_started.set()
        return "next"

    second = await scheduler.submit({"selected": "model-a"}, second_callback)
    await asyncio.sleep(0)
    assert not second_started.is_set()
    manager.release_gate.set()
    result = await first.result()
    assert result.text == "HELLO"
    assert calls == [("chosen-model", "hello", "sys", {"temperature": 0})]
    assert await second.result() == "next"
    assert manager.released == manager.acquired
    await scheduler.close()


@pytest.mark.asyncio
async def test_provider_error_propagates_without_retry_and_releases_lease():
    manager = TrackingManager()
    error = RuntimeError("provider failure")

    class FailingProvider:
        calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            raise error

    provider = FailingProvider()
    scheduler = Scheduler(
        model_manager=manager,
        model_selector=lambda _: "model-a",
        provider=provider,
    )
    await scheduler.start()
    request = await scheduler.submit({"prompt": "hello"})
    with pytest.raises(RuntimeError, match="provider failure"):
        await request.result()
    assert provider.calls == 1
    assert manager.released == manager.acquired
    await scheduler.close()


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_cancel_during_lease_cleanup_waits_before_next_callback():
    manager = TrackingManager()
    manager.release_gate = asyncio.Event()
    scheduler = build_scheduler(manager)
    await scheduler.start()
    callback_finished = asyncio.Event()
    next_started = asyncio.Event()

    async def first_callback(_):
        callback_finished.set()
        return "first"

    async def second_callback(_):
        next_started.set()
        return "second"

    first = await scheduler.submit(1, first_callback)
    await wait_event(callback_finished)
    await wait_event(manager.release_entered)
    second = await scheduler.submit(2, second_callback)
    cancelling = asyncio.create_task(first.cancel())
    await asyncio.sleep(0)
    assert not next_started.is_set()
    manager.release_gate.set()
    assert await cancelling
    with pytest.raises(RequestCancelledError):
        await first.result()
    assert await second.result() == "second"
    assert next_started.is_set()
    assert manager.released == manager.acquired
    await scheduler.close()


@pytest.mark.asyncio
async def test_active_timeout_releases_lease():
    manager = TrackingManager()
    scheduler = build_scheduler(manager, request_timeout=0.02)
    await scheduler.start()
    started = asyncio.Event()

    async def slow(_):
        started.set()
        await asyncio.Event().wait()

    handle = await scheduler.submit(None, slow)
    await wait_event(started)
    with pytest.raises(RequestTimeoutError):
        await handle.result()
    assert handle.state.status is RequestStatus.REQUEST_TIMEOUT
    assert len(manager.acquired) == len(manager.released) == 1
    await scheduler.close()


@pytest.mark.asyncio
async def test_acquire_error_or_timeout_never_releases_a_phantom_lease():
    manager = TrackingManager()
    manager.acquire_error = RuntimeError("acquire failed")
    scheduler = build_scheduler(manager)
    await scheduler.start()
    failed = await scheduler.submit(None, _return)
    with pytest.raises(RuntimeError, match="acquire failed"):
        await failed.result()
    assert manager.acquired == manager.released == []
    await scheduler.close()

    manager = TrackingManager()
    manager.acquire_gate = asyncio.Event()
    scheduler = build_scheduler(manager, request_timeout=0.02)
    await scheduler.start()
    timed_out = await scheduler.submit(None, _return)
    await wait_event(manager.acquire_entered)
    with pytest.raises(RequestTimeoutError):
        await timed_out.result()
    assert manager.acquired == manager.released == []
    await scheduler.close()


@pytest.mark.asyncio
async def test_queued_cancel_and_timeout_do_not_acquire_lease():
    manager = TrackingManager()
    scheduler = build_scheduler(manager, queue_capacity=2, queue_timeout=0.02)
    await scheduler.start()
    active_started = asyncio.Event()
    gate = asyncio.Event()

    async def block(_):
        active_started.set()
        await gate.wait()

    active = await scheduler.submit("active", block)
    await wait_event(active_started)
    cancelled = await scheduler.submit("cancelled", _return)
    expired = await scheduler.submit("expired", _return)
    assert await cancelled.cancel()
    with pytest.raises(RequestCancelledError):
        await cancelled.result()
    with pytest.raises(QueueTimeoutError):
        await expired.result()
    assert len(manager.acquired) == 1
    gate.set()
    await active.result()
    assert len(manager.released) == 1
    await scheduler.close()


@pytest.mark.asyncio
async def test_selector_error_has_no_lease_and_fifo_continues():
    manager = TrackingManager()

    def selector(payload):
        if payload == "bad":
            raise ValueError("no model")
        return "model-a"

    scheduler = build_scheduler(manager, selector=selector)
    await scheduler.start()
    first = await scheduler.submit("bad", _return)
    second = await scheduler.submit("good", _return)
    with pytest.raises(ValueError, match="no model"):
        await first.result()
    assert await second.result() == "good"
    assert first.state.status is RequestStatus.FAILED
    assert len(manager.acquired) == len(manager.released) == 1
    await scheduler.close()


@pytest.mark.asyncio
async def test_reconciliation_does_not_change_active_lease_or_unload():
    provider = FakeOllama()
    manager = ModelManager(provider)
    scheduler = build_scheduler(manager)
    await scheduler.start()
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def callback(_):
        entered.set()
        await gate.wait()
        return "done"

    handle = await scheduler.submit(None, callback)
    await wait_event(entered)
    assert manager.get_states()[0].active_requests == 1
    result = await manager.reconcile()
    assert result.errors == ()
    assert manager.get_states()[0].active_requests == 1
    assert provider.unload_calls == []
    gate.set()
    await handle.result()
    assert manager.get_states()[0].active_requests == 0
    await scheduler.close()


@pytest.mark.asyncio
async def test_close_waits_until_acquired_lease_is_released():
    manager = TrackingManager()
    manager.release_gate = asyncio.Event()
    scheduler = build_scheduler(manager)
    await scheduler.start()
    entered = asyncio.Event()

    async def callback(_):
        entered.set()
        await asyncio.Event().wait()

    handle = await scheduler.submit(None, callback)
    await wait_event(entered)
    closing = asyncio.create_task(scheduler.close())
    await wait_event(manager.release_entered)
    assert not closing.done()
    manager.release_gate.set()
    await closing
    assert manager.acquired == manager.released
    assert handle.state.status is RequestStatus.CANCELLED
    assert (await scheduler.queue_status()).active_count == 0


@pytest.mark.asyncio
async def test_slow_reconciliation_delays_acquire_without_changing_fifo():
    provider = FakeOllama()
    provider.list_entered = asyncio.Event()
    provider.list_gate = asyncio.Event()
    manager = ModelManager(provider)
    reconciliation = asyncio.create_task(manager.reconcile())
    await wait_event(provider.list_entered)

    scheduler = build_scheduler(manager)
    await scheduler.start()
    order = []

    async def callback(value):
        order.append(value)
        return value

    first = await scheduler.submit("first", callback)
    second = await scheduler.submit("second", callback)
    await asyncio.sleep(0)
    assert order == []
    provider.list_gate.set()
    await reconciliation
    assert await first.result() == "first"
    assert await second.result() == "second"
    assert order == ["first", "second"]
    await scheduler.close()
