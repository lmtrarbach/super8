"""Donation settings: everything about how people can support Super8, in one place.

Card donations go through a Stripe Payment Link: a checkout page hosted by Stripe. Super8 only opens
that URL in the browser, so no Stripe keys ship inside the app (anything in a distributed app can be
extracted) and no server is needed. Create the link in the Stripe Dashboard (Payment Links, with
"Customers choose what to pay") and paste it below; the "Donate with card" button appears once it is set.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Stripe Payment Link for card donations, e.g. "https://buy.stripe.com/abc123...". Empty: no card button.
STRIPE_PAYMENT_LINK = "https://donate.stripe.com/5kQbJ1agL0eaaMA2vI9R600"

# Only Stripe's own checkout hosts are accepted, so a typo or a lookalike address can't make the
# button send people somewhere else.
STRIPE_HOSTS = ("buy.stripe.com", "donate.stripe.com")

# Same addresses as Candle (Help → Support Candle).
CRYPTO = (
    ("Bitcoin", "bc1qnyceh488yqr0l6ffljygf47243p8wsvct4mkw7"),
    ("Ethereum", "0x8bc6D2D3d47D15EEd881975A09F008609F3bAdF2"),
    ("Solana", "3Gq3t2Xoix8eRvyxCE8mdB5BPsNCwHxDVc8oLXMTeFe1"),
)


def valid_stripe_link(url: str) -> bool:
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    return (u.scheme == "https" and u.hostname in STRIPE_HOSTS and len(u.path.strip("/")) >= 4
            and not u.username and not u.password and u.port is None)


def stripe_link() -> str | None:
    """The card-donation link, or None when it isn't set (or isn't a Stripe checkout URL)."""
    url = STRIPE_PAYMENT_LINK.strip()
    return url if url and valid_stripe_link(url) else None
