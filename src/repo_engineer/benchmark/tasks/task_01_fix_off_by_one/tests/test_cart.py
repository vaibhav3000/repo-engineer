from shop.cart import cart_total


def test_sum_single_item():
    assert cart_total([19.99]) == 19.99


def test_sum_many_items():
    assert cart_total([1.0, 2.0, 3.0]) == 6.0


def test_empty_cart():
    assert cart_total([]) == 0.0
