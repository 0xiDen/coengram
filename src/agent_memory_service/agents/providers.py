"""Model-provider adapters kept behind the application Agent Runtime boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import BaseModel


class AgentProviderError(RuntimeError):
    """A model adapter failed without exposing provider credentials or payloads."""


class ProviderDependencyUnavailable(AgentProviderError):
    """An optional production provider dependency is not installed."""


@runtime_checkable
class AgentModelProvider(Protocol):
    """Application-facing subset of ActiveGraph's provider protocol."""

    default_model: str
    provider_id: str
    adapter_id: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        output_schema: type | None,
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        structured_output_mode: str = "prompt",
    ) -> Any: ...

    def estimate_cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> Decimal: ...

    def count_tokens(
        self,
        *,
        system: str,
        messages: list[Any],
        model: str,
    ) -> int: ...

    def supports_native_structured_output(self, model: str) -> bool: ...

    def recognizes_model(self, name: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    raw_text: str
    parsed: Any | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_seconds: float
    model: str
    finish_reason: str
    tool_calls: list[Any] | None = None


@dataclass(frozen=True, slots=True)
class RecordedCompletion:
    parsed: Any | None
    raw_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    finish_reason: str = "end_turn"


@dataclass(frozen=True, slots=True)
class ProviderCall:
    system: str
    messages: tuple[Any, ...]
    model: str
    max_tokens: int
    temperature: float
    top_p: float
    output_schema: type | None
    timeout_seconds: float
    tools: tuple[dict[str, Any], ...]
    structured_output_mode: str


def _response(
    *,
    raw_text: str,
    parsed: Any | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    model: str,
    finish_reason: str,
    latency_seconds: float = 0.0,
    tool_calls: list[Any] | None = None,
    provider_meta: dict[str, Any] | None = None,
) -> Any:
    try:
        from activegraph.llm import LLMResponse  # type: ignore[import-untyped]
    except ImportError:
        return ProviderResponse(
            raw_text=raw_text,
            parsed=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_seconds=latency_seconds,
            model=model,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )
    return LLMResponse(
        raw_text=raw_text,
        parsed=parsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_seconds=latency_seconds,
        model=model,
        finish_reason=finish_reason,
        provider_meta=provider_meta or {},
        tool_calls=tool_calls,
    )


class RecordedProvider:
    """Deterministic ActiveGraph-compatible provider for tests and replay."""

    default_model = "claude-sonnet-5"
    provider_id = "recorded"
    adapter_id = "recorded"

    def __init__(self, completions: Iterable[RecordedCompletion]) -> None:
        self._remaining = list(completions)
        self._calls: list[ProviderCall] = []

    @property
    def calls(self) -> tuple[ProviderCall, ...]:
        return tuple(self._calls)

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        output_schema: type | None,
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        structured_output_mode: str = "prompt",
    ) -> Any:
        self._calls.append(
            ProviderCall(
                system=system,
                messages=tuple(messages),
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                output_schema=output_schema,
                timeout_seconds=timeout_seconds,
                tools=tuple(tools or ()),
                structured_output_mode=structured_output_mode,
            )
        )
        if not self._remaining:
            raise AgentProviderError("No recorded completion remains")
        item = self._remaining.pop(0)
        parsed = item.parsed
        if output_schema is not None and parsed is not None:
            parsed = cast(type[BaseModel], output_schema).model_validate(parsed)
        raw_text = item.raw_text or json.dumps(
            parsed.model_dump(mode="json") if isinstance(parsed, BaseModel) else parsed,
            sort_keys=True,
        )
        return _response(
            raw_text=raw_text,
            parsed=parsed,
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            cost_usd=item.cost_usd,
            model=model,
            finish_reason=item.finish_reason,
            provider_meta={"recorded": True},
        )

    def estimate_cost(self, *, input_tokens: int, output_tokens: int, model: str) -> Decimal:
        del model
        return (
            Decimal(input_tokens) * Decimal("3") + Decimal(output_tokens) * Decimal("15")
        ) / Decimal("1000000")

    def count_tokens(self, *, system: str, messages: list[Any], model: str) -> int:
        del model
        return max(1, (len(system) + sum(len(str(message)) for message in messages)) // 4)

    def supports_native_structured_output(self, model: str) -> bool:
        del model
        return False

    def recognizes_model(self, name: str) -> bool:
        return name.startswith("claude-")


class LangChainAnthropicProvider:
    """ActiveGraph provider implemented with LangChain's Anthropic adapter.

    Imports are deliberately lazy so deterministic tests and administrative
    commands do not need provider SDKs or credentials.
    """

    default_model = "claude-sonnet-5"
    provider_id = "anthropic"
    adapter_id = "langchain-anthropic"

    def __init__(self, model_factory: Callable[..., Any] | None = None) -> None:
        self._model_factory = model_factory

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        output_schema: type | None,
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        structured_output_mode: str = "prompt",
    ) -> Any:
        del structured_output_mode
        factory = self._model_factory
        try:
            from langchain_core.messages import (
                AIMessage,
                HumanMessage,
                SystemMessage,
                ToolMessage,
            )
        except ImportError as exc:
            raise ProviderDependencyUnavailable(
                "langchain-core==1.5.3 is required for the Anthropic provider"
            ) from exc
        if factory is None:
            try:
                from langchain_anthropic import ChatAnthropic
            except ImportError as exc:
                raise ProviderDependencyUnavailable(
                    "langchain-anthropic==1.5.3 is required for production model calls"
                ) from exc
            factory = ChatAnthropic

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "timeout": timeout_seconds,
        }
        # Sonnet 5 uses provider defaults. Only non-default sampling values
        # are sent, preserving the exact values in the run record either way.
        if temperature != 1.0:
            kwargs["temperature"] = temperature
        if top_p != 1.0:
            kwargs["top_p"] = top_p
        chat = factory(**kwargs)
        if tools:
            chat = chat.bind_tools(tools, parallel_tool_calls=False)

        langchain_messages: list[Any] = [SystemMessage(content=system)]
        for message in messages:
            role = getattr(message, "role", None)
            content = getattr(message, "content", str(message))
            if role == "assistant":
                assistant_tool_calls = [
                    {
                        "id": call.id,
                        "name": call.name,
                        "args": dict(call.args),
                        "type": "tool_call",
                    }
                    for call in (getattr(message, "tool_calls", None) or ())
                ]
                langchain_messages.append(
                    AIMessage(content=content, tool_calls=assistant_tool_calls)
                )
            elif role == "tool":
                langchain_messages.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=getattr(message, "tool_use_id", "unknown"),
                    )
                )
            else:
                langchain_messages.append(HumanMessage(content=content))
        try:
            reply = chat.invoke(langchain_messages)
        except Exception as exc:
            raise AgentProviderError(f"Anthropic completion failed: {type(exc).__name__}") from exc

        raw_text = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        parsed: Any | None = None
        if output_schema is not None and not getattr(reply, "tool_calls", None):
            try:
                from activegraph.llm import parse_structured_response
            except ImportError as exc:
                raise ProviderDependencyUnavailable(
                    "activegraph==1.10.0 is required for structured parsing"
                ) from exc
            parsed = parse_structured_response(raw_text, output_schema)
        usage = getattr(reply, "usage_metadata", None) or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        tool_calls = _activegraph_tool_calls(getattr(reply, "tool_calls", None) or [])
        return _response(
            raw_text=raw_text,
            parsed=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.estimate_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
            ),
            model=model,
            finish_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls or None,
            provider_meta={"adapter": "langchain-anthropic"},
        )

    def estimate_cost(self, *, input_tokens: int, output_tokens: int, model: str) -> Decimal:
        del model
        return (
            Decimal(input_tokens) * Decimal("3") + Decimal(output_tokens) * Decimal("15")
        ) / Decimal("1000000")

    def count_tokens(self, *, system: str, messages: list[Any], model: str) -> int:
        del model
        return max(1, (len(system) + sum(len(str(message)) for message in messages)) // 4)

    def supports_native_structured_output(self, model: str) -> bool:
        del model
        return False

    def recognizes_model(self, name: str) -> bool:
        return name.startswith("claude-")


def _activegraph_tool_calls(calls: list[dict[str, Any]]) -> list[Any]:
    if not calls:
        return []
    try:
        from activegraph.llm import ToolCall
    except ImportError as exc:
        raise ProviderDependencyUnavailable(
            "activegraph==1.10.0 is required for tool-enabled calls"
        ) from exc
    return [
        ToolCall(
            id=str(call.get("id", "")),
            name=str(call.get("name", "")),
            args=dict(call.get("args", {})),
        )
        for call in calls
    ]
