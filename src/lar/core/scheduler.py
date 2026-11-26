"""Bounded FIFO scheduler for in-process asynchronous request execution."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from lar.core.model_manager import ModelLease, ModelUnavailableError
from lar.providers.base import GenerationProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")
ExecutionCallback = Callable[[Any], Awaitable[T]]
StopVerifier = Callable[[str], Awaitable[bool | None]]


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """Primary and router-approved alternatives for a pre-inference load fallback."""

    primary: str
    alternatives: tuple[str, ...] = ()


ModelSelector = Callable[[Any], str | ModelSelection | Awaitable[str | ModelSelection]]


class LeaseManager(Protocol):
    async def acquire(self, model: str) -> ModelLease: ...

    async def release(self, lease: ModelLease) -> Any: ...


class SchedulerError(Exception):
    """Base exception for scheduler lifecycle and request errors."""


class QueueFullError(SchedulerError):
    """Raised when the bounded waiting queue has no free slot."""


class SchedulerClosedError(SchedulerError):
    """Raised when submitting to a scheduler that is not accepting work."""


class RequestCancelledError(SchedulerError):
    """Raised when a cancelled job's result is awaited."""


class QueueTimeoutError(SchedulerError):
    """Raised when a request is not started before its queue deadline."""


class RequestTimeoutError(SchedulerError):
    """Raised when execution exceeds its admitted-request deadline."""


class RequestStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUEUE_TIMEOUT = "queue_timeout"
    REQUEST_TIMEOUT = "request_timeout"
    REJECTED = "rejected"
    PROVIDER_STOP_UNKNOWN = "provider_stop_unknown"
    BLOCKED = "blocked"


_TERMINAL = frozenset(
    {
        RequestStatus.COMPLETED,
        RequestStatus.FAILED,
        RequestStatus.CANCELLED,
        RequestStatus.QUEUE_TIMEOUT,
        RequestStatus.REQUEST_TIMEOUT,
        RequestStatus.REJECTED,
        RequestStatus.BLOCKED,
    }
)


@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    """Immutable, payload-free view of a request's scheduler state."""

    request_id: str
    sequence_number: int
    submitted_at: datetime
    queued_at: datetime
    deadline: float
    status: RequestStatus
    error: str | None = None
    selected_model: str | None = None
    lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class QueueStatus:
    """Immutable scheduler status without request payloads."""

    queued_count: int
    capacity: int
    active_count: int
    active_request_id: str | None
    next_sequence_number: int
    accepting: bool
    worker_running: bool
    provider_stop_unknown: bool = False


@dataclass(slots=True)
class _Job[T]:
    request_id: str
    sequence_number: int
    submitted_at: datetime
    queued_at: datetime
    deadline: float
    payload: Any
    callback: ExecutionCallback[T]
    future: asyncio.Future[T]
    status: RequestStatus = RequestStatus.RECEIVED
    error: str | None = None
    execution_task: asyncio.Task[T] | None = None
    queue_timer: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    cancel_reason: str = "client_cancelled"
    selected_model: str | None = None
    lease: ModelLease | None = None

    def snapshot(self) -> RequestSnapshot:
        return RequestSnapshot(
            request_id=self.request_id,
            sequence_number=self.sequence_number,
            submitted_at=self.submitted_at,
            queued_at=self.queued_at,
            deadline=self.deadline,
            status=self.status,
            error=self.error,
            selected_model=self.selected_model,
            lease_id=str(self.lease.id) if self.lease is not None else None,
        )


class JobHandle[T]:
    """Caller-facing handle; internal payload and mutable job record stay private."""

    def __init__(self, scheduler: Scheduler, job: _Job[T]) -> None:
        self._scheduler = scheduler
        self._job = job

    @property
    def request_id(self) -> str:
        return self._job.request_id

    @property
    def state(self) -> RequestSnapshot:
        return self._job.snapshot()

    async def result(self) -> T:
        return await asyncio.shield(self._job.future)

    async def cancel(self, reason: str = "client_cancelled") -> bool:
        return await self._scheduler.cancel(self.request_id, reason=reason)


class Scheduler:
    """Single-worker bounded FIFO scheduler with per-queue and execution deadlines."""

    def __init__(
        self,
        *,
        queue_capacity: int = 10,
        queue_timeout: float = 120.0,
        request_timeout: float = 500.0,
        model_manager: LeaseManager,
        model_selector: ModelSelector,
        provider: GenerationProvider | None = None,
        stop_verifier: StopVerifier | None = None,
        stop_check_interval: float = 5.0,
        stop_confirmation_timeout: float = 60.0,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity deve essere almeno 1.")
        if queue_capacity > 10:
            raise ValueError("queue_capacity non può superare 10 richieste in attesa.")
        if queue_timeout <= 0 or request_timeout <= 0:
            raise ValueError("I timeout devono essere maggiori di zero.")
        if stop_check_interval <= 0 or stop_confirmation_timeout <= 0:
            raise ValueError("I timeout di verifica stop devono essere positivi.")
        self.queue_capacity = queue_capacity
        self.queue_timeout = queue_timeout
        self.request_timeout = request_timeout
        self.model_manager = model_manager
        self.model_selector = model_selector
        self.provider = provider
        self.stop_verifier = stop_verifier
        self.stop_check_interval = stop_check_interval
        self.stop_confirmation_timeout = stop_confirmation_timeout
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._queue: deque[_Job[Any]] = deque()
        self._jobs: dict[str, _Job[Any]] = {}
        self._next_sequence_number = 1
        self._queued_count = 0
        self._active: _Job[Any] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._accepting = False
        self._closing = False
        self._provider_stop_unknown = False

    async def start(self) -> None:
        """Start the sole worker. Repeated starts are harmless."""
        async with self._lock:
            if self._closing or self._provider_stop_unknown:
                raise SchedulerClosedError("Lo Scheduler è già stato chiuso.")
            if self._worker is not None and not self._worker.done():
                self._accepting = True
                return
            self._accepting = True
            self._worker = asyncio.create_task(self._run_worker(), name="lar-scheduler-worker")
            self._wake.set()

    async def submit(
        self,
        payload: Any,
        callback: ExecutionCallback[T] | None = None,
        *,
        request_id: str | None = None,
    ) -> JobHandle[T]:
        """Atomically admit a request and assign its FIFO sequence number."""
        if callback is not None and not callable(callback):
            raise TypeError("callback deve essere callable.")
        if callback is None and self.provider is None:
            raise TypeError("È necessario un callback o un provider di generazione.")
        async with self._lock:
            if not self._accepting or self._closing or self._provider_stop_unknown:
                raise SchedulerClosedError("Lo Scheduler non accetta richieste.")
            if self._queued_count >= self.queue_capacity:
                raise QueueFullError("La coda FIFO è piena.")
            now = asyncio.get_running_loop().time()
            wall_now = datetime.now(UTC)
            identifier = request_id or str(uuid4())
            if identifier in self._jobs:
                raise ValueError(f"request_id già usato: {identifier}")
            loop = asyncio.get_running_loop()
            job: _Job[T] = _Job(
                request_id=identifier,
                sequence_number=self._next_sequence_number,
                submitted_at=wall_now,
                queued_at=wall_now,
                deadline=now + self.queue_timeout,
                payload=payload,
                callback=callback,
                future=loop.create_future(),
                status=RequestStatus.QUEUED,
            )
            self._next_sequence_number += 1
            self._jobs[identifier] = job
            self._queue.append(job)
            self._queued_count += 1
            job.queue_timer = asyncio.create_task(
                self._expire_queued(job), name=f"lar-queue-timeout-{identifier}"
            )
            self._wake.set()
            return JobHandle(self, job)

    async def cancel(self, request_id: str, *, reason: str = "client_cancelled") -> bool:
        """Cancel queued or active work idempotently and wait for active cleanup."""
        async with self._lock:
            job = self._jobs.get(request_id)
            if job is None or job.status in _TERMINAL:
                return False
            if job.status is RequestStatus.QUEUED:
                self._queued_count -= 1
                job.status = RequestStatus.CANCELLED
                job.error = reason
                if job.queue_timer is not None:
                    job.queue_timer.cancel()
                self._settle_exception(job, RequestCancelledError(reason))
                self._wake.set()
                return True
            if job is not self._active or job.status not in {
                RequestStatus.RUNNING,
                RequestStatus.FINALIZING,
            }:
                return False
            job.cancel_requested = True
            job.cancel_reason = reason
            task = job.execution_task
            if task is not None and not task.done():
                task.cancel(reason)
            future = job.future
        await asyncio.wait((future,))
        return True

    async def queue_status(self) -> QueueStatus:
        async with self._lock:
            worker = self._worker
            return QueueStatus(
                queued_count=self._queued_count,
                capacity=self.queue_capacity,
                active_count=int(self._active is not None),
                active_request_id=self._active.request_id if self._active else None,
                next_sequence_number=self._next_sequence_number,
                accepting=self._accepting and not self._closing,
                worker_running=worker is not None and not worker.done(),
                provider_stop_unknown=self._provider_stop_unknown,
            )

    async def close(self, *, drain: bool = False, grace_period: float = 0.0) -> None:
        """Stop accepting, cancel queued work, cancel active work and join the worker."""
        if grace_period < 0:
            raise ValueError("Il periodo di grazia non può essere negativo.")
        async with self._lock:
            if self._closing and self._worker is None:
                return
            self._accepting = False
        if drain and grace_period:
            deadline = asyncio.get_running_loop().time() + grace_period
            while True:
                status = await self.queue_status()
                if status.queued_count == 0 and status.active_count == 0:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.05, remaining))
        async with self._lock:
            self._closing = True
            for job in self._queue:
                if job.status is RequestStatus.QUEUED:
                    job.status = RequestStatus.CANCELLED
                    job.error = "scheduler_shutdown"
                    self._settle_exception(job, RequestCancelledError(job.error))
                    if job.queue_timer is not None:
                        job.queue_timer.cancel()
            self._queued_count = 0
            if self._active is not None:
                self._active.cancel_requested = True
                self._active.cancel_reason = "scheduler_shutdown"
                task = self._active.execution_task
                if task is not None and not task.done():
                    task.cancel("scheduler_shutdown")
            self._wake.set()
            worker = self._worker
        if worker is not None:
            await asyncio.shield(worker)
        timers = [job.queue_timer for job in self._jobs.values() if job.queue_timer]
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        async with self._lock:
            if self._worker is worker:
                self._worker = None

    async def _expire_queued(self, job: _Job[Any]) -> None:
        try:
            delay = max(0.0, job.deadline - asyncio.get_running_loop().time())
            await asyncio.sleep(delay)
            async with self._lock:
                if job.status is not RequestStatus.QUEUED:
                    return
                job.status = RequestStatus.QUEUE_TIMEOUT
                job.error = "queue_timeout"
                self._queued_count -= 1
                self._settle_exception(job, QueueTimeoutError(job.request_id))
                self._wake.set()
        except asyncio.CancelledError:
            pass

    async def _run_worker(self) -> None:
        while True:
            async with self._lock:
                job = self._take_next_locked()
                if job is None:
                    if self._closing:
                        return
                    self._wake.clear()
            if job is None:
                await self._wake.wait()
                continue
            await self._execute(job)

    def _take_next_locked(self) -> _Job[Any] | None:
        while self._queue:
            job = self._queue.popleft()
            if job.status is not RequestStatus.QUEUED:
                continue
            self._queued_count -= 1
            if job.queue_timer is not None:
                job.queue_timer.cancel()
            if asyncio.get_running_loop().time() >= job.deadline:
                job.status = RequestStatus.QUEUE_TIMEOUT
                job.error = "queue_timeout"
                self._settle_exception(job, QueueTimeoutError(job.request_id))
                continue
            job.status = RequestStatus.RUNNING
            job.deadline = asyncio.get_running_loop().time() + self.request_timeout
            self._active = job
            return job
        return None

    async def _execute(self, job: _Job[Any]) -> None:
        task = asyncio.create_task(self._execute_request(job), name=f"lar-request-{job.request_id}")
        job.execution_task = task
        try:
            result = await asyncio.wait_for(task, timeout=self.request_timeout)
        except TimeoutError:
            async with self._lock:
                job.status = RequestStatus.PROVIDER_STOP_UNKNOWN
            stopped = await self._wait_for_provider_stop(job)
            if not stopped:
                await self._mark_provider_stop_unknown(job)
            await self._finish(
                job,
                RequestStatus.REQUEST_TIMEOUT if stopped else RequestStatus.BLOCKED,
                RequestTimeoutError(job.request_id)
                if stopped
                else RequestTimeoutError("provider_stop_unknown; operatore richiesto"),
            )
        except asyncio.CancelledError:
            # Worker cancellation is only expected during external task cancellation;
            # active request cancellation is delivered to its callback task above.
            if asyncio.current_task() and asyncio.current_task().cancelling():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                async with self._lock:
                    job.status = RequestStatus.PROVIDER_STOP_UNKNOWN
                stopped = await self._wait_for_provider_stop(job)
                if not stopped:
                    await self._mark_provider_stop_unknown(job)
                await self._finish(
                    job,
                    RequestStatus.CANCELLED if stopped else RequestStatus.BLOCKED,
                    RequestCancelledError(job.cancel_reason or "worker_cancelled")
                    if stopped
                    else RequestCancelledError("provider_stop_unknown; operatore richiesto"),
                )
                raise
            async with self._lock:
                job.status = RequestStatus.PROVIDER_STOP_UNKNOWN
            stopped = await self._wait_for_provider_stop(job)
            if not stopped:
                await self._mark_provider_stop_unknown(job)
            else:
                async with self._lock:
                    job.status = RequestStatus.FINALIZING
            await self._finish(
                job,
                RequestStatus.CANCELLED if stopped else RequestStatus.BLOCKED,
                RequestCancelledError(
                    job.cancel_reason
                    if stopped
                    else "provider_stop_unknown; è richiesto l'intervento dell'operatore"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - preserve callback failure for handle
            async with self._lock:
                job.status = RequestStatus.FINALIZING
                job.error = f"{type(exc).__name__}: {exc}"
            await self._finish(job, RequestStatus.FAILED, exc)
        else:
            async with self._lock:
                job.status = RequestStatus.FINALIZING
            await self._finish(job, RequestStatus.COMPLETED, result)

    async def _execute_request(self, job: _Job[T]) -> T:
        selected = self.model_selector(job.payload)
        if isinstance(selected, Awaitable):
            selected = await selected
        if isinstance(selected, ModelSelection):
            model_ids = (selected.primary, *selected.alternatives)
        else:
            model_ids = (selected,)
        candidates = tuple(dict.fromkeys(model_ids))
        if not candidates or any(not isinstance(model, str) or not model.strip() for model in candidates):
            raise ValueError("Il selettore deve restituire nomi modello non vuoti.")
        if len(candidates) > 11:
            raise ValueError("Il selettore non può superare 11 candidati unici.")
        lease = None
        selected_model = candidates[0]
        unavailable_error: ModelUnavailableError | None = None
        for selected_model in candidates:
            job.selected_model = selected_model
            try:
                lease = await self.model_manager.acquire(selected_model)
                break
            except ModelUnavailableError as exc:
                unavailable_error = exc
        if lease is None:
            if unavailable_error is not None:
                raise unavailable_error
            raise RuntimeError("Nessun modello disponibile per l’inferenza.")
        if hasattr(job.payload, "selected_model_id"):
            job.payload.selected_model_id = selected_model
        if not isinstance(lease, ModelLease):
            raise TypeError("ModelManager.acquire() deve restituire ModelLease.")
        if lease.model != selected_model:
            raise ValueError("La lease acquisita non corrisponde al modello selezionato.")
        job.lease = lease
        try:
            if job.callback is not None:
                return await job.callback(job.payload)
            if self.provider is None:
                raise RuntimeError("Provider di generazione non configurato.")
            if not isinstance(job.payload, dict) or not isinstance(
                job.payload.get("prompt"), str
            ):
                raise TypeError("La richiesta di generazione richiede un prompt testuale.")
            return await self.provider.generate(
                model=selected,
                prompt=job.payload["prompt"],
                system=job.payload.get("system"),
                options=job.payload.get("options"),
            )
        finally:
            async with self._lock:
                if job.status is not RequestStatus.FINALIZING:
                    job.status = RequestStatus.FINALIZING
            await self._release_lease(lease)

    async def _release_lease(self, lease: ModelLease) -> None:
        """Release exactly once and wait for completion despite task cancellation."""
        cleanup = asyncio.create_task(
            self.model_manager.release(lease), name=f"lar-lease-release-{lease.id}"
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # The release task is independent: do not free the FIFO slot until it ends.
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise

    async def _wait_for_provider_stop(self, job: _Job[Any]) -> bool:
        """Wait only for explicit provider evidence, never infer stop from /api/ps."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.stop_confirmation_timeout
        while True:
            if self.stop_verifier is not None and job.selected_model is not None:
                try:
                    if await self.stop_verifier(job.selected_model) is True:
                        return True
                except Exception:
                    logger.warning(
                        "Verifica arresto provider fallita; mantengo lo scheduler in attesa",
                        exc_info=True,
                    )
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.stop_check_interval, remaining))

    async def _mark_provider_stop_unknown(self, job: _Job[Any]) -> None:
        """Fail closed and reject queued work when physical stop is unproven."""
        async with self._lock:
            self._provider_stop_unknown = True
            self._accepting = False
            self._closing = True
            job.status = RequestStatus.PROVIDER_STOP_UNKNOWN
            for queued in self._queue:
                if queued.status is RequestStatus.QUEUED:
                    queued.status = RequestStatus.CANCELLED
                    queued.error = "provider_stop_unknown"
                    self._settle_exception(
                        queued, RequestCancelledError("provider_stop_unknown")
                    )
                    if queued.queue_timer is not None:
                        queued.queue_timer.cancel()
            self._queued_count = 0

    async def _finish(self, job: _Job[Any], status: RequestStatus, outcome: Any) -> None:
        async with self._lock:
            if job.cancel_requested and status is RequestStatus.COMPLETED:
                status = RequestStatus.CANCELLED
                outcome = RequestCancelledError(job.cancel_reason)
            job.status = status
            if isinstance(outcome, BaseException):
                job.error = str(outcome)
                self._settle_exception(job, outcome)
            else:
                if not job.future.done():
                    job.future.set_result(outcome)
            if self._active is job:
                self._active = None
            job.execution_task = None
            self._wake.set()

    @staticmethod
    def _settle_exception(job: _Job[Any], error: BaseException) -> None:
        if not job.future.done():
            job.future.set_exception(error)
            # Prevent "Future exception was never retrieved" when callers choose
            # not to await a terminal job; awaiters still receive the exception.
            job.future.add_done_callback(lambda future: future.exception())
