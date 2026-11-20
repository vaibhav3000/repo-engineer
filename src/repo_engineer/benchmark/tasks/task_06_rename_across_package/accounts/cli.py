from accounts import store


def show(user_id: int) -> str:
    return f"user {user_id}: {store.get_user(user_id)}"
