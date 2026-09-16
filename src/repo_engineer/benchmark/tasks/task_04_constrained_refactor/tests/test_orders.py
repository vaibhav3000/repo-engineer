from orders.pricing import calculate_total
from orders.service import order_total


def test_calculate_total():
    assert calculate_total(3.33, 3) == 9.99


def test_service_uses_pricing():
    assert order_total(2.0, 2) == 4.0
