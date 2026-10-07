"""Anthropic Claude executor used by the v0.4 Notion agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionResult:
    text: str
    response_id: str = ""
    total_tokens: int | None = None
    estimated_cost: float | None = None


class AnthropicAgentExecutor:
    """Execute one agent prompt with Claude and a bounded request timeout."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 90.0,
        default_model: str = "claude-sonnet-5-5",
        max_tokens: int = 2000,
    ) -> None:
        from anthropic import Anthropic

        self.client = Anthropic(api_key=api_key, timeout=timeout_seconds)
        self.default_model = default_model
        self.max_tokens = max(256, max_tokens)

    def _resolve_model(self, registry_model: str) -> str:
        model = registry_model.strip()
        return model if model.lower().startswith("claude-") else self.default_model

    @staticmethod
    def _usage_total(response: Any) -> int | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        if input_tokens is None and output_tokens is None:
            return None
        return int(input_tokens or 0) + int(output_tokens or 0)

    def execute(self, *, model: str, instructions: str, prompt: str) -> ExecutionResult:
        response = self.client.messages.create(
            model=self._resolve_model(model),
            system=instructions,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
        )
        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            raise ValueError("Claude returned an empty agent result")
        return ExecutionResult(
            text=text,
            response_id=getattr(response, "id", "") or "",
            total_tokens=self._usage_total(response),
        )
