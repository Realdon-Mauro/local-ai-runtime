"""Supported OpenAI Chat Completions request/response boundary for v1."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OpenAIMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class OpenAIFunction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    strict: bool | None = None


class OpenAITool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: OpenAIFunction


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[OpenAIMessage] = Field(min_length=1)
    tools: list[OpenAITool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    stop: str | list[str] | None = None
    n: int = Field(default=1, ge=1, le=1)
    user: str | None = None

    @model_validator(mode="after")
    def validate_token_limits(self) -> ChatCompletionRequest:
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Usare max_tokens oppure max_completion_tokens, non entrambi.")
        return self


class OpenAIModel(BaseModel):
    id: str = "lar-auto"
    object: str = "model"
    created: int = 0
    owned_by: str = "lar"


class OpenAIModelsResponse(BaseModel):
    object: str = "list"
    data: list[OpenAIModel]


class OpenCodeObservation(BaseModel):
    """Privacy allowlist for one observed OpenCode event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["plugin", "listener", "recovery"]
    native_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    workflow_id: str | None = Field(default=None, min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    observed_at: datetime
    status: Literal["started", "completed", "failed", "incomplete", "unknown"] = "unknown"
    tool_category: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    model: str | None = Field(default=None, max_length=256)
    duration_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    correlation_status: Literal["native", "unmatched", "unknown", "conflicted"] = "unknown"
    history_status: Literal["complete", "incomplete", "unknown"] = "unknown"
