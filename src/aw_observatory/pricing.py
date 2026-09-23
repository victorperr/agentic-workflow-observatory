"""Fallback cost estimate when the AWF proxy did not report AI Credits for a call.

1 AIC = $0.01. Prices are USD per million tokens (input, output) and only used as an
estimate; runs where the proxy reports `aic` directly never touch this table.
"""

from __future__ import annotations

from aw_observatory.model import LlmCall

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
    "gpt-4.1": (2.0, 8.0),
    "gemini": (1.25, 10.0),
}
DEFAULT_PRICE = (3.0, 15.0)
CACHE_READ_DISCOUNT = 0.1  # cached input tokens bill at ~10% of the input price


def price_for(model: str) -> tuple[float, float]:
    model = model.lower()
    # Longest prefix wins so "gpt-5-mini" is not priced as "gpt-5".
    for prefix in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if prefix in model:
            return PRICES_PER_MTOK[prefix]
    return DEFAULT_PRICE


def estimate_aic(call: LlmCall) -> float:
    in_price, out_price = price_for(call.model)
    usd = (
        call.input_tokens * in_price
        + call.cache_read_tokens * in_price * CACHE_READ_DISCOUNT
        + call.cache_write_tokens * in_price * 1.25
        + call.output_tokens * out_price
    ) / 1_000_000
    return round(usd * 100, 4)
