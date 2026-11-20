from app.forms import default_handle
from app.views import page_url, teaser


def test_page_url():
    assert page_url("Hello World!") == "/pages/hello-world"


def test_teaser():
    assert teaser("A very long title indeed") == "A very long..."


def test_default_handle():
    assert default_handle("Ada Lovelace") == "@ada-lovelace"
