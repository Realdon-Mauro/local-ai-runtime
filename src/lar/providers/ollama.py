import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Self

import httpx

from lar.providers.base import (
    ChatChunk,
    ChatProvider,
    ChatResponse,
    GenerationResponse,
    GenerationUsage,
    ProviderConnectionError,
    ProviderError,
    ProviderStreamError,
)


class OllamaError(ProviderError):
    """Errore generico del provider Ollama."""


class OllamaConnectionError(OllamaError, ProviderConnectionError):
    """Ollama non è raggiungibile."""


class OllamaTimeoutError(OllamaError):
    """La richiesta a Ollama è scaduta; l'esito remoto può essere ambiguo."""


class OllamaHTTPError(OllamaError):
    """Ollama ha risposto con uno status HTTP non valido."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class OllamaResponseError(OllamaError):
    """La risposta di Ollama è malformata o inattesa."""


class OllamaStreamError(OllamaResponseError, ProviderStreamError):
    """Lo stream NDJSON di Ollama è malformato o incompleto."""


class OllamaProvider(ChatProvider):
    """Client asincrono per le API locali di Ollama."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )

    async def close(self) -> None:
        """Chiude il client HTTP se gestito dal provider."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "Self":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                endpoint,
                **kwargs,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(
                f"Timeout durante la richiesta a Ollama: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise OllamaConnectionError(
                f"Impossibile contattare Ollama: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaHTTPError(
                f"Ollama ha restituito HTTP {exc.response.status_code}: "
                f"{exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc
        except ValueError as exc:
            raise OllamaResponseError(
                "Ollama ha restituito una risposta JSON non valida."
            ) from exc

        if not isinstance(data, dict):
            raise OllamaResponseError("Formato della risposta Ollama non valido.")

        return data

    async def health_check(self) -> dict[str, Any]:
        """Restituisce la versione di Ollama se il servizio è raggiungibile."""
        return await self._request("GET", "/api/version")

    async def list_models(self) -> list[dict[str, Any]]:
        """Elenca i modelli installati."""
        data = await self._request("GET", "/api/tags")
        return self._read_models(data)

    async def list_running(self) -> list[dict[str, Any]]:
        """Elenca i modelli attualmente caricati in memoria."""
        data = await self._request("GET", "/api/ps")
        return self._read_models(data)

    @staticmethod
    def _read_models(data: dict[str, Any]) -> list[dict[str, Any]]:
        models = data.get("models")
        if not isinstance(models, list) or any(
            not isinstance(model, dict) for model in models
        ):
            raise OllamaResponseError(
                "Ollama ha restituito un elenco di modelli non valido."
            )
        return models

    async def load_model(
        self,
        model: str,
        keep_alive: str | int = -1,
    ) -> dict[str, Any]:
        """Carica un modello senza richiedere una generazione."""
        data = await self._request(
            "POST",
            "/api/generate",
            json={
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": keep_alive,
            },
        )
        if not isinstance(data.get("done"), bool) or not data["done"]:
            raise OllamaResponseError(
                "Ollama non ha confermato il completamento del caricamento."
            )
        return data

    async def unload_model(self, model: str) -> dict[str, Any]:
        """Scarica un modello dalla memoria."""
        return await self._request(
            "POST",
            "/api/generate",
            json={
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            },
        )

    @staticmethod
    def _read_counter(data: dict[str, Any], key: str) -> int | None:
        value = data.get(key)

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None

        return value

    @classmethod
    def _read_seconds(cls, data: dict[str, Any], key: str) -> float | None:
        value = cls._read_counter(data, key)

        if value is None:
            return None

        return value / 1_000_000_000

    async def generate(
        self,
        model: str,
        prompt: str,
        system: str | None = None,
        options: dict[str, Any] | None = None,
        keep_alive: str | int = "5m",
    ) -> GenerationResponse:
        """Genera testo e normalizza le metriche restituite da Ollama."""
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
        }

        if system is not None:
            payload["system"] = system

        if options is not None:
            payload["options"] = options

        raw = await self._request(
            "POST",
            "/api/generate",
            json=payload,
        )

        text = raw.get("response")

        if not isinstance(text, str) or not isinstance(raw.get("done"), bool):
            raise OllamaResponseError("Risposta di generazione Ollama incompleta o non valida.")
        if not raw["done"]:
            raise OllamaResponseError("Ollama non ha completato la generazione.")

        usage = GenerationUsage(
            total_duration_seconds=self._read_seconds(
                raw, "total_duration"
            ),
            load_duration_seconds=self._read_seconds(
                raw, "load_duration"
            ),
            input_tokens=self._read_counter(
                raw, "prompt_eval_count"
            ),
            input_duration_seconds=self._read_seconds(
                raw, "prompt_eval_duration"
            ),
            output_tokens=self._read_counter(
                raw, "eval_count"
            ),
            output_duration_seconds=self._read_seconds(
                raw, "eval_duration"
            ),
        )

        return GenerationResponse(
            text=text,
            usage=usage,
            raw=raw,
        )

    @classmethod
    def _read_chat(cls, raw: dict[str, Any]) -> ChatResponse:
        message = raw.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise OllamaResponseError("Messaggio assistant Ollama assente o non valido.")
        if not isinstance(message.get("content", ""), str):
            raise OllamaResponseError("Contenuto chat Ollama non valido.")
        if not isinstance(raw.get("done"), bool) or not raw["done"]:
            raise OllamaResponseError("Ollama non ha completato la risposta chat.")
        usage = GenerationUsage(
            total_duration_seconds=cls._read_seconds(raw, "total_duration"),
            load_duration_seconds=cls._read_seconds(raw, "load_duration"),
            input_tokens=cls._read_counter(raw, "prompt_eval_count"),
            input_duration_seconds=cls._read_seconds(raw, "prompt_eval_duration"),
            output_tokens=cls._read_counter(raw, "eval_count"),
            output_duration_seconds=cls._read_seconds(raw, "eval_duration"),
        )
        reason = raw.get("done_reason")
        if reason is not None and not isinstance(reason, str):
            raise OllamaResponseError("done_reason Ollama non valido.")
        return ChatResponse(message=message, usage=usage, done_reason=reason, raw=raw)

    @staticmethod
    def _validate_chat_request(request: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(request)
        if not isinstance(payload.get("model"), str) or not payload["model"].strip():
            raise ValueError("La richiesta chat richiede un modello.")
        if not isinstance(payload.get("messages"), list) or not payload["messages"]:
            raise ValueError("La richiesta chat richiede almeno un messaggio.")
        return payload

    async def chat(self, request: Mapping[str, Any]) -> ChatResponse:
        """Esegue POST /api/chat senza stream e conserva tool_calls del backend."""
        payload = self._validate_chat_request(request)
        payload["stream"] = False
        return self._read_chat(await self._request("POST", "/api/chat", json=payload))

    async def stream_chat(self, request: Mapping[str, Any]) -> AsyncIterator[ChatChunk]:
        """Legge NDJSON /api/chat; nessun retry o esecuzione automatica dei tool."""
        payload = self._validate_chat_request(request)
        payload["stream"] = True
        saw_done = False
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise OllamaStreamError("Chunk NDJSON Ollama non valido.") from exc
                    if not isinstance(item, dict):
                        raise OllamaStreamError("Chunk Ollama non è un oggetto JSON.")
                    message = item.get("message")
                    done = item.get("done")
                    if not isinstance(message, dict) or not isinstance(done, bool):
                        raise OllamaStreamError("Chunk chat Ollama incompleto.")
                    if message.get("role") not in (None, "assistant"):
                        raise OllamaStreamError("Ruolo chunk chat Ollama non valido.")
                    reason = item.get("done_reason")
                    if reason is not None and not isinstance(reason, str):
                        raise OllamaStreamError("done_reason chunk Ollama non valido.")
                    usage = None
                    if done:
                        usage = GenerationUsage(
                            total_duration_seconds=self._read_seconds(item, "total_duration"),
                            load_duration_seconds=self._read_seconds(item, "load_duration"),
                            input_tokens=self._read_counter(item, "prompt_eval_count"),
                            input_duration_seconds=self._read_seconds(item, "prompt_eval_duration"),
                            output_tokens=self._read_counter(item, "eval_count"),
                            output_duration_seconds=self._read_seconds(item, "eval_duration"),
                        )
                        saw_done = True
                    yield ChatChunk(
                        message=message,
                        done=done,
                        usage=usage,
                        done_reason=reason,
                        raw=item,
                    )
                    if done:
                        break
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(f"Timeout durante lo stream Ollama: {exc}") from exc
        except httpx.RequestError as exc:
            raise OllamaConnectionError(f"Connessione stream Ollama interrotta: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaHTTPError(
                f"Ollama ha restituito HTTP {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
            ) from exc
        if not saw_done:
            raise OllamaStreamError("Stream Ollama terminato senza chunk finale done=true.")

	
    async def show_model(self, model: str) -> dict[str, Any]:
        """Recupera i metadati dettagliati di un modello."""
        return await self._request(
            "POST",
            "/api/show",
            json={
                "model": model,
                "verbose": True,
            },
        )
