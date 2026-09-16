"""Order service."""

from orders.pricing import compute_total


def order_total(unit_price: float, quantity: int) -> float:
    return compute_total(unit_price, quantity)
