"""Application composition root for the single-process LAR runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lar.api.compat import to_ollama_request
from lar.api.schemas import ChatCompletionRequest
from lar.core.config import RuntimeConfig
from lar.core.model_manager import ModelManager
from lar.core.reconciliation_service import ReconciliationService
from lar.core.resources import ResourceManager, ResourcePolicy
from lar.core.router import ModelCandidate, ModelRouter, RoutingDecision
from lar.core.scheduler import JobHandle, ModelSelection, RequestSnapshot, Scheduler
from lar.evaluation.discovery import discover_models
from lar.providers.base import ProviderError
from lar.providers.ollama import OllamaProvider
from lar.storage.database import Database

logger = logging.getLogger("lar.runtime")


class NoEligibleModelError(RuntimeError):
    """No model satisfies task compatibility and resource admission."""


class ProviderStopUnresolvedError(RuntimeError):
    """Startup is blocked until an operator verifies a prior provider stop."""


@dataclass(slots=True)
class ScheduledInference:
    request_id: str
    request: ChatCompletionRequest
    event_queue: asyncio.Queue[tuple[str, Any]] | None
    decision: RoutingDecision | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    first_token_at: datetime | None = None
    resource_snapshot: Any = None
    usage: Any = None
    selected_model_id: str | None = None


class LARApplication:
    """Own provider, resources, scheduler, model manager and storage lifecycle."""

    def __init__(self, config: RuntimeConfig, *, provider: Any | None = None) -> None:
        self.config = config
        self.database = Database(config.database_path)
        self.provider = provider or OllamaProvider(
            base_url=config.ollama_url, timeout=config.ollama_timeout_seconds
        )
        self.model_manager = ModelManager(self.provider)
        self.resource_manager = ResourceManager(
            storage_path=config.database_path.expanduser().parent,
            provider=self.provider,
        )
        self.resource_policy = ResourcePolicy.from_config(config)
        self.router = ModelRouter(resource_policy=self.resource_policy)
        self.scheduler = Scheduler(
            queue_capacity=config.queue_capacity,
            queue_timeout=config.queue_timeout_seconds,
            request_timeout=config.request_timeout_seconds,
            model_manager=self.model_manager,
            model_selector=self._select_model,
            stop_check_interval=config.stop_check_interval_seconds,
            stop_confirmation_timeout=config.stop_confirmation_timeout_seconds,
        )
        self.reconciliation = ReconciliationService(self.model_manager)
        self._candidates: tuple[ModelCandidate, ...] = ()
        self._artifact_ids: dict[str, str] = {}
        self._hardware_profile_id: str | None = None
        self._started = False
        self._closed = False
        self._provider_error: str | None = None

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("LARApplication già chiusa.")
        try:
            self.database.migrate()
            self.database.save_config(self.config)
            self.database.purge_expired(self.config)
            if self.database.has_unresolved_provider_stop():
                raise ProviderStopUnresolvedError(
                    "Avvio bloccato: verificare fisicamente lo stop Ollama e registrare la conferma operatore."
                )
            hardware = await self.resource_manager.discover()
            self._hardware_profile_id = self.database.save_hardware_profile(hardware)
            try:
                await self.provider.health_check()
                inventory = await discover_models(self.provider)
                self._provider_error = None
            except ProviderError as exc:
                self._provider_error = type(exc).__name__
                inventory = []
            candidates = []
            for item in inventory:
                self._artifact_ids[item.name] = self.database.save_model_artifact(
                    model_id=item.name,
                    digest=item.digest,
                    model_info=dict(item.model_info),
                    inspection_status="error" if item.inspection_error else "available",
                )
                candidates.append(ModelCandidate(
                    model_id=item.name,
                    digest=item.digest,
                    family=item.family,
                    capabilities=(
                        frozenset(item.capabilities) if not item.inspection_error else None
                    ),
                    context_limit_tokens=self._metadata_int(
                        item.model_info,
                        ("llama.context_length", "context_length", "model.context_length"),
                    ),
                    artifact_size_bytes=item.size_bytes,
                    model_info=item.model_info,
                ))
            self._candidates = tuple(candidates)
            await self.scheduler.start()
            await self.reconciliation.start()
            self._started = True
        except BaseException:
            await self.provider.close()
            raise

    async def close(self, *, drain: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        await self.reconciliation.stop()
        await self.scheduler.close(
            drain=drain,
            grace_period=self.config.shutdown_grace_period_seconds if drain else 0.0,
        )
        await self.provider.close()

    async def submit(
        self,
        request_id: str,
        request: ChatCompletionRequest,
        *,
        event_queue: asyncio.Queue[tuple[str, Any]] | None = None,
    ) -> JobHandle[Any]:
        if not self._started or self._closed:
            raise RuntimeError("Runtime LAR non disponibile.")
        scheduled = ScheduledInference(
            request_id, request, event_queue, queued_at=datetime.now(UTC)
        )
        self.database.record_request(
            request_id, stream=request.stream, tool_count=len(request.tools or ())
        )
        try:
            handle = await self.scheduler.submit(
                scheduled, self._run_scheduled, request_id=request_id
            )
            self.database.update_request(request_id, status="queued")
            return handle
        except Exception as exc:
            logger.exception(
                "Inference admission failed",
                extra={"request_id": request_id, "error_type": type(exc).__name__},
            )
            self.database.update_request(
                request_id, status="rejected", error_code=type(exc).__name__
            )
            raise

    async def _select_model(self, payload: ScheduledInference) -> ModelSelection:
        payload.started_at = datetime.now(UTC)
        snapshot = await self.resource_manager.snapshot()
        payload.resource_snapshot = snapshot
        if self._hardware_profile_id is None:
            raise RuntimeError("Profilo hardware non inizializzato.")
        self.database.save_resource_snapshot(snapshot, self._hardware_profile_id)
        decision = self.router.route(
            request_id=payload.request_id,
            request=payload.request.model_dump(mode="python", exclude_none=True),
            candidates=self._candidates,
            snapshot=snapshot,
        )
        payload.decision = decision
        self.database.save_routing_decision(
            decision,
            request_id=payload.request_id,
            hardware_profile_id=self._hardware_profile_id,
            config_id=self.config.effective_hash(),
            artifact_ids=self._artifact_ids,
        )
        if decision.selected_model_id is None:
            self.database.update_request(
                payload.request_id, status="rejected", error_code="NoEligibleModelError"
            )
            raise NoEligibleModelError("Nessun modello compatibile e ammissibile.")
        return ModelSelection(decision.selected_model_id, decision.alternatives[:10])

    def persist_scheduler_state(self, state: RequestSnapshot) -> None:
        terminal = {
            "completed", "failed", "cancelled", "queue_timeout", "request_timeout",
            "rejected", "blocked", "provider_stop_unknown",
        }
        if state.status.value in terminal:
            self.database.update_request(
                state.request_id,
                status=state.status.value,
                error_code=(
                    "provider_stop_unknown"
                    if state.status.value == "blocked" and state.error and "provider_stop_unknown" in state.error
                    else state.error
                ),
            )

    async def _run_scheduled(self, payload: ScheduledInference) -> Any:
        self.database.update_request(payload.request_id, status="running")
        try:
            response = await self._execute(payload)
        except asyncio.CancelledError:
            # Runtime Ollama non può provare la fine fisica della generazione.
            self.database.update_request(
                payload.request_id,
                status="provider_stop_unknown",
                error_code="provider_stop_unverified",
            )
            raise
        except Exception as exc:
            self.database.update_request(
                payload.request_id, status="failed", error_code=type(exc).__name__
            )
            raise
        self._persist_request_metrics(payload)
        self.database.update_request(payload.request_id, status="completed")
        return response

    def _persist_request_metrics(self, payload: ScheduledInference) -> None:
        now = datetime.now(UTC)
        started = payload.started_at or now
        queued = payload.queued_at or started
        usage = payload.usage
        snapshot = payload.resource_snapshot
        sources = {
            "queue_wait_ms": "lar_wall_clock",
            "total_ms": "lar_wall_clock",
            "first_token_ms": "provider_stream_chunk_observed" if payload.first_token_at else "unavailable",
            "model_load_ms": "ollama_usage" if usage and usage.load_duration_seconds is not None else "unavailable",
            "prompt_tokens": "ollama_usage" if usage and usage.input_tokens is not None else "unavailable",
            "output_tokens": "ollama_usage" if usage and usage.output_tokens is not None else "unavailable",
            "resource_peaks": "unavailable:no_continuous_sampling",
        }
        if snapshot is not None:
            sources["resource_snapshot_id"] = snapshot.snapshot_id
            sources["resource_snapshot_sources"] = snapshot.sources
        self.database.save_request_metrics(
            payload.request_id,
            {
                "queue_wait_ms": max(0.0, (started - queued).total_seconds() * 1000),
                "model_load_ms": usage.load_duration_seconds * 1000
                if usage and usage.load_duration_seconds is not None else None,
                "first_token_ms": max(0.0, (payload.first_token_at - started).total_seconds() * 1000)
                if payload.first_token_at else None,
                "total_ms": max(0.0, (now - queued).total_seconds() * 1000),
                "prompt_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "generation_tps": usage.output_tokens_per_second if usage else None,
                "sources": sources,
            },
        )

    async def _execute(self, payload: ScheduledInference) -> Any:
        request = payload.request
        ollama_request = to_ollama_request(request)
        # Scheduler calls this only after routing and acquiring the selected lease.
        if payload.decision is None or payload.decision.selected_model_id is None:
            raise RuntimeError("Decisione di routing assente dopo la selezione.")
        selected_model = payload.selected_model_id or payload.decision.selected_model_id
        ollama_request["model"] = selected_model
        artifact_id = self._artifact_ids.get(selected_model)
        if artifact_id is not None:
            if selected_model != payload.decision.selected_model_id:
                self.database.record_model_fallback(
                    payload.request_id,
                    from_model=payload.decision.selected_model_id,
                    to_model=selected_model,
                    artifact_id=artifact_id,
                )
            else:
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE inference_requests SET selected_artifact=? WHERE request_id=?",
                        (artifact_id, payload.request_id),
                    )
        if request.stream:
            if payload.event_queue is None:
                raise RuntimeError("Coda eventi SSE non configurata.")
            try:
                async for event in self.provider.stream_chat(ollama_request):
                    if payload.first_token_at is None and (
                        event.message.get("content") or event.message.get("tool_calls")
                    ):
                        payload.first_token_at = datetime.now(UTC)
                    if event.usage is not None:
                        payload.usage = event.usage
                    await payload.event_queue.put(("chunk", event))
                return None
            except asyncio.CancelledError:
                payload.event_queue.put_nowait(("cancelled", None))
                raise
            except Exception as exc:
                payload.event_queue.put_nowait(("error", exc))
                raise
        response = await self.provider.chat(ollama_request)
        payload.usage = response.usage
        return response

    async def health(self) -> dict[str, Any]:
        provider_ok = False
        try:
            await self.provider.health_check()
            provider_ok = True
            self._provider_error = None
        except Exception as exc:  # noqa: BLE001 - health reports arbitrary provider failure as unavailable
            self._provider_error = type(exc).__name__
        storage_ok = self.database.health() == "ok"
        queue = await self.scheduler.queue_status()
        warnings = []
        if not provider_ok:
            warnings.append("provider_unavailable")
        if not storage_ok:
            warnings.append("storage_unavailable")
        if queue.provider_stop_unknown:
            warnings.append("provider_stop_unknown_operator_action_required")
        if not self._candidates:
            warnings.append("no_models_discovered")
        status = "healthy" if not warnings else "degraded"
        return {
            "status": status,
            "provider": {"status": "healthy" if provider_ok else "unavailable"},
            "storage": {"status": "healthy" if storage_ok else "unavailable"},
            "scheduler": {
                "status": "blocked" if queue.provider_stop_unknown else "healthy",
                "queued": queue.queued_count,
                "active": queue.active_count,
                "capacity": queue.capacity,
                "accepting": queue.accepting,
            },
            "model_manager": {"status": "healthy" if self.model_manager.get_states() else "unknown"},
            "resources": {"status": "available"},
            "catalog_model_count": len(self._candidates),
            "warnings": warnings,
        }

    @staticmethod
    def _metadata_int(metadata: dict[str, Any], keys: tuple[str, ...]) -> int | None:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return None
