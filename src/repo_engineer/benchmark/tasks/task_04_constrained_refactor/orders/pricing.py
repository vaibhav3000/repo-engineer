"""Order pricing."""


def compute_total(unit_price: float, quantity: int) -> float:
    return round(unit_price * quantity, 2)
