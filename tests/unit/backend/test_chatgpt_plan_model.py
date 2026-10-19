from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from CAD2BIM_backend.domain.role_memory import RoleMemory
from CAD2BIM_backend.infrastructure.chatgpt_model import (
    ModelCallCancelled,
    ModelProtocolError,
    ModelResponseFailed,
    ModelStreamInterrupted,
    create_chatgpt_plan_model,
)
from CAD2BIM_backend.infrastructure.input_token_count import count_request_input_tokens


def _response(status="completed"):
    return {
        "id": "resp_protocol", "object": "response", "created_at": 0,
        "model": "gpt-6.1-sol", "status": status, "error": None,
        "incomplete_details": None, "service_tier": "default",
        "reasoning": {"effort": "max", "context": "all_turns"},
        "output": [
            {"type": "reasoning", "id": "rs_protocol", "summary": [],
             "encrypted_content": "opaque-terminal-only"},
            {"type": "message", "role": "assistant", "id": "msg_protocol",
             "status": "completed", "phase": "commentary",
             "content": [{"type": "output_text", "text": "Inspect the image.", "annotations": []}]},
            {"type": "function_call", "id": "fc_protocol", "call_id": "call_protocol",
             "namespace": "component", "name": "inspect_image", "arguments": '{"label":"sample"}',
             "status": "completed"},
        ],
        "parallel_tool_calls": True, "tools": [], "tool_choice": "auto",
        "temperature": None, "top_p": None,
    }


def _events(response=None):
    response = response or _response()
    return [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}, "sequence_number": 0},
        {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_protocol", "summary": []}, "output_index": 0, "sequence_number": 1},
        {"type": "response.output_item.done", "item": response["output"][0], "output_index": 0, "sequence_number": 2},
        {"type": f"response.{response['status']}", "response": response, "sequence_number": 3},
    ]


def _transport(events, requests):
    async def respond(request):
        requests.append(json.loads(request.content))
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    return httpx.MockTransport(respond)


async def _token():
    return "protocol-fixture-only"


class _TextCallbacks(AsyncCallbackHandler):
    def __init__(self):
        self.events = []
        self.activities = []

    async def on_llm_start(self, serialized, prompts, *, run_id, tags=None, metadata=None, **kwargs):
        self.events.append(("start", run_id, tags, metadata))

    async def on_llm_new_token(self, token, *, run_id, **kwargs):
        if kwargs.get("activity_stage"):
            self.activities.append((token, run_id, kwargs))
        if kwargs.get("activity_stage") == "retry":
            self.events.append(("retry", run_id, kwargs["retry_attempt"]))
        else:
            self.events.append(("delta", run_id, token))

    async def on_llm_end(self, response, *, run_id, **kwargs):
        self.events.append(("end", run_id, response))

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.events.append(("error", run_id, error))


def _text_events():
    response = _response()
    message = response["output"][1]
    events = _events()[:-1]
    events.extend([
        {"type": "response.output_item.added", "item": {**message, "status": "in_progress", "content": []}, "output_index": 1},
        {"type": "response.content_part.added", "part": {"type": "output_text", "text": "", "annotations": []}, "item_id": message["id"], "output_index": 1, "content_index": 0},
        {"type": "response.output_text.delta", "delta": "Inspect ", "item_id": message["id"], "output_index": 1, "content_index": 0, "logprobs": []},
        {"type": "response.output_text.delta", "delta": "the image.", "item_id": message["id"], "output_index": 1, "content_index": 0, "logprobs": []},
        {"type": "response.output_item.done", "item": message, "output_index": 1},
        {"type": "response.completed", "response": response},
    ])
    return [{**event, "sequence_number": index} for index, event in enumerate(events)]


async def test_sdk_text_deltas_use_standard_callbacks_and_preserve_complete_response(tmp_path):
    callbacks = _TextCallbacks()
    requests = []
    events = _text_events()
    async with httpx.AsyncClient(transport=_transport(events, requests)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        message = await model.ainvoke([HumanMessage("Inspect")], config={
            "callbacks": [callbacks], "tags": ["role_main"],
            "metadata": {"main_message_id": "main-message-1"},
        })
    assert [event[0] for event in callbacks.events] == ["start", "delta", "delta", "end"]
    assert len({event[1] for event in callbacks.events}) == 1
    assert callbacks.events[0][2] == ["role_main"]
    assert callbacks.events[0][3]["main_message_id"] == "main-message-1"
    assert [event[2] for event in callbacks.events[1:3]] == ["Inspect ", "the image."]
    assert message.content[0]["encrypted_content"] == "opaque-terminal-only"
    assert message.content[1]["text"] == "Inspect the image."
    assert message.content[1]["phase"] == "commentary"
    assert message.content[1]["id"] == "msg_protocol"
    assert message.tool_calls[0]["id"] == "call_protocol"
    assert "main_message_id" not in json.dumps(requests)
    (tmp_path / "callback_events.json").write_text(json.dumps([
        {"event": event[0], "call_id": str(event[1]), "text": event[2] if event[0] == "delta" else None}
        for event in callbacks.events
    ], indent=2), encoding="utf-8")


async def test_stream_failure_closes_original_callback_identity_without_final_message(monkeypatch):
    callbacks = _TextCallbacks()
    events = _text_events()[:-1]
    requests, delays = [], []
    async def backoff(seconds):
        delays.append(seconds)
    monkeypatch.setattr("CAD2BIM_backend.infrastructure.chatgpt_model.asyncio.sleep", backoff)
    async with httpx.AsyncClient(transport=_transport(events, requests)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        with pytest.raises(ModelStreamInterrupted):
            await model.ainvoke([HumanMessage("Inspect")], config={
                "callbacks": [callbacks], "tags": ["role_main"],
                "metadata": {"main_message_id": "main-message-failed"},
            })
    assert [event[0] for event in callbacks.events] == [
        "start", "delta", "delta", "retry", "delta", "delta", "retry", "delta", "delta", "error",
    ]
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    assert delays == [1, 2]
    assert len({event[1] for event in callbacks.events}) == 1
    assert callbacks.events[0][3]["main_message_id"] == "main-message-failed"


async def test_interrupted_partial_stream_retries_same_call_and_discards_stale_tool_intent(monkeypatch):
    callbacks = _TextCallbacks()
    requests, delays = [], []
    first_events = _text_events()[:-1]
    first_events.append({"type": "response.output_item.done", "output_index": 2,
        "sequence_number": len(first_events), "item": {**_response()["output"][2],
        "id": "fc_stale", "call_id": "call_stale", "arguments": '{"label":"stale"}'}})

    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for event in first_events:
                yield ("data: " + json.dumps(event) + "\n\n").encode()
            raise httpx.RemoteProtocolError("incomplete chunked read")

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=InterruptedBody(), headers={"content-type": "text/event-stream"})
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in _text_events())
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def backoff(seconds):
        delays.append(seconds)
    monkeypatch.setattr("CAD2BIM_backend.infrastructure.chatgpt_model.asyncio.sleep", backoff)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        message = await model.ainvoke([HumanMessage("Inspect")], config={
            "callbacks": [callbacks], "tags": ["role_main"],
            "metadata": {"main_message_id": "main-retry"},
        })
    assert len(requests) == 2 and requests[0] == requests[1] and delays == [1]
    assert [event[0] for event in callbacks.events] == ["start", "delta", "delta", "retry", "delta", "delta", "end"]
    assert len({event[1] for event in callbacks.events}) == 1
    assert message.tool_calls[0]["id"] == "call_protocol"
    assert "stale" not in json.dumps(message.model_dump())


async def test_authentication_rejection_is_not_automatically_retried():
    from openai import AuthenticationError
    requests = []
    async def reject(request):
        requests.append(request)
        return httpx.Response(401, json={"error": {"code": "invalid_api_key", "message": "Unauthenticated"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        with pytest.raises(AuthenticationError):
            await model.ainvoke([HumanMessage("Inspect")])
    assert len(requests) == 1


@pytest.mark.parametrize("failure_kind", ["http_server", "failed_server"])
async def test_explicit_transient_server_failure_retries_only_inference(failure_kind, monkeypatch):
    requests, delays = [], []
    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1 and failure_kind == "http_server":
            return httpx.Response(503, json={"error": {"code": "server_error", "message": "Temporarily unavailable"}})
        response = _response()
        if len(requests) == 1:
            response = {**response, "status": "failed", "error": {"code": "server_error", "message": "Temporarily unavailable"}}
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in _events(response))
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    async def backoff(seconds):
        delays.append(seconds)
    monkeypatch.setattr("CAD2BIM_backend.infrastructure.chatgpt_model.asyncio.sleep", backoff)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        message = await model.ainvoke([HumanMessage("Inspect")])
    assert len(requests) == 2 and requests[0] == requests[1] and delays == [1]
    assert message.tool_calls[0]["id"] == "call_protocol"


async def test_function_argument_deltas_emit_one_activity_without_private_arguments():
    callbacks = _TextCallbacks()
    events = _text_events()
    call = _response()["output"][2]
    events.insert(-1, {"type": "response.output_item.added", "output_index": 2,
        "item": {**call, "arguments": "", "status": "in_progress"}})
    for text in ['{"private":', '"source body"}']:
        events.insert(-1, {"type": "response.function_call_arguments.delta", "output_index": 2,
            "item_id": call["id"], "delta": text})
    events.insert(-1, {"type": "response.output_item.done", "output_index": 2, "item": call})
    events = [{**event, "sequence_number": index} for index, event in enumerate(events)]
    async with httpx.AsyncClient(transport=_transport(events, [])) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        message = await model.ainvoke([HumanMessage("Inspect")], config={"callbacks": [callbacks]})
    assert len(callbacks.activities) == 1
    assert callbacks.activities[0][0] == ""
    assert callbacks.activities[0][2]["activity_stage"] == "tool_arguments"
    assert "private" not in json.dumps([(event[0], event[2]) for event in callbacks.events if event[0] == "delta"])
    assert message.tool_calls[0]["id"] == "call_protocol"


async def test_locked_sdk_terminal_loss_and_complete_message_replay(tmp_path):
    requests = []
    events = _events()
    async with httpx.AsyncClient(transport=_transport(events, requests)) as client:
        standard = ChatOpenAI(
            model="gpt-6.1-sol", api_key=_token, http_async_client=client,
            use_responses_api=True, output_version="responses/v1", streaming=True,
            store=False, max_retries=0, http_socket_options=(),
        )
        original = await standard.ainvoke([HumanMessage("Protocol fixture")])
        assert not any(block.get("encrypted_content") for block in original.content)
        fixed = create_chatgpt_plan_model(_token, http_async_client=client)
        image = {"type": "image", "base64": "aW1hZ2U=", "mime_type": "image/png"}
        user = HumanMessage(content=[{"type": "text", "text": "Inspect"}, image])
        instruction = SystemMessage("Use the current skill")
        message = await fixed.ainvoke([instruction, user])
        assert message.content[0] == _response()["output"][0]
        assert message.content[1]["phase"] == "commentary"
        assert message.content[2] == _response()["output"][2]
        assert message.tool_calls == [{"type": "tool_call", "id": "call_protocol", "name": "inspect_image", "args": {"label": "sample"}}]
        assert message.response_metadata["reasoning"]["context"] == "all_turns"
        assert instruction.type == "system"
        memory = RoleMemory(messages=[instruction, user, message, ToolMessage(
            content=[{"type": "text", "text": "Rendered"}, image], tool_call_id="call_protocol",
        )])
        restored = RoleMemory.model_validate_json(memory.model_dump_json())
        payload = fixed._get_request_payload(restored.messages)
        assert payload["input"][0]["role"] == "developer"
        assert payload["input"][1]["content"][1]["image_url"] == "data:image/png;base64,aW1hZ2U="
        assert next(item for item in payload["input"] if item.get("type") == "reasoning")["encrypted_content"] == "opaque-terminal-only"
        tool_result = payload["input"][-1]
        assert tool_result["call_id"] == "call_protocol"
        assert tool_result["output"][1]["type"] == "input_image"
        streamed = [part async for part in fixed.astream([user])]
        assert len(streamed) == 1 and streamed[0].content == message.content
        assert streamed[0].tool_calls == message.tool_calls
    for name, value in {"protocol_events": events, "standard_ai_message": original.model_dump(mode="json"),
                        "fixed_ai_message": message.model_dump(mode="json"), "replay_payload": payload}.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(value, indent=2), encoding="utf-8")


async def test_route_constraints_and_standard_tool_schema():
    model = create_chatgpt_plan_model(_token)
    schema = {"type": "function", "function": {"name": "inspect_image", "description": "Inspect an image", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    bound = model.bind_tools([schema])
    payload = model._get_request_payload([HumanMessage("Inspect")], **bound.kwargs)
    assert payload["tools"][0]["tools"][0]["parameters"] == schema["function"]["parameters"]
    assert payload["store"] is False and payload["stream"] is True
    assert payload["service_tier"] == "default"
    for kwargs in ({"max_tokens": 10}, {"previous_response_id": "resp_old"}, {"temperature": 0}, {"store": True}, {"service_tier": "ultrafast"}, {"stop": ["END"]}):
        with pytest.raises(ModelProtocolError):
            model._get_request_payload([HumanMessage("Inspect")], **kwargs)


@pytest.mark.parametrize("model_name,effort", [("gpt-6.1-sol", "high"), ("gpt-6-astra", "medium"), ("gpt-6-astra", "high")])
def test_selected_reasoning_effort_reaches_actual_provider_payload(model_name, effort):
    model = create_chatgpt_plan_model(_token, model_name=model_name, reasoning_effort=effort)
    payload = model._get_request_payload([HumanMessage("Inspect")])
    assert payload["model"] == model_name
    assert payload["reasoning"] == {"effort": effort, "context": "all_turns"}
    for invalid in ({"effort": "none", "context": "all_turns"}, {"effort": effort, "context": "last_turn"}):
        with pytest.raises(ModelProtocolError):
            model._get_request_payload([HumanMessage("Inspect")], reasoning=invalid)


async def test_official_count_uses_same_final_bound_payload_and_separate_api_key():
    count_requests, inference_requests = [], []

    def respond(request):
        count_requests.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 321})

    image = {"type": "image", "base64": "aW1hZ2U=", "mime_type": "image/png"}
    messages = [
        SystemMessage("Actual Prompt, Skill directory, conversation summary and tool-chain summary."),
        HumanMessage(content=[{"type": "text", "text": "Inspect"}, image]),
        AIMessage(content=[{"type": "reasoning", "id": "rs_saved", "summary": [],
                            "encrypted_content": "opaque-saved"}],
                  tool_calls=[{"id": "old_call", "name": "inspect_image", "args": {}}]),
        ToolMessage(tool_call_id="old_call", content=[{"type": "text", "text": "Result"}, image]),
    ]
    schema = {"type": "function", "function": {"name": "inspect_image", "description": "Inspect",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as count_http, \
            httpx.AsyncClient(transport=_transport(_events(), inference_requests)) as inference_http:
        async with AsyncOpenAI(api_key="count-fixture-only", http_client=count_http, max_retries=0, timeout=10) as counter:
            model = create_chatgpt_plan_model(_token, http_async_client=inference_http,
                                              input_token_client=counter)
            bound = model.bind_tools([schema], tool_choice="inspect_image").bind(
                include=["reasoning.encrypted_content"],
            ).with_config(tags=["unit"])
            count = await count_request_input_tokens(bound, messages)
            await bound.ainvoke(messages)
    assert (count.tokens, count.source, count.fallback_reason) == (321, "official", None)
    assert len(count_requests) == len(inference_requests) == 1
    authorization, counted = count_requests[0]
    assert authorization == "Bearer count-fixture-only"
    actual = inference_requests[0]
    assert set(actual) - set(counted) == {"store", "stream", "service_tier", "include"}
    assert counted == {key: actual[key] for key in counted}
    assert counted["tools"][0]["type"] == "namespace"
    assert counted["tool_choice"]["namespace"] == "component"
    assert counted["input"][0]["role"] == "developer"
    assert next(item for item in counted["input"] if item.get("type") == "reasoning")["encrypted_content"] == "opaque-saved"
    assert counted["input"][-1]["output"][1]["type"] == "input_image"
    assert counted["reasoning"] == {"effort": "max", "context": "all_turns"}
    assert messages[0].type == "system"


@pytest.mark.parametrize("failure", ["unavailable", "unauthorized", "timeout", "sdk"])
async def test_count_failure_is_marked_local_estimate_without_inference_or_secret_logs(failure, caplog):
    from types import SimpleNamespace

    calls = []

    async def count(**kwargs):
        calls.append(kwargs)
        if failure == "sdk":
            raise RuntimeError("SECRET-PAYLOAD-AND-KEY")
        if failure == "timeout":
            raise TimeoutError("SECRET-PAYLOAD-AND-KEY")
        from openai import AuthenticationError
        raise AuthenticationError("SECRET-PAYLOAD-AND-KEY", response=httpx.Response(
            401, request=httpx.Request("POST", "https://api.openai.com/v1/responses/input_tokens")), body={})

    client = None if failure == "unavailable" else SimpleNamespace(
        responses=SimpleNamespace(input_tokens=SimpleNamespace(count=count)),
    )
    model = create_chatgpt_plan_model(_token, input_token_client=client)
    result = await model.acount_input_tokens([HumanMessage("Actual required input")])
    assert result.tokens > 0 and result.source == "local_estimate" and result.fallback_reason
    assert "source=local_estimate" in caplog.text and "fallback_reason=" in caplog.text
    assert "SECRET-PAYLOAD-AND-KEY" not in caplog.text and "Actual required input" not in caplog.text
    assert len(calls) == (failure != "unavailable")
    with pytest.raises(ModelProtocolError):
        await model.acount_input_tokens([HumanMessage("Invalid request")], temperature=0)
    assert len(calls) == (failure != "unavailable")


async def test_count_cancellation_propagates_without_local_fallback(caplog):
    from types import SimpleNamespace

    async def count(**_kwargs):
        raise asyncio.CancelledError

    model = create_chatgpt_plan_model(_token, input_token_client=SimpleNamespace(
        responses=SimpleNamespace(input_tokens=SimpleNamespace(count=count)),
    ))
    with pytest.raises(asyncio.CancelledError):
        await model.acount_input_tokens([HumanMessage("Keep this message")])
    assert "local_estimate" not in caplog.text


@pytest.mark.parametrize("model_name,configured_tier,returned_tier", [
    ("gpt-6.1-sol", "fast", "priority"),
    ("gpt-6-astra", "priority", "fast"),
    ("gpt-6-astra", "fast", "default"),
])
async def test_fast_request_reaches_transport_and_reports_actual_tier(
    model_name, configured_tier, returned_tier, caplog,
):
    response = {**_response(), "model": model_name, "service_tier": returned_tier}
    requests = []
    async with httpx.AsyncClient(transport=_transport(_events(response), requests)) as client:
        model = create_chatgpt_plan_model(
            _token, http_async_client=client, model_name=model_name, service_tier=configured_tier,
        )
        message = await model.ainvoke([HumanMessage("Inspect")])
    assert len(requests) == 1
    assert requests[0]["model"] == model_name
    assert requests[0]["service_tier"] == "priority"
    assert message.response_metadata["requested_service_tier"] == "priority"
    assert message.response_metadata["service_tier"] == returned_tier
    assert message.tool_calls[0]["id"] == "call_protocol"
    assert ("Fast is not confirmed" in caplog.text) == (returned_tier == "default")


@pytest.mark.parametrize("terminal", ["failed", "incomplete", "interrupted", "cancelled", "protocol"])
async def test_non_success_is_not_an_ai_message(terminal):
    requests = []
    response = _response(terminal)
    if terminal == "failed": response["error"] = {"code": "subscription_sharing_usage_limit_exceeded", "message": "Limit reached"}
    events = _events(response)
    if terminal in {"interrupted", "cancelled", "protocol"}: events = events[:1]
    if terminal == "protocol": events.append({"type": "error", "code": "invalid_request", "message": "Protocol", "param": None, "sequence_number": 1})
    transport = _transport(events, requests)
    if terminal == "cancelled":
        async def cancel(request): raise asyncio.CancelledError()
        transport = httpx.MockTransport(cancel)
    error = {"failed": ModelResponseFailed, "incomplete": ModelResponseFailed,
             "interrupted": ModelStreamInterrupted, "cancelled": ModelCallCancelled,
             "protocol": ModelProtocolError}[terminal]
    async with httpx.AsyncClient(transport=transport) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        with pytest.raises(error): await model.ainvoke([HumanMessage("Inspect")])
    assert len(requests) == (3 if terminal == "interrupted" else 0 if terminal == "cancelled" else 1)


async def test_actual_outer_task_cancellation_keeps_asyncio_semantics():
    started = asyncio.Event()
    async def wait_for_cancel(request):
        started.set()
        await asyncio.Future()
    async with httpx.AsyncClient(transport=httpx.MockTransport(wait_for_cancel)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        task = asyncio.create_task(model.ainvoke([HumanMessage("Inspect")]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task


async def test_actual_stream_task_cancellation_keeps_asyncio_semantics():
    started = asyncio.Event()
    async def wait_for_cancel(request):
        started.set()
        await asyncio.Future()
    async with httpx.AsyncClient(transport=httpx.MockTransport(wait_for_cancel)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        async def read_stream():
            return [chunk async for chunk in model.astream([HumanMessage("Inspect")])]
        task = asyncio.create_task(read_stream())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task


async def test_http_rejection_preserves_sdk_diagnostics_without_retry():
    from openai import BadRequestError
    requests = []
    async def reject(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"code": "subscription_sharing_unsupported_capability", "param": "service_tier", "message": "Unsupported"}}, headers={"x-request-id": "req_protocol"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        with pytest.raises(BadRequestError) as failure: await model.ainvoke([HumanMessage("Inspect")])
        assert failure.value.request_id == "req_protocol"
        assert failure.value.param == "service_tier"
        assert len(requests) == 1


async def test_live_stream_shape_with_empty_completed_output_retains_done_items():
    response = _response()
    events = _events()
    events[-1]["response"] = {**response, "output": []}
    events.insert(-1, {"type": "response.output_item.done", "item": response["output"][1], "output_index": 1, "sequence_number": 3})
    events.insert(-1, {"type": "response.output_item.done", "item": response["output"][2], "output_index": 2, "sequence_number": 4})
    events[-1]["sequence_number"] = 5
    async with httpx.AsyncClient(transport=_transport(events, [])) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        message = await model.ainvoke([HumanMessage("Inspect")])
    assert message.content[0]["encrypted_content"] == "opaque-terminal-only"
    assert message.content[1]["phase"] == "commentary"
    assert message.content[2]["namespace"] == "component"
    assert message.tool_calls[0]["id"] == "call_protocol"


async def test_complete_terminal_keeps_native_fields_and_rejects_identity_conflict():
    events = _events()
    events[2]["item"] = {**events[2]["item"], "encrypted_content": "earlier-done-content"}
    async with httpx.AsyncClient(transport=_transport(events, [])) as client:
        model = create_chatgpt_plan_model(_token, http_async_client=client)
        message = await model.ainvoke([HumanMessage("Inspect")])
        assert message.content[0]["encrypted_content"] == "opaque-terminal-only"
        events[2]["item"]["id"] = "rs_conflicting"
        with pytest.raises(ModelProtocolError, match="identities conflict"):
            await model.ainvoke([HumanMessage("Inspect")])
