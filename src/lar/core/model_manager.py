import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from lar.providers.ollama import OllamaHTTPError, OllamaProvider


class ModelManagerError(Exception):
    """Errore generico del ModelManager."""


class ModelInUseError(ModelManagerError):
    """Il modello non può essere scaricato perché è in uso."""


class ModelUnavailableError(ModelManagerError):
    """Ollama ha rifiutato definitivamente il caricamento del modello (HTTP 404)."""


class ModelOwnership(StrEnum):
    LAR = "lar"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class ObservedPresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelState:
    """Snapshot immutabile dello stato pubblico di un modello."""

    name: str
    ownership: ModelOwnership = ModelOwnership.UNKNOWN
    observed_presence: ObservedPresence = ObservedPresence.UNKNOWN
    active_requests: int = 0

    @property
    def loaded(self) -> bool:
        """Compatibilità: True solo quando la presenza è stata osservata."""
        return self.observed_presence is ObservedPresence.PRESENT

    @property
    def owned_by_manager(self) -> bool:
        """Compatibilità legacy; la provenienza è esposta da ``ownership``."""
        return self.ownership is ModelOwnership.LAR


@dataclass(frozen=True, slots=True)
class ModelLease:
    """Token immutabile che identifica una singola acquisizione."""

    id: UUID
    model: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Risultato immutabile di un'osservazione completa o fallita."""

    present: tuple[str, ...]
    absent: tuple[str, ...]
    unknown: tuple[str, ...]
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class _ModelRecord:
    name: str
    ownership: ModelOwnership = ModelOwnership.UNKNOWN
    observed_presence: ObservedPresence = ObservedPresence.UNKNOWN
    active_requests: int = 0
    load_outcome_ambiguous: bool = False


@dataclass(slots=True)
class _LeaseRecord:
    id: UUID
    model: str


class ModelManager:
    """Gestisce caricamento e lease dei modelli Ollama condivisi."""

    def __init__(self, provider: OllamaProvider) -> None:
        self.provider = provider
        self._states: dict[str, _ModelRecord] = {}
        self._leases: dict[UUID, _LeaseRecord] = {}
        self._lock = asyncio.Lock()

    def _get_state(self, model: str) -> _ModelRecord:
        if model not in self._states:
            self._states[model] = _ModelRecord(name=model)

        return self._states[model]

    async def acquire(self, model: str) -> ModelLease:
        """Verifica la presenza, carica se assente e registra una lease."""
        async with self._lock:
            state = self._get_state(model)

            try:
                running = await self.provider.list_running()
                loaded_names = {
                    item["name"] for item in running
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
                if len(loaded_names) != len(running):
                    raise ValueError("Elenco dei modelli Ollama incompleto.")
            except asyncio.CancelledError:
                state.observed_presence = ObservedPresence.UNKNOWN
                raise
            except Exception:
                state.observed_presence = ObservedPresence.UNKNOWN
                raise

            if model in loaded_names:
                state.observed_presence = ObservedPresence.PRESENT
                if (
                    state.ownership is ModelOwnership.UNKNOWN
                    and not state.load_outcome_ambiguous
                ):
                    state.ownership = ModelOwnership.EXTERNAL
            else:
                state.observed_presence = ObservedPresence.ABSENT
                try:
                    await self.provider.load_model(model)
                except asyncio.CancelledError:
                    state.observed_presence = ObservedPresence.UNKNOWN
                    state.load_outcome_ambiguous = True
                    raise
                except OllamaHTTPError as exc:
                    if exc.status_code == 404:
                        state.observed_presence = ObservedPresence.ABSENT
                        state.load_outcome_ambiguous = False
                        raise ModelUnavailableError(
                            f"Modello Ollama non più disponibile: {model}"
                        ) from exc
                    state.observed_presence = ObservedPresence.UNKNOWN
                    state.load_outcome_ambiguous = True
                    raise
                except Exception:
                    state.observed_presence = ObservedPresence.UNKNOWN
                    state.load_outcome_ambiguous = True
                    raise
                state.observed_presence = ObservedPresence.PRESENT
                state.ownership = ModelOwnership.LAR
                state.load_outcome_ambiguous = False

            lease = ModelLease(id=uuid4(), model=model)
            self._leases[lease.id] = _LeaseRecord(id=lease.id, model=lease.model)
            state.active_requests += 1
            return lease

    async def release(self, lease: ModelLease) -> ModelState:
        """Rilascia in modo idempotente la specifica lease identificata."""
        if not isinstance(lease, ModelLease):
            raise ModelManagerError("Il rilascio richiede una lease identificabile.")
        async with self._lock:
            registered = self._leases.get(lease.id)
            if registered is None:
                state = self._states.get(lease.model)
                return self._snapshot(state) if state else ModelState(name=lease.model)
            if registered.model != lease.model:
                return self._snapshot(self._states[registered.model])

            state = self._states[registered.model]
            del self._leases[lease.id]
            state.active_requests -= 1
            return self._snapshot(state)

    async def reconcile(self) -> ReconciliationResult:
        """Riallinea la presenza osservata senza modificare ownership o lease.

        La query di rete mantiene il lock condiviso con acquire/release per
        impedire che una risposta più vecchia sovrascriva lo stato di un'operazione
        concorrente. Durante la query, acquire e release attendono la risposta.
        """
        async with self._lock:
            try:
                running = await self.provider.list_running()
                if not isinstance(running, list):
                    raise TypeError("Elenco dei modelli Ollama non valido.")
                names: set[str] = set()
                for item in running:
                    if not isinstance(item, dict) or not isinstance(
                        item.get("name"), str
                    ) or not item["name"]:
                        raise ValueError("Elenco dei modelli Ollama incompleto.")
                    names.add(item["name"])
            except asyncio.CancelledError:
                for state in self._states.values():
                    state.observed_presence = ObservedPresence.UNKNOWN
                raise
            except Exception as exc:  # noqa: BLE001 - ogni errore rende l'osservazione ignota
                for state in self._states.values():
                    state.observed_presence = ObservedPresence.UNKNOWN
                return ReconciliationResult(
                    present=(),
                    absent=(),
                    unknown=tuple(sorted(self._states)),
                    errors=(f"{type(exc).__name__}: {exc}",),
                )

            for name in names:
                self._get_state(name).observed_presence = ObservedPresence.PRESENT
            for name, state in self._states.items():
                if name not in names:
                    state.observed_presence = ObservedPresence.ABSENT

            return ReconciliationResult(
                present=tuple(sorted(names)),
                absent=tuple(sorted(name for name in self._states if name not in names)),
                unknown=(),
            )

    async def unload(self, model: str) -> None:
        """Rifiuta unload amministrativi: il lifecycle memoria è gestito da Ollama."""
        raise ModelManagerError(
            "Unload amministrativo non supportato con Ollama condiviso."
        )

    @asynccontextmanager
    async def lease(self, model: str) -> AsyncIterator[ModelLease]:
        """Acquisisce e rilascia automaticamente un lease."""
        lease = await self.acquire(model)

        try:
            yield lease
        finally:
            await asyncio.shield(self.release(lease))

    def get_states(self) -> list[ModelState]:
        """Restituisce snapshot immutabili indipendenti dallo stato interno."""
        return [self._snapshot(state) for state in self._states.values()]

    @staticmethod
    def _snapshot(state: _ModelRecord) -> ModelState:
        return ModelState(
            name=state.name,
            ownership=state.ownership,
            observed_presence=state.observed_presence,
            active_requests=state.active_requests,
        )
