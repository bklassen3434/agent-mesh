from __future__ import annotations

from pydantic import BaseModel


class LLMUsage(BaseModel):
    """Token usage for a single LLM call, provider-agnostic.

    The four token kinds map onto Anthropic's usage object:
    ``input_tokens`` (uncached prompt + completion request), ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``. For providers
    without prompt caching (Ollama) the two cache fields stay zero.

    Note: Anthropic reports ``input_tokens`` *excluding* cached tokens — a cache
    read shows up in ``cache_read_tokens``, not ``input_tokens``. Cost
    computation in :mod:`mesh_llm.pricing` accounts for that.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens
            + other.cache_creation_tokens,
        )
