"""Outgoing messages, recorded rather than sent."""


def send(user: str, text: str) -> dict[str, str]:
    return {"to": user, "text": text}
