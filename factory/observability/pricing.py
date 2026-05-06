"""LLM pricing constants for spend estimation.

IMPORTANT — these are per-call cost ESTIMATES based on Anthropic's published
list prices, NOT actuals from the Anthropic billing API. Actual costs may
differ due to:
  - Volume discounts or negotiated rates
  - Price changes after this file was last updated
  - API-level rounding differences

Last updated: 2026-05-06
Source: https://www.anthropic.com/pricing (claude-sonnet-4-6 row)

To update pricing: bump the relevant ModelPricing fields and update the
date above. Do NOT integrate with the billing API — that would require
credentials and network access in the ingest path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token pricing for a single model.

    All costs are in USD per 1,000,000 tokens (i.e. $/Mtok).

    Fields:
        model: Anthropic model identifier string.
        input_per_mtok: Cost per million input (non-cached) tokens.
        output_per_mtok: Cost per million output tokens.
        cache_read_per_mtok: Cost per million prompt-cache-read tokens.
        cache_write_per_mtok: Cost per million prompt-cache-write tokens.
    """

    model: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


# ── Published Anthropic list prices (USD / 1M tokens) ─────────────────────────
#
# claude-sonnet-4-6 (as of 2026-05-06):
#   Input:        $3.00 / Mtok
#   Output:       $15.00 / Mtok
#   Cache read:   $0.30 / Mtok
#   Cache write:  $3.75 / Mtok
SONNET_4_6 = ModelPricing(
    model="claude-sonnet-4-6",
    input_per_mtok=3.00,
    output_per_mtok=15.00,
    cache_read_per_mtok=0.30,
    cache_write_per_mtok=3.75,
)

# Registry: model string → pricing. Extend as new models are added.
_PRICING_REGISTRY: dict[str, ModelPricing] = {
    SONNET_4_6.model: SONNET_4_6,
}


def get_pricing(model: str) -> ModelPricing | None:
    """Return the ModelPricing for *model*, or None if not registered.

    Callers that need a non-None fallback should use SONNET_4_6 directly
    or add the missing model to _PRICING_REGISTRY.
    """
    return _PRICING_REGISTRY.get(model)


def compute_cost_usd(
    pricing: ModelPricing,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Compute the estimated USD cost for a single LLM call.

    Returns a float (not Decimal) suitable for insertion into a
    NUMERIC(10, 6) column via psycopg2's default float adapter.

    Formula:
        cost = (input_tokens / 1_000_000)  * input_per_mtok
             + (output_tokens / 1_000_000) * output_per_mtok
             + (cache_read_tokens / 1_000_000)  * cache_read_per_mtok
             + (cache_write_tokens / 1_000_000) * cache_write_per_mtok
    """
    mtok = 1_000_000.0
    return (
        (input_tokens / mtok) * pricing.input_per_mtok
        + (output_tokens / mtok) * pricing.output_per_mtok
        + (cache_read_tokens / mtok) * pricing.cache_read_per_mtok
        + (cache_write_tokens / mtok) * pricing.cache_write_per_mtok
    )
