import base64

import pytest

from lar.api.compat import CompatibilityError, to_ollama_request
from lar.api.schemas import ChatCompletionRequest


def test_openai_request_maps_tools_options_and_response_format():
    request = ChatCompletionRequest.model_validate(
        {
            "model": "lar-auto",
            "messages": [{"role": "developer", "content": "Be precise"}, {"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "auto",
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 100,
            "temperature": 0.2,
            "top_p": 0.8,
            "stop": "END",
        }
    )
    ollama = to_ollama_request(request)
    assert ollama["messages"][0]["role"] == "system"
    assert ollama["tools"][0]["function"]["name"] == "lookup"
    assert ollama["format"] == "json"
    assert ollama["options"] == {
        "num_predict": 100,
        "temperature": 0.2,
        "top_p": 0.8,
        "stop": ["END"],
    }


def test_openai_tool_history_maps_ids_to_ollama_tool_name():
    request = ChatCompletionRequest.model_validate(
        {
            "model": "lar-auto",
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"}}
                ]},
                {"role": "tool", "tool_call_id": "call-1", "content": "found"},
            ],
        }
    )
    messages = to_ollama_request(request)["messages"]
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == {"q": "x"}
    assert messages[1]["tool_name"] == "lookup"


def test_unsupported_openai_fields_are_rejected_explicitly():
    required = ChatCompletionRequest.model_validate(
        {
            "model": "lar-auto",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": "required",
        }
    )
    with pytest.raises(CompatibilityError, match="tool_choice"):
        to_ollama_request(required)

    wrong_model = ChatCompletionRequest(
        model="physical", messages=[{"role": "user", "content": "x"}]
    )
    with pytest.raises(CompatibilityError, match="lar-auto"):
        to_ollama_request(wrong_model)


def test_remote_image_url_is_rejected_and_inline_data_is_validated():
    remote = ChatCompletionRequest(
        model="lar-auto",
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}],
    )
    with pytest.raises(CompatibilityError, match="remote"):
        to_ollama_request(remote)
    encoded = base64.b64encode(b"img").decode()
    inline = ChatCompletionRequest(
        model="lar-auto",
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]}],
    )
    assert to_ollama_request(inline)["messages"][0]["images"] == [encoded]
