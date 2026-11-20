import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from lar.providers.ollama import OllamaProvider


class ModelManagerError(Exception):
    """Errore generico del ModelManager."""


class ModelInUseError(ModelManagerError):
    """Il modello non può essere scaricato perché è in uso."""


@dataclass
class ModelState:
    name: str
    loaded: bool = False
    active_requests: int = 0
    owned_by_manager: bool = False


class ModelManager:
    """Gestisce caricamento, utilizzo e scaricamento dei modelli."""

    def __init__(self, provider: OllamaProvider) -> None:
        self.provider = provider
        self._states: dict[str, ModelState] = {}
        self._lock = asyncio.Lock()

    def _get_state(self, model: str) -> ModelState:
        if model not in self._states:
            self._states[model] = ModelState(name=model)

        return self._states[model]

    async def acquire(self, model: str) -> ModelState:
        """Carica il modello se necessario e registra una richiesta attiva."""
        async with self._lock:
            state = self._get_state(model)

            running = await self.provider.list_running()
            loaded_names = {item["name"] for item in running}

            if model not in loaded_names:
                await self.provider.load_model(model)
                state.owned_by_manager = True
            elif not state.loaded:
                state.owned_by_manager = False

            state.loaded = True
            state.active_requests += 1

            return state

    async def release(self, model: str) -> ModelState:
        """Registra la fine di una richiesta che utilizzava il modello."""
        async with self._lock:
            state = self._get_state(model)

            if state.active_requests == 0:
                raise ModelManagerError(
                    f"Il modello {model} non ha richieste attive da rilasciare."
                )

            state.active_requests -= 1
            return state

    async def unload(self, model: str) -> None:
        """Scarica un modello, ma solo se non è utilizzato."""
        async with self._lock:
            state = self._get_state(model)

            if state.active_requests > 0:
                raise ModelInUseError(
                    f"Impossibile scaricare {model}: "
                    f"{state.active_requests} richieste ancora attive."
                )

            if state.loaded and state.owned_by_manager:
                await self.provider.unload_model(model)
                state.loaded = False
                state.owned_by_manager = False

    @asynccontextmanager
    async def lease(self, model: str) -> AsyncIterator[ModelState]:
        """Acquisisce e rilascia automaticamente un lease."""
        state = await self.acquire(model)

        try:
            yield state
        finally:
            await self.release(model)

    def get_states(self) -> list[ModelState]:
        """Restituisce una copia degli stati gestiti dal runtime."""
        return [
            ModelState(
                name=state.name,
                loaded=state.loaded,
                active_requests=state.active_requests,
                owned_by_manager=state.owned_by_manager,
            )
            for state in self._states.values()
        ]
