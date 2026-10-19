"""ChatGPT-plan Responses calls using the existing LangChain message protocol."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import _construct_lc_result_from_responses_api
from openai.types.responses import Response
from openai.types.responses.input_token_count_params import InputTokenCountParams
from openai import APIConnectionError, APIStatusError, AuthenticationError
from pydantic import Field

from .input_token_count import InputTokenCount, local_input_token_count


class ModelProtocolError(ValueError):
    """The request or stream violates the approved ChatGPT-plan protocol."""


class ModelResponseFailed(RuntimeError):
    """The provider reported a failed or incomplete terminal response."""

    def __init__(self, status: str, response: Any) -> None:
        self.status = status
        self.response_id = response.id
        self.error = response.error
        self.incomplete_details = response.incomplete_details
        code = getattr(response.error, "code", None) or status
        super().__init__(f"Model response {status}: {code}")


class ModelStreamInterrupted(RuntimeError):
    """The stream ended without a successful terminal event; do not save partial output."""


class ModelCallCancelled(RuntimeError):
    """A nested SDK operation was cancelled; LangChain must not treat it as a result."""


def _retryable_model_error(error: Exception) -> bool:
    if isinstance(error, (APIConnectionError, httpx.TransportError, ModelStreamInterrupted)):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code >= 500
    if isinstance(error, ModelResponseFailed):
        return error.status == "failed" and getattr(error.error, "code", None) in {
            "server_error", "internal_error", "service_unavailable",
        }
    return False


def model_execution_error(error: Exception) -> str | None:
    """Classify model failures for the existing Run pause/recovery owner.

    Only safe reason codes are exposed; the original SDK error remains in logs.
    This does not classify tool execution failures as model retries.
    """
    if isinstance(error, ModelStreamInterrupted):
        return "model_network_interrupted"
    if isinstance(error, AuthenticationError):
        return "model_authentication_failed"
    if isinstance(error, ModelResponseFailed):
        return "model_response_failed"
    if isinstance(error, ModelProtocolError):
        return "model_protocol_error"
    if isinstance(error, ModelCallCancelled):
        return "model_call_cancelled"
    if isinstance(error, APIStatusError):
        return "model_request_failed"
    # Authentication uses the same supplier as normal inference, before HTTP.
    from .chatgpt_auth import ChatGPTAuthError

    if isinstance(error, ChatGPTAuthError):
        return "model_authentication_failed"
    return None


_UNSUPPORTED = frozenset({
    "background", "conversation", "max_output_tokens", "max_tool_calls", "metadata",
    "moderation", "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier",
    "temperature", "top_logprobs", "top_p", "truncation", "user", "previous_response_id",
})


class ChatGPTPlanModel(ChatOpenAI):
    """A thin route specialization; LangChain owns input and full-response conversion.

    HTTP always streams. Only a complete response becomes an authoritative message;
    the local protocol fixture reproduces terminal-only reasoning loss in the default
    LangChain delta aggregation. Text deltas use ordinary LangChain callbacks.
    Cancellation remains asyncio.CancelledError and SDK HTTP errors retain diagnostics.
    """

    input_token_client: Any = Field(default=None, exclude=True, repr=False)

    def _get_request_payload(
        self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: Any,
    ) -> dict:
        if stop is not None or self.use_previous_response_id:
            raise ModelProtocolError("Stop sequences and server-side continuation are unsupported")
        payload = super()._get_request_payload(input_, **kwargs)
        forbidden = _UNSUPPORTED.intersection(payload)
        if forbidden:
            raise ModelProtocolError(f"Unsupported ChatGPT-plan fields: {', '.join(sorted(forbidden))}")
        reasoning = payload.get("reasoning") or {}
        if (payload.get("model") not in {"gpt-6.1-sol", "gpt-6-astra"}
            or reasoning.get("effort") not in {"low", "medium", "high", "xhigh", "max"}
            or reasoning != {"effort": reasoning.get("effort"), "context": "all_turns"}):
            raise ModelProtocolError("Use gpt-6.1-sol or gpt-6-astra with a supported effort and all_turns reasoning")
        if payload.get("store") is not False:
            raise ModelProtocolError("Use store=false for ChatGPT-plan requests")
        if payload.get("service_tier") not in {"default", "fast", "priority"}:
            raise ModelProtocolError("Choose Standard (default) or Fast (fast/priority)")
        # Both spellings are current API contract; the SIWC route rejects 'fast'.
        if payload["service_tier"] == "fast":
            payload["service_tier"] = "priority"
        payload["stream"] = True
        if not isinstance(payload.get("input"), list):
            raise ModelProtocolError("The full required message history must be an input array")
        for item in payload["input"]:
            if item.get("role") == "system":
                item["role"] = "developer"
        if tools := payload.get("tools"):
            if not all(tool.get("type") in {"function", "custom"} for tool in tools):
                raise ModelProtocolError("This model entry accepts local function/custom tools")
            payload["tools"] = [{
                "type": "namespace", "name": "component",
                "description": "The current application's local tools.", "tools": tools,
            }]
            if isinstance(choice := payload.get("tool_choice"), dict):
                payload["tool_choice"] = {**choice, "namespace": "component"}
        return payload

    async def acount_input_tokens(
        self, messages: list[BaseMessage], **kwargs: Any,
    ) -> InputTokenCount:
        # Invalid inference input must still fail at the same protocol boundary;
        # it must not become a successful count of a simplified request.
        payload = self._get_request_payload(messages, **kwargs)
        if self.input_token_client is None:
            return local_input_token_count(
                messages, payload.get("tools", ()), reason="project_api_key_unavailable",
            )
        # Count accepts the inference input fields, not create-only controls such
        # as stream/store/service_tier/include. Preserve nested input verbatim.
        count_payload = {key: value for key, value in payload.items()
                         if key in InputTokenCountParams.__annotations__}
        try:
            response = await self.input_token_client.responses.input_tokens.count(**count_payload)
            tokens = response.input_tokens
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise ValueError("Invalid input-token count response")
        except Exception as exc:
            # Exception messages and SDK response bodies may contain request
            # material. Only safe diagnostic categories enter budget logs.
            reason = type(exc).__name__
            if isinstance(status := getattr(exc, "status_code", None), int):
                reason += f":status={status}"
            return local_input_token_count(messages, payload.get("tools", ()), reason=reason)
        result = InputTokenCount(tokens, "official")
        logging.getLogger(__name__).info(
            "Input token count source=%s tokens=%s model=%s", result.source, result.tokens, payload["model"],
        )
        return result

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        for attempt in range(1, 4):
            try:
                return await self._agenerate_attempt(payload, run_manager)
            except Exception as error:
                if not _retryable_model_error(error):
                    raise
                if attempt == 3:
                    # Mark the failure at the model boundary so Runner cannot
                    # misclassify an unrelated tool's transport exception.
                    raise ModelStreamInterrupted(
                        f"Model inference interrupted after 3 attempts: {type(error).__name__}",
                    ) from error
                logging.getLogger(__name__).warning(
                    "Model transport interrupted (%s); retrying inference attempt %s/3.",
                    type(error).__name__, attempt + 1,
                )
                if run_manager is not None:
                    await run_manager.on_llm_new_token(
                        "", activity_stage="retry", retry_attempt=attempt + 1, reset_text=True,
                    )
                await asyncio.sleep(attempt)
        raise AssertionError("model attempt loop exhausted without result or error")

    async def _agenerate_attempt(
        self, payload: dict[str, Any], run_manager: AsyncCallbackManagerForLLMRun | None,
    ) -> ChatResult:
        # Every attempt has fresh native output. A previous partial response is
        # never merged into a complete response or executed as a tool intent.
        completed = None
        finished_output: dict[int, Any] = {}
        preparing_tool_arguments = False
        try:
            async with self.root_async_client.responses.stream(**{
                key: value for key, value in payload.items() if key != "stream"
            }) as stream:
                async for event in stream:
                    if event.type == "response.function_call_arguments.delta" and run_manager is not None and not preparing_tool_arguments:
                        preparing_tool_arguments = True
                        await run_manager.on_llm_new_token("", activity_stage="tool_arguments")
                    if event.type == "response.output_text.delta" and run_manager is not None:
                        await run_manager.on_llm_new_token(event.delta)
                    if event.type == "response.output_item.done":
                        finished_output[event.output_index] = event.item
                    if event.type in {"response.failed", "response.incomplete"}:
                        raise ModelResponseFailed(event.response.status, event.response)
                    if event.type == "error":
                        raise ModelProtocolError(f"Provider stream error: {event.code}")
                    if event.type == "response.completed":
                        completed = event.response
                if completed is None:
                    raise ModelStreamInterrupted("Stream ended without response.completed")
                response = await stream.get_final_response()
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            # langchain-core 1.4.9's batch gather handles Exception, not BaseException.
            # A nested cancellation must not reach its successful-generation branch.
            # Cancelling the outer ainvoke task still propagates asyncio.CancelledError.
            raise ModelCallCancelled("Model transport operation was cancelled") from None
        if response.status != "completed":
            raise ModelProtocolError("Completed event has an inconsistent response status")
        # The live SIWC stream completed with output=[] despite finalized items.
        # Retain those SDK-native objects verbatim, without item-type conversion.
        output = list(response.output)
        for index, item in sorted(finished_output.items()):
            if index > len(output):
                raise ModelProtocolError("Finalized response output has an index gap")
            if index == len(output):
                output.append(item)
            elif output[index].id != item.id:
                raise ModelProtocolError("Finalized and terminal output identities conflict")
            # A non-empty terminal output is already complete; never replace its
            # native fields with an earlier finalized item.
        if len(output) != len(response.output):
            # Use the SDK's non-generic Response for native finalized objects;
            # its ParsedResponse[TypeVar] serializer rejects message done items.
            response = Response.model_construct(**{**dict(response), "output": output})
        result = _construct_lc_result_from_responses_api(response, output_version="responses/v1")
        result.generations[0].message.response_metadata["requested_service_tier"] = payload["service_tier"]
        if payload["service_tier"] == "priority" and response.service_tier not in {"priority", "fast"}:
            logging.getLogger(__name__).warning(
                "Requested Fast for %s; provider returned service_tier=%s. Fast is not confirmed.",
                payload["model"], response.service_tier,
            )
        if response.reasoning is not None:
            result.generations[0].message.response_metadata["reasoning"] = (
                response.reasoning.model_dump(exclude_none=True, mode="json")
            )
        return result

    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        result = await self._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(
            **message.model_dump(exclude={"type"}), chunk_position="last",
        ))

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise ModelProtocolError("Use the async model entry with the async credential supplier")

    def _stream(self, *args: Any, **kwargs: Any):
        raise ModelProtocolError("Use the async model entry with the async credential supplier")


def create_chatgpt_plan_model(
    token_supplier: Callable[[], Awaitable[str]], *, http_async_client: Any = None,
    input_token_client: Any = None,
    request_timeout_seconds: float = 300,
    model_name: str = "gpt-6.1-sol", service_tier: str = "default",
    reasoning_effort: str = "max",
) -> ChatGPTPlanModel:
    """Keep inference on SIWC; an independent project-key client may count inputs."""
    return ChatGPTPlanModel(
        model=model_name, api_key=token_supplier, base_url="https://api.openai.com/v1",
        use_responses_api=True, use_previous_response_id=False, output_version="responses/v1",
        store=False, reasoning={"effort": reasoning_effort, "context": "all_turns"},
        service_tier=service_tier, max_retries=0, timeout=request_timeout_seconds,
        cache=False, http_async_client=http_async_client, http_socket_options=(),
        input_token_client=input_token_client,
    )
