from notify.messages import send


def invoice_ready(user: str, amount: str) -> dict[str, str]:
    return send(user, f"Your invoice for {amount} is ready.")
