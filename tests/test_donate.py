"""Donation settings: only Stripe checkout links are accepted; the card button appears only with one."""
import pytest

from super8 import donate


@pytest.mark.parametrize("url,ok", [
    ("https://buy.stripe.com/test_5kAbcd1234", True),
    ("https://donate.stripe.com/aBcD1234", True),
    ("  https://buy.stripe.com/abcd1234  ", True),
    ("http://buy.stripe.com/abcd1234", False),           # not https
    ("https://buy.stripe.com.evil.example/abcd", False),  # lookalike host
    ("https://evil.example/buy.stripe.com/abcd", False),
    ("https://user@buy.stripe.com/abcd1234", False),
    ("https://buy.stripe.com:8443/abcd1234", False),
    ("https://buy.stripe.com/", False),                   # no link id
    ("", False),
])
def test_only_stripe_checkout_links(url, ok):
    assert donate.valid_stripe_link(url) is ok


def test_card_button_only_when_link_set(qapp, monkeypatch):
    from PyQt6.QtGui import QDesktopServices
    from super8.ui.about import AboutDialog
    monkeypatch.setattr(donate, "STRIPE_PAYMENT_LINK", "")
    assert AboutDialog().card_button is None
    monkeypatch.setattr(donate, "STRIPE_PAYMENT_LINK", "https://evil.example/pay")
    assert AboutDialog().card_button is None  # invalid links never get a button
    monkeypatch.setattr(donate, "STRIPE_PAYMENT_LINK", "https://buy.stripe.com/test_abc12345")
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toString())))
    d = AboutDialog(focus_support=True)
    assert d.card_button is not None
    d.card_button.click()
    assert opened == ["https://buy.stripe.com/test_abc12345"]


def test_crypto_addresses_unchanged():
    assert [c for c, _ in donate.CRYPTO] == ["Bitcoin", "Ethereum", "Solana"]
    assert donate.CRYPTO[0][1] == "bc1qnyceh488yqr0l6ffljygf47243p8wsvct4mkw7"
