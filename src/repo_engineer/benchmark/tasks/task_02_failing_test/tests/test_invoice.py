from billing.invoice import total_with_tax


def test_total_applies_correct_tax():
    # Business tax rate is 25%.
    assert total_with_tax(100.0) == 125.0
