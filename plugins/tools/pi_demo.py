"""Deterministic, synthetic order tools for Pi demonstrations; no business systems or writes."""

from __future__ import annotations

import hashlib
from typing import Literal

from framework.tool.function_tool import function_tool


def _token(order_id: str, purpose: str) -> str:
    if not order_id.startswith("PI-DEMO-") or len(order_id) > 100:
        raise ValueError("Only synthetic PI-DEMO orders are supported")
    return hashlib.sha256(f"pi-demo-v1:{order_id}:{purpose}".encode()).hexdigest()[:24]


@function_tool(name_override="pi-demo-load-order")
async def load_order(order_id: str) -> dict:
    """Read a synthetic order. Pass its source_token to the quote tool; do not invent tokens."""
    return {
        "order_id": order_id,
        "currency": "CNY",
        "items": [
            {"sku": "DEMO-BOOK", "quantity": 2, "unit_price_cents": 1250},
            {"sku": "DEMO-CABLE", "quantity": 1, "unit_price_cents": 990},
        ],
        "source_token": _token(order_id, "source"),
    }


@function_tool(name_override="pi-demo-quote-order")
async def quote_order(order_id: str, source_token: str, delivery_method: Literal["standard", "express"]) -> dict:
    """Quote an order using source_token from load-order. Express is unavailable; retry standard."""
    if source_token != _token(order_id, "source"):
        return {"_tool_error": True, "error": "INVALID_SOURCE_TOKEN", "next_action": "Read the order first."}
    if delivery_method == "express":
        return {"_tool_error": True, "error": "EXPRESS_UNAVAILABLE", "retry_with": "standard"}
    return {
        "order_id": order_id,
        "delivery_method": "standard",
        "currency": "CNY",
        "subtotal_cents": 3490,
        "shipping_cents": 490,
        "total_cents": 3980,
        "quote_token": _token(order_id, "standard-3980"),
    }


@function_tool(name_override="pi-demo-verify-quote")
async def verify_quote(order_id: str, quote_token: str) -> dict:
    """Verify quote_token returned by quote-order. Only this result proves the quote was verified."""
    if quote_token != _token(order_id, "standard-3980"):
        return {"_tool_error": True, "error": "INVALID_QUOTE_TOKEN", "next_action": "Obtain a valid quote first."}
    return {
        "order_id": order_id,
        "verified": True,
        "delivery_method": "standard",
        "currency": "CNY",
        "total_cents": 3980,
        "quote_token": quote_token,
    }
