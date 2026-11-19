"""Inventory reports."""

from inventory.money import format_cents, to_cents


def line_total(price: float, quantity: int) -> int:
    return to_cents(price) * quantity


def report_total(lines: list[tuple[float, int]]) -> str:
    return format_cents(sum(line_total(price, quantity) for price, quantity in lines))
