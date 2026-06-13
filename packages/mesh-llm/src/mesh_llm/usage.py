from __future__ import annotations

from pydantic import BaseModel, Field


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

    #: The model that actually served the call. Carried on the per-call usage
    #: object (rather than read off the client) so a RoutedLLMClient that
    #: escalates cheap→strong reports the realized tier model, and so concurrent
    #: callers sharing one client never read a clobbered value. Excluded from
    #: serialization: the token-usage dicts that flow into skill outputs are
    #: typed ``dict[str, int]``, and the realized model is carried separately on
    #: each skill output's own ``model`` field.
    model: str = Field(default="", exclude=True)
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
            # A summed total spans calls; keep the model only when both agree.
            model=self.model if self.model == other.model else "",
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens
            + other.cache_creation_tokens,
        )
