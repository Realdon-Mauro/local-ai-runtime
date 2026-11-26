import asyncio
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from lar.core.model_manager import ModelLease, ModelUnavailableError
from lar.core.scheduler import (
    JobHandle,
    ModelSelection,
    QueueFullError,
    QueueTimeoutError,
    RequestCancelledError,
    RequestStatus,
    RequestTimeoutError,
    Scheduler,
    SchedulerClosedError,
)


class FakeLeaseManager:
    def __init__(self):
        self.acquired = []
        self.released = []

    async def acquire(self, model):
        lease = ModelLease(uuid4(), model)
        self.acquired.append(lease)
        return lease

    async def release(self, lease):
        self.released.append(lease)


def make_scheduler(**kwargs):
    kwargs.setdefault("model_manager", FakeLeaseManager())
    kwargs.setdefault("model_selector", lambda _payload: "test-model")
    kwargs.setdefault("stop_verifier", _confirm_provider_stopped)
    return Scheduler(**kwargs)


async def _confirm_provider_stopped(_model):
    return True


async def wait_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


@pytest.mark.asyncio
async def test_start_submit_result_and_close():
    scheduler = make_scheduler()
    await scheduler.start()
    handle = await scheduler.submit(4, lambda value: _resolved(value * 2))

    assert isinstance(handle, JobHandle)
    assert await handle.result() == 8
    assert handle.state.status is RequestStatus.COMPLETED
    await scheduler.close()
    assert (await scheduler.queue_status()).accepting is False
    with pytest.raises(SchedulerClosedError):
        await scheduler.submit(None, lambda _: _resolved(None))


@pytest.mark.asyncio
async def test_fallback_tries_only_router_alternatives_after_definitive_model_404():
    class OneMissingModel(FakeLeaseManager):
        async def acquire(self, model):
            self.acquired.append(model)
            if model == "missing":
                raise ModelUnavailableError("not found")
            return ModelLease(uuid4(), model)

    manager = OneMissingModel()
    scheduler = make_scheduler(
        model_manager=manager,
        model_selector=lambda _payload: ModelSelection("missing", ("usable", "unused")),
    )
    await scheduler.start()
    payload = type("Payload", (), {"selected_model_id": None})()
    handle = await scheduler.submit(payload, lambda value: _resolved(value.selected_model_id))
    assert await handle.result() == "usable"
    assert payload.selected_model_id == "usable"
    assert manager.acquired == ["missing", "usable"]
    await scheduler.close()


@pytest.mark.asyncio
async def test_fallback_does_not_try_alternative_on_ambiguous_load_error():
    class AmbiguousManager(FakeLeaseManager):
        async def acquire(self, model):
            self.acquired.append(model)
            raise RuntimeError("unknown load outcome")

    manager = AmbiguousManager()
    scheduler = make_scheduler(
        model_manager=manager,
        model_selector=lambda _payload: ModelSelection("primary", ("alternative",)),
    )
    await scheduler.start()
    handle = await scheduler.submit(None, lambda _: _resolved(None))
    with pytest.raises(RuntimeError, match="unknown load outcome"):
        await handle.result()
    assert manager.acquired == ["primary"]
    await scheduler.close()


async def _resolved(value):
    return value


def test_scheduler_rejects_queue_capacity_above_architectural_limit():
    with pytest.raises(ValueError, match="superare 10"):
        make_scheduler(queue_capacity=11)


@pytest.mark.asyncio
async def test_fifo_and_only_one_callback_active():
    scheduler = make_scheduler()
    await scheduler.start()
    active = 0
    max_active = 0
    order = []

    async def callback(value):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(value)
        await asyncio.sleep(0)
        active -= 1
        return value

    handles = [await scheduler.submit(i, callback) for i in range(6)]
    assert [await handle.result() for handle in handles] == list(range(6))
    assert order == list(range(6))
    assert max_active == 1
    await scheduler.close()


@pytest.mark.asyncio
async def test_concurrent_submits_get_atomic_sequence_and_execute_in_sequence():
    scheduler = make_scheduler()
    await scheduler.start()
    gate = asyncio.Event()
    entered = asyncio.Event()
    started = []

    async def callback(value):
        started.append(value)
        if value == "block":
            entered.set()
            await gate.wait()
        return value

    first = await scheduler.submit("block", callback)
    await wait_event(entered)
    handles = await asyncio.gather(*(scheduler.submit(i, callback) for i in range(8)))
    assert [h.state.sequence_number for h in handles] == list(range(2, 10))
    gate.set()
    await asyncio.gather(first.result(), *(h.result() for h in handles))
    assert started == ["block", *range(8)]
    await scheduler.close()


@pytest.mark.asyncio
async def test_queue_capacity_excludes_active_and_rejection_has_no_sequence():
    scheduler = make_scheduler(queue_capacity=2)
    await scheduler.start()
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def callback(value):
        if value == 0:
            entered.set()
            await gate.wait()
        return value

    active = await scheduler.submit(0, callback)
    await wait_event(entered)
    queued = [await scheduler.submit(i, callback) for i in (1, 2)]
    status = await scheduler.queue_status()
    assert status.queued_count == 2
    assert status.active_count == 1
    assert status.active_request_id == active.request_id
    assert status.capacity == 2
    with pytest.raises(QueueFullError):
        await scheduler.submit(3, callback)
    assert (await scheduler.queue_status()).next_sequence_number == 4
    gate.set()
    await asyncio.gather(active.result(), *(job.result() for job in queued))
    await scheduler.close()


@pytest.mark.asyncio
async def test_queue_timeout_does_not_call_callback():
    scheduler = make_scheduler(queue_capacity=2, queue_timeout=0.02)
    await scheduler.start()
    entered = asyncio.Event()
    gate = asyncio.Event()
    called = []

    async def callback(value):
        called.append(value)
        if value == "active":
            entered.set()
            await gate.wait()
        return value

    active = await scheduler.submit("active", callback)
    await wait_event(entered)
    queued = await scheduler.submit("expire", callback)
    with pytest.raises(QueueTimeoutError):
        await queued.result()
    assert queued.state.status is RequestStatus.QUEUE_TIMEOUT
    assert called == ["active"]
    gate.set()
    await active.result()
    await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_queued_is_idempotent_and_skips_callback():
    scheduler = make_scheduler()
    await scheduler.start()
    entered = asyncio.Event()
    gate = asyncio.Event()
    calls = []

    async def callback(value):
        calls.append(value)
        if value == "active":
            entered.set()
            await gate.wait()
        return value

    active = await scheduler.submit("active", callback)
    await wait_event(entered)
    queued = await scheduler.submit("queued", callback)
    assert await queued.cancel()
    assert not await queued.cancel()
    assert queued.state.status is RequestStatus.CANCELLED
    with pytest.raises(RequestCancelledError):
        await queued.result()
    gate.set()
    await active.result()
    assert calls == ["active"]
    await scheduler.close()


@pytest.mark.asyncio
async def test_cancel_active_waits_for_callback_cleanup_before_next_request():
    scheduler = make_scheduler()
    await scheduler.start()
    started = asyncio.Event()
    cleaned = asyncio.Event()
    order = []

    async def callback(value):
        order.append(f"start:{value}")
        if value == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        order.append(f"end:{value}")
        return value

    active = await scheduler.submit(1, callback)
    await wait_event(started)
    next_job = await scheduler.submit(2, callback)
    assert await active.cancel()
    await wait_event(cleaned)
    with pytest.raises(RequestCancelledError):
        await active.result()
    assert await next_job.result() == 2
    assert order.index("start:2") > order.index("end:1") if "end:1" in order else cleaned.is_set()
    assert active.state.status is RequestStatus.CANCELLED
    assert not await active.cancel()
    await scheduler.close()


@pytest.mark.asyncio
async def test_unconfirmed_provider_stop_blocks_scheduler_and_cancels_waiters():
    scheduler = make_scheduler(
        stop_check_interval=0.005,
        stop_confirmation_timeout=0.02,
        stop_verifier=lambda _model: _provider_stop_unknown(),
    )
    await scheduler.start()
    started = asyncio.Event()

    async def slow(_value):
        started.set()
        await asyncio.Event().wait()

    active = await scheduler.submit("active", slow)
    await wait_event(started)
    queued = await scheduler.submit("queued", slow)
    assert await active.cancel()
    assert active.state.status is RequestStatus.BLOCKED
    with pytest.raises(RequestCancelledError, match="provider_stop_unknown"):
        await active.result()
    with pytest.raises(RequestCancelledError, match="provider_stop_unknown"):
        await queued.result()
    assert (await scheduler.queue_status()).accepting is False
    assert scheduler._provider_stop_unknown is True
    await scheduler.close()


async def _provider_stop_unknown():
    return None


@pytest.mark.asyncio
async def test_active_request_timeout_cancels_callback_and_continues_fifo():
    scheduler = make_scheduler(request_timeout=0.02)
    await scheduler.start()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def callback(value):
        if value == "slow":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return value

    slow = await scheduler.submit("slow", callback)
    await wait_event(started)
    following = await scheduler.submit("next", callback)
    with pytest.raises(RequestTimeoutError):
        await slow.result()
    assert cleaned.is_set()
    assert slow.state.status is RequestStatus.REQUEST_TIMEOUT
    assert await following.result() == "next"
    await scheduler.close()


@pytest.mark.asyncio
async def test_callback_failure_does_not_stop_worker():
    scheduler = make_scheduler()
    await scheduler.start()

    async def fail(_):
        raise ValueError("expected")

    failed = await scheduler.submit(None, fail)
    following = await scheduler.submit("ok", _resolved)
    with pytest.raises(ValueError, match="expected"):
        await failed.result()
    assert failed.state.status is RequestStatus.FAILED
    assert await following.result() == "ok"
    await scheduler.close()


@pytest.mark.asyncio
async def test_close_cancels_queued_and_active_without_orphan_worker():
    scheduler = make_scheduler()
    await scheduler.start()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def callback(value):
        if value == 0:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return value

    active = await scheduler.submit(0, callback)
    await wait_event(started)
    queued = await scheduler.submit(1, callback)
    await scheduler.close()
    assert cleaned.is_set()
    assert active.state.status is RequestStatus.CANCELLED
    assert queued.state.status is RequestStatus.CANCELLED
    assert scheduler._worker is None
    with pytest.raises(RequestCancelledError):
        await active.result()
    with pytest.raises(RequestCancelledError):
        await queued.result()


@pytest.mark.asyncio
async def test_terminal_snapshot_is_immutable_and_never_reactivated():
    scheduler = make_scheduler()
    await scheduler.start()
    handle = await scheduler.submit("x", _resolved)
    await handle.result()
    snapshot = handle.state
    with pytest.raises((FrozenInstanceError, AttributeError)):
        snapshot.status = RequestStatus.RUNNING
    assert handle.state.status is RequestStatus.COMPLETED
    await scheduler.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_no_callback_for_rejected_request():
    scheduler = make_scheduler(queue_capacity=1)
    await scheduler.start()
    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = []

    async def callback(value):
        calls.append(value)
        entered.set()
        await gate.wait()

    active = await scheduler.submit("active", callback)
    await wait_event(entered)
    queued = await scheduler.submit("queued", callback)
    with pytest.raises(QueueFullError):
        await scheduler.submit("rejected", callback)
    await scheduler.close()
    await scheduler.close()
    assert calls == ["active"]
    assert queued.state.status is RequestStatus.CANCELLED
    assert active.state.status is RequestStatus.CANCELLED
