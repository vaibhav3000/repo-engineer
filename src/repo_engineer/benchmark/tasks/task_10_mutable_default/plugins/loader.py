from plugins.registry import Registry


def load(names: list[str]) -> Registry:
    registry = Registry()
    for name in names:
        registry.enable(name)
    return registry
