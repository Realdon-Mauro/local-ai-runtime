"""OpenAI ↔ Ollama mapping for the explicitly supported chat contract."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from lar.api.schemas import ChatCompletionRequest


class CompatibilityError(ValueError):
    """A valid OpenAI field cannot be represented by the Ollama v1 contract."""


MAX_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


def to_ollama_request(request: ChatCompletionRequest) -> dict[str, Any]:
    if request.model != "lar-auto":
        raise CompatibilityError("Il solo modello esposto è lar-auto.")
    if request.tool_choice not in (None, "auto", "none"):
        raise CompatibilityError("Ollama v1 non supporta tool_choice required/function.")
    if request.response_format is not None:
        kind = request.response_format.get("type")
        if kind == "json_object":
            output_format: Any = "json"
        elif kind == "json_schema":
            output_format = request.response_format.get("json_schema", {}).get("schema")
            if not isinstance(output_format, dict):
                raise CompatibilityError("response_format json_schema richiede uno schema valido.")
        else:
            raise CompatibilityError(f"response_format non supportato: {kind!r}.")
    else:
        output_format = None

    tool_names: dict[str, str] = {}
    messages = []
    for item in request.messages:
        role = "system" if item.role == "developer" else item.role
        content, images = _message_content(item.content)
        message: dict[str, Any] = {"role": role, "content": content}
        if images:
            message["images"] = images
        if item.name is not None:
            message["name"] = item.name
        if item.role == "assistant" and item.tool_calls:
            calls = []
            for call in item.tool_calls:
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not isinstance(function, dict):
                    raise CompatibilityError("assistant tool_call malformata.")
                name = function.get("name")
                arguments = function.get("arguments", "{}")
                if not isinstance(name, str) or not isinstance(arguments, str):
                    raise CompatibilityError("tool_call name/arguments non validi.")
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise CompatibilityError("tool_call arguments non è JSON valido.") from exc
                if not isinstance(parsed, dict):
                    raise CompatibilityError("tool_call arguments deve essere un oggetto JSON.")
                tool_names[call_id] = name
                calls.append({"function": {"name": name, "arguments": parsed}})
            message["tool_calls"] = calls
        if item.role == "tool":
            if not item.tool_call_id or item.tool_call_id not in tool_names:
                raise CompatibilityError("Risposta tool senza tool_call_id assistant correlato.")
            message["tool_name"] = tool_names[item.tool_call_id]
        messages.append(message)

    payload: dict[str, Any] = {"model": "lar-auto", "messages": messages, "stream": request.stream}
    if request.tools and request.tool_choice != "none":
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.function.name,
                    "description": tool.function.description,
                    "parameters": tool.function.parameters,
                },
            }
            for tool in request.tools
        ]
    if output_format is not None:
        payload["format"] = output_format
    options: dict[str, Any] = {}
    max_output = request.max_completion_tokens or request.max_tokens
    if max_output is not None:
        options["num_predict"] = max_output
    if request.temperature is not None:
        options["temperature"] = request.temperature
    if request.top_p is not None:
        options["top_p"] = request.top_p
    if request.stop is not None:
        options["stop"] = [request.stop] if isinstance(request.stop, str) else request.stop
    if options:
        payload["options"] = options
    return payload


def _message_content(value: str | list[dict[str, Any]] | None) -> tuple[str, list[str]]:
    if value is None:
        return "", []
    if isinstance(value, str):
        return value, []
    text_parts: list[str] = []
    images: list[str] = []
    for part in value:
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        elif kind in {"image_url", "input_image"}:
            image = part.get("image_url", part)
            url = image.get("url") if isinstance(image, dict) else None
            if not isinstance(url, str) or not url.startswith("data:"):
                raise CompatibilityError("Le immagini remote non sono scaricate dal gateway; usare data URI.")
            header, separator, encoded = url.partition(",")
            if not separator or ";base64" not in header:
                raise CompatibilityError("Immagine inline deve usare data URI base64.")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise CompatibilityError("Immagine base64 non valida.") from exc
            if len(decoded) > MAX_INLINE_IMAGE_BYTES:
                raise CompatibilityError("Immagine inline supera il limite di 20 MiB.")
            images.append(base64.b64encode(decoded).decode("ascii"))
        else:
            raise CompatibilityError(f"Content part non supportata: {kind!r}.")
    return "".join(text_parts), images


def from_ollama_message(message: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    content = message.get("content")
    if not isinstance(content, str):
        raise CompatibilityError("Ollama assistant content malformato.")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise CompatibilityError("Ollama tool_calls malformato.")
    openai_calls = []
    for index, call in enumerate(calls):
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise CompatibilityError("Ollama function tool call malformata.")
        arguments = function.get("arguments", {})
        argument_text = (
            arguments if isinstance(arguments, str)
            else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        )
        openai_calls.append(
            {
                "index": index,
                "id": f"call_{uuid_for_call(index, function['name'], argument_text)}",
                "type": "function",
                "function": {"name": function["name"], "arguments": argument_text},
            }
        )
    return content or None, openai_calls


def uuid_for_call(index: int, name: str, arguments: str) -> str:
    """Create a per-response stable opaque ID without exposing tool arguments."""
    from uuid import uuid4

    del index, name, arguments
    return uuid4().hex
