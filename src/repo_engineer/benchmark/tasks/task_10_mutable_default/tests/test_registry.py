from plugins.loader import load
from plugins.registry import Registry


def test_registries_do_not_share_plugins():
    first = load(["audio"])
    second = load(["video"])
    assert first.enabled == ["audio"]
    assert second.enabled == ["video"]


def test_an_explicit_list_is_used():
    names = ["a"]
    assert Registry(names).enabled == ["a"]
