import pytest

from accounts import store
from accounts.api import user_payload
from accounts.cli import show


def test_fetch_user():
    assert store.fetch_user(1) == "ada"
    with pytest.raises(KeyError):
        store.fetch_user(99)


def test_old_name_is_gone():
    assert not hasattr(store, "get_user")


def test_callers_still_work():
    assert user_payload(2) == {"id": 2, "name": "grace"}
    assert show(1) == "user 1: ada"
