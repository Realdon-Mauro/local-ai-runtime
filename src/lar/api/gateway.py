"""Loopback-first FastAPI gateway for the supported OpenAI chat subset."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from lar.api.compat import CompatibilityError, from_ollama_message, to_ollama_request
from lar.api.schemas import (
    ChatCompletionRequest,
    OpenAIModel,
    OpenAIModelsResponse,
    OpenCodeObservation,
)
from lar.core.application import LARApplication, NoEligibleModelError
from lar.core.config import RuntimeConfig
from lar.core.model_manager import ModelUnavailableError
from lar.core.scheduler import (
    QueueFullError,
    QueueTimeoutError,
    RequestCancelledError,
    RequestTimeoutError,
    SchedulerClosedError,
)
from lar.providers.ollama import (
    OllamaConnectionError,
    OllamaError,
    OllamaHTTPError,
    OllamaResponseError,
    OllamaTimeoutError,
)
from lar.storage.database import StorageError

logger = logging.getLogger("lar.http")


def openai_error(
    message: str,
    *,
    code: str,
    request_id: str,
    error_type: str = "invalid_request_error",
    param: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
            "request_id": request_id,
        }
    }


def _map_exception(exc: Exception) -> tuple[int, str, str, str]:
    if isinstance(exc, QueueFullError):
        return 429, "queue_full", str(exc), "rate_limit_error"
    if isinstance(exc, QueueTimeoutError):
        return 408, "queue_timeout", str(exc), "timeout_error"
    if isinstance(exc, RequestTimeoutError):
        return 504, "request_timeout", str(exc), "timeout_error"
    if isinstance(exc, RequestCancelledError):
        return 499, "request_cancelled", str(exc), "server_error"
    if isinstance(exc, SchedulerClosedError):
        return 503, "scheduler_unavailable", str(exc), "server_error"
    if isinstance(exc, NoEligibleModelError):
        return 503, "no_eligible_model", str(exc), "server_error"
    if isinstance(exc, ModelUnavailableError):
        return 503, "model_unavailable", "Il modello non è più disponibile in Ollama.", "server_error"
    if isinstance(exc, OllamaConnectionError):
        return 503, "provider_unavailable", "Il provider Ollama non è disponibile.", "server_error"
    if isinstance(exc, OllamaTimeoutError):
        return 504, "provider_timeout", "Timeout del provider Ollama.", "timeout_error"
    if isinstance(exc, (OllamaHTTPError, OllamaResponseError, CompatibilityError)):
        return 502, "provider_protocol_error", "Il provider non ha completato il contratto chat.", "server_error"
    if isinstance(exc, OllamaError):
        return 502, "provider_error", "Errore del provider Ollama.", "server_error"
    if isinstance(exc, ValueError):
        return 400, "invalid_request", str(exc), "invalid_request_error"
    return 500, "internal_error", "Errore interno LAR.", "server_error"


def _usage_dict(input_tokens: int | None, output_tokens: int | None) -> dict[str, int] | None:
    if input_tokens is None or output_tokens is None:
        return None
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _completion_payload(request_id: str, response: Any) -> dict[str, Any]:
    content, tool_calls = from_ollama_message(dict(response.message))
    calls = []
    for call in tool_calls:
        calls.append({key: value for key, value in call.items() if key != "index"})
    finish_reason = "tool_calls" if calls else (response.done_reason or "stop")
    if finish_reason not in {"stop", "length", "tool_calls", "content_filter"}:
        finish_reason = "stop"
    result: dict[str, Any] = {
        "id": f"chatcmpl-{request_id.replace('-', '')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "lar-auto",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": calls} if calls else {}),
                },
                "finish_reason": finish_reason,
            }
        ],
    }
    usage = _usage_dict(response.usage.input_tokens, response.usage.output_tokens)
    if usage is not None:
        result["usage"] = usage
    return result


def create_app(
    config: RuntimeConfig | None = None,
    *,
    application: LARApplication | None = None,
) -> FastAPI:
    effective_config = config or RuntimeConfig()
    runtime = application or LARApplication(effective_config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close(drain=True)

    app = FastAPI(
        title="Local AI Runtime",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.lar = runtime

    if effective_config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(effective_config.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    @app.middleware("http")
    async def correlation_and_auth(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        if effective_config.api_token and request.url.path != "/health/live":
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {effective_config.api_token}"
            if not hmac.compare_digest(supplied, expected):
                response = JSONResponse(
                    openai_error(
                        "Autenticazione richiesta.", code="authentication_error",
                        request_id=request_id, error_type="authentication_error",
                    ),
                    status_code=401,
                )
                response.headers["WWW-Authenticate"] = "Bearer"
                response.headers["X-Request-ID"] = request_id
                return response
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                },
                separators=(",", ":"),
            )
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        safe_errors = [
            {"loc": error.get("loc"), "msg": error.get("msg"), "type": error.get("type")}
            for error in exc.errors()
        ]
        return JSONResponse(
            openai_error(
                f"Richiesta non valida: {safe_errors}",
                code="invalid_request_error",
                request_id=request.state.request_id,
            ),
            status_code=400,
        )

    @app.get("/health/live")
    async def liveness():
        return {"status": "alive"}

    @app.get("/health")
    async def health():
        result = await runtime.health()
        status = 200 if result["status"] == "healthy" else 503
        return JSONResponse(result, status_code=status)

    @app.get("/ready")
    async def readiness():
        result = await runtime.health()
        ready = (
            result["provider"]["status"] == "healthy"
            and result["storage"]["status"] == "healthy"
            and result["scheduler"]["accepting"]
            and result["catalog_model_count"] > 0
        )
        return JSONResponse(
            {"ready": ready, "status": result["status"], "warnings": result["warnings"]},
            status_code=200 if ready else 503,
        )

    try:
        integration_loopback = ipaddress.ip_address(effective_config.bind_host).is_loopback
    except ValueError:
        integration_loopback = False
    if integration_loopback:

        @app.post("/integrations/opencode/v1/events")
        async def opencode_observation(payload: OpenCodeObservation, request: Request):
            peer = request.client.host if request.client else ""
            try:
                peer_loopback = ipaddress.ip_address(peer).is_loopback
            except ValueError:
                peer_loopback = False
            if not peer_loopback:
                return JSONResponse({"error": "loopback_required"}, status_code=403)
            try:
                token = effective_config.opencode_token_file.expanduser().read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                return JSONResponse({"error": "integration_not_configured"}, status_code=503)
            supplied = request.headers.get("authorization", "")
            if not token or not hmac.compare_digest(supplied, f"Bearer {token}"):
                return JSONResponse({"error": "authentication_required"}, status_code=401)
            try:
                observation_id, workflow_id, duplicate = runtime.database.append_opencode_observation(
                    payload
                )
            except StorageError as exc:
                logger.warning(
                    "OpenCode observation rejected",
                    extra={"request_id": request.state.request_id, "error_type": type(exc).__name__},
                )
                return JSONResponse({"error": "observation_rejected"}, status_code=400)
            return {
                "status": "duplicate" if duplicate else "accepted",
                "observation_id": observation_id,
                "workflow_id": workflow_id,
            }

    @app.get("/v1/models", response_model=OpenAIModelsResponse)
    async def list_models() -> OpenAIModelsResponse:
        return OpenAIModelsResponse(data=[OpenAIModel()])

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: ChatCompletionRequest, request: Request):
        request_id = request.state.request_id
        if payload.model != "lar-auto":
            return JSONResponse(
                openai_error(
                    "Il solo modello esposto è lar-auto.",
                    code="model_not_found",
                    request_id=request_id,
                    param="model",
                ),
                status_code=400,
            )
        try:
            to_ollama_request(payload)
        except CompatibilityError as exc:
            status, code, message, kind = _map_exception(exc)
            return JSONResponse(
                openai_error(message, code=code, request_id=request_id, error_type=kind),
                status_code=status,
            )
        if not payload.stream:
            handle = None
            try:
                handle = await runtime.submit(request_id, payload)
                response = await handle.result()
                return JSONResponse(_completion_payload(request_id, response))
            except Exception as exc:  # noqa: BLE001 - map provider/runtime failures at the API boundary
                if handle is not None:
                    runtime.persist_scheduler_state(handle.state)
                status, code, message, kind = _map_exception(exc)
                return JSONResponse(
                    openai_error(message, code=code, request_id=request_id, error_type=kind),
                    status_code=status,
                )

        events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        handle = None
        try:
            handle = await runtime.submit(request_id, payload, event_queue=events)
            first_kind, first = await events.get()
            if first_kind != "chunk":
                await handle.result()
                raise RuntimeError("Lo stream è terminato senza chunk iniziale.")
        except asyncio.CancelledError:
            if handle is not None:
                await handle.cancel("client_disconnected_before_stream")
                runtime.persist_scheduler_state(handle.state)
            raise
        except Exception as exc:  # noqa: BLE001 - map scheduler/provider failures at the API boundary
            if handle is not None:
                runtime.persist_scheduler_state(handle.state)
            status, code, message, kind = _map_exception(exc)
            return JSONResponse(
                openai_error(message, code=code, request_id=request_id, error_type=kind),
                status_code=status,
            )

        async def stream_body():
            completion_id = f"chatcmpl-{request_id.replace('-', '')}"
            created = int(time.time())
            call_ids: dict[int, str] = {}
            saw_tool_call = False
            completed = False
            yield _sse_data(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "lar-auto",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
            )
            current_kind, current = "chunk", first
            emitted_call_ids: set[int] = set()
            try:
                while True:
                    if current_kind == "error":
                        _status, code, message, kind = _map_exception(current)
                        yield _sse_data(
                            openai_error(message, code=code, request_id=request_id, error_type=kind)
                        )
                        return
                    if current_kind == "cancelled":
                        return
                    if current_kind != "chunk":
                        return
                    message = dict(current.message)
                    delta: dict[str, Any] = {}
                    content = message.get("content")
                    if isinstance(content, str) and content:
                        delta["content"] = content
                    tool_calls = message.get("tool_calls") or []
                    if isinstance(tool_calls, list) and tool_calls:
                        saw_tool_call = True
                        deltas = []
                        for index, call in enumerate(tool_calls):
                            function = call.get("function") if isinstance(call, dict) else None
                            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                                continue
                            call_id = call_ids.setdefault(index, f"call_{uuid4().hex}")
                            arguments = function.get("arguments", {})
                            argument_text = (
                                arguments if isinstance(arguments, str)
                                else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                            )
                            call_delta: dict[str, Any] = {
                                "index": index,
                                "function": {"arguments": argument_text},
                            }
                            if index not in emitted_call_ids:
                                emitted_call_ids.add(index)
                                call_delta["id"] = call_id
                                call_delta["type"] = "function"
                                call_delta["function"]["name"] = function["name"]
                            deltas.append(call_delta)
                        if deltas:
                            delta["tool_calls"] = deltas
                    if delta:
                        yield _sse_data(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": "lar-auto",
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            }
                        )
                    if current.done:
                        try:
                            await handle.result()
                        except Exception as exc:  # noqa: BLE001 - emit terminal SSE error safely
                            _status, code, error_message, kind = _map_exception(exc)
                            yield _sse_data(
                                openai_error(error_message, code=code, request_id=request_id, error_type=kind)
                            )
                            return
                        usage = None
                        if current.usage is not None:
                            usage = _usage_dict(
                                current.usage.input_tokens, current.usage.output_tokens
                            )
                        if payload.stream_options and payload.stream_options.get("include_usage") and usage:
                            yield _sse_data(
                                {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": "lar-auto",
                                    "choices": [],
                                    "usage": usage,
                                }
                            )
                        finish_reason = "tool_calls" if saw_tool_call else (current.done_reason or "stop")
                        yield _sse_data(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": "lar-auto",
                                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                            }
                        )
                        yield b"data: [DONE]\n\n"
                        completed = True
                        return
                    current_kind, current = await events.get()
            except asyncio.CancelledError:
                await handle.cancel("client_disconnected_before_stream")
                runtime.persist_scheduler_state(handle.state)
                raise
            finally:
                if not completed:
                    await handle.cancel("client_disconnected")
                    runtime.persist_scheduler_state(handle.state)

        return StreamingResponse(
            stream_body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _sse_data(item: dict[str, Any]) -> bytes:
    encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    return f"data: {encoded}\n\n".encode()
