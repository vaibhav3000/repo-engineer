from notify import messages


def welcome(user: str) -> dict[str, str]:
    return messages.send(user, f"Welcome aboard, {user}!")
