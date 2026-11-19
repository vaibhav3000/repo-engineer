"""User records kept in memory."""

USERS = {1: "ada", 2: "grace"}


def get_user(user_id: int) -> str:
    """The name of a user, or a KeyError for an unknown id."""
    return USERS[user_id]
