from inventory.money import to_cents
from inventory.report import report_total


def test_to_cents_rounds_to_the_nearest_cent():
    assert to_cents(1.005) in (100, 101)
    assert to_cents(12.34) == 1234


def test_report_total():
    assert report_total([(1.50, 2), (9.34, 1)]) == "$12.34"


def test_empty_report():
    assert report_total([]) == "$0.00"
