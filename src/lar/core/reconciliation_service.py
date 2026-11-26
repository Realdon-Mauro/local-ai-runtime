"""Periodic, lifecycle-independent reconciliation for a model manager."""

import asyncio
from collections.abc import Awaitable, Callable

from lar.core.model_manager import ModelManager, ReconciliationResult


class ReconciliationService:
    """Run ``ModelManager.reconcile`` periodically in one owned task.

    The first reconciliation runs as soon as the service starts. Later runs are
    separated by ``interval_seconds``. A custom ``wait`` can be supplied by
    deterministic callers and tests; it must return when the stop event is set.
    """

    def __init__(
        self,
        model_manager: ModelManager,
        interval_seconds: float = 60.0,
        *,
        wait: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("L'intervallo deve essere maggiore di zero.")
        self._model_manager = model_manager
        self.interval_seconds = interval_seconds
        self._wait = wait or self._wait_for_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self.last_error: str | None = None
        self.last_result: ReconciliationResult | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the background loop without waiting for reconciliation."""
        if self.running:
            return
        # A completed task may still be awaiting its done callback; replace it
        # synchronously so repeated starts can never create two active loops.
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(self._stop_event))

    async def stop(self) -> None:
        """Cancel and join the owned loop, including an active reconciliation."""
        task = self._task
        if task is None:
            return
        if not task.done():
            assert self._stop_event is not None
            self._stop_event.set()
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation of the service task is the requested stop behavior.
            # Cancellation of this stop() caller must still propagate.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            if self._task is task:
                self._task = None
                self._stop_event = None

    async def _run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                result = await self._model_manager.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the periodic loop alive
                self.last_error = type(exc).__name__
                self.last_result = None
            else:
                self.last_result = result
                self.last_error = (
                    "Reconciliation reported errors" if result.errors else None
                )

            if stop_event.is_set():
                break
            await self._wait(stop_event, self.interval_seconds)

    @staticmethod
    async def _wait_for_interval(stop_event: asyncio.Event, interval: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            pass
