from accounts.store import get_user


def user_payload(user_id: int) -> dict[str, object]:
    return {"id": user_id, "name": get_user(user_id)}
