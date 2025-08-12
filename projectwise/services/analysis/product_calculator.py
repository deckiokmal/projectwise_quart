"""
Product cost calculator tool.

This module defines an asynchronous function that computes the total
and discounted cost for a list of products or services.  It serves
as a simple example of how pricing logic might be encapsulated in
its own service.  The calculation rules can be replaced or
extended to match your actual business requirements.
"""

from __future__ import annotations

from typing import Dict, Any, List


async def product_calculator(
    items: List[Dict[str, Any]], discount_rate: float = 0.0
) -> Dict[str, Any]:
    """Calculate total and discounted cost for a collection of items.

    Args:
        items: A list of dictionaries, each representing an item with
            at least ``quantity`` and ``unit_price`` keys.  Additional
            fields are ignored.
        discount_rate: Optional discount rate to apply (0.0–1.0).

    Returns:
        A dictionary containing the total before discount and after
        discount.  The discounted total is simply ``total * (1 - discount_rate)``.
    """
    total = 0.0
    for item in items:
        qty = float(item.get("quantity", 1))
        unit_price = float(item.get("unit_price", 0))
        total += qty * unit_price
    discounted_total = total * (1 - discount_rate)
    return {
        "total_price": total,
        "discounted_total": discounted_total,
    }