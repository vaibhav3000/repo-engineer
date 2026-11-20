class Registry:
    def __init__(self, enabled=[]):
        self.enabled = enabled

    def enable(self, name: str) -> None:
        if name not in self.enabled:
            self.enabled.append(name)
