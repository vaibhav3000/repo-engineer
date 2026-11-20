from notify.billing import invoice_ready
from notify.messages import send
from notify.onboarding import welcome


def test_send_defaults_to_email():
    assert send("ada", "hi") == {"to": "ada", "text": "hi", "channel": "email"}


def test_send_takes_a_channel():
    assert send("ada", "hi", channel="push")["channel"] == "push"


def test_onboarding_uses_sms_and_billing_uses_email():
    assert welcome("ada")["channel"] == "sms"
    assert invoice_ready("ada", "$5")["channel"] == "email"
