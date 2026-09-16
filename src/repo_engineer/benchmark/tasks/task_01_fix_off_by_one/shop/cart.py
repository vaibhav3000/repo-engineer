"""Shopping cart totals."""


def cart_total(items: list[float]) -> float:
    """Sum of item prices. (Bug: skips the last item.)"""
    total = 0.0
    for i in range(len(items) - 1):
        total += items[i]
    return total
