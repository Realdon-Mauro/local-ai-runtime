from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(Exception):
    """Errore generico indipendente dal runtime."""


class ProviderConnectionError(ProviderError):
    """Errore di connessione a un provider."""


class ProviderGenerationError(ProviderError):
    """Il provider ha rifiutato o interrotto una generazione."""


class ProviderStreamError(ProviderError):
    """Lo stream del provider è malformato o incompleto."""


@dataclass(frozen=True)
class GenerationUsage:
    """Metriche di generazione normalizzate."""

    total_duration_seconds: float | None = None
    load_duration_seconds: float | None = None
    input_tokens: int | None = None
    input_duration_seconds: float | None = None
    output_tokens: int | None = None
    output_duration_seconds: float | None = None

    @staticmethod
    def _throughput(
        tokens: int | None,
        duration: float | None,
    ) -> float | None:
        if tokens is None or duration is None or duration <= 0:
            return None

        return tokens / duration

    @property
    def input_tokens_per_second(self) -> float | None:
        return self._throughput(
            self.input_tokens,
            self.input_duration_seconds,
        )

    @property
    def output_tokens_per_second(self) -> float | None:
        return self._throughput(
            self.output_tokens,
            self.output_duration_seconds,
        )


@dataclass(frozen=True)
class GenerationResponse:
    """Risposta normalizzata prodotta da un modello."""

    text: str
    usage: GenerationUsage
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Messaggio chat completo restituito dal provider."""

    message: Mapping[str, Any]
    usage: GenerationUsage
    done_reason: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """Evento NDJSON normalizzato; raw resta confinato al boundary provider."""

    message: Mapping[str, Any]
    done: bool
    usage: GenerationUsage | None = None
    done_reason: str | None = None
    raw: Mapping[str, Any] | None = None


class GenerationProvider(Protocol):
    """Contratto comune per i provider di generazione."""

    async def generate(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> GenerationResponse:
        """Genera una risposta utilizzando il modello indicato."""
        ...


class ChatProvider(Protocol):
    """Contratto chat: non esegue i tool che il modello restituisce."""

    async def chat(self, request: Mapping[str, Any]) -> ChatResponse: ...

    def stream_chat(self, request: Mapping[str, Any]) -> AsyncIterator[ChatChunk]: ...

    async def close(self) -> None: ...
