"""Set up Stripe card donations for Super8: a product, a pay-what-you-want price and a Payment Link.

    .venv/bin/pip install stripe
    .venv/bin/python tools/stripe_setup.py                  # prompts for the key, prints the link
    .venv/bin/python tools/stripe_setup.py --write          # ...and puts it in super8/donate.py
    .venv/bin/python tools/stripe_setup.py --minimum 200 --preset 1000 --currency eur

Donations only, no subscriptions: one-time payments of an amount the donor chooses, so there is no
server, webhook or customer portal (Candle Premium's setup needs those; this doesn't). Super8 just
opens the link.

The key is read from STRIPE_API_KEY or asked for without echo; it is never stored. A restricted key
with write access to Products, Prices and Payment Links is enough. Test or live mode follows the key:
set up and try it in test mode first, then run again with the live key.

Safe to re-run: the product has a fixed id, the price a lookup key, and the link is found by its
metadata, so nothing is duplicated. Changing --minimum/--preset/--currency makes a new price and a new
link, and the old link stays active on purpose: every copy of Super8 already installed has the old URL
built in, and switching it off would break their donate button.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

PRODUCT_ID = "super8_donation"
APP_TAG = "super8"
DONATE_PY = Path(__file__).resolve().parent.parent / "super8" / "donate.py"


def ensure_product(client, tax_code: str | None = None) -> str:
    found = client.v1.products.list({"ids": [PRODUCT_ID]}).data
    if found:
        if tax_code and getattr(found[0], "tax_code", None) != tax_code:
            client.v1.products.update(PRODUCT_ID, {"tax_code": tax_code})
        return found[0].id
    params = {"id": PRODUCT_ID, "name": "Donation to Super8",
              "description": "Support Super8, the free and open-source video editor for Linux."}
    if tax_code:
        params["tax_code"] = tax_code
    return client.v1.products.create(params).id


def ensure_price(client, product: str, currency: str, minimum: int, preset: int) -> str:
    """A pay-what-you-want price: the donor types the amount (at least `minimum`, `preset` suggested)."""
    key = f"super8_donation_{currency}"
    found = client.v1.prices.list({"lookup_keys": [key], "active": True}).data
    if found:
        cua = found[0].custom_unit_amount
        if (cua and cua.minimum == minimum and cua.preset == preset and found[0].currency == currency):
            return found[0].id
        print(f"  new price (minimum {minimum}, suggested {preset}); the old one stays for the old link")
    return client.v1.prices.create({
        "product": product, "currency": currency, "lookup_key": key, "transfer_lookup_key": True,
        "custom_unit_amount": {"enabled": True, "minimum": minimum, "preset": preset},
        "nickname": "Super8 donation (customer chooses)",
    }).id


def ensure_link(client, price: str, message: str) -> str:
    """The Payment Link for `price`; reused when one already exists. Old links are never deactivated."""
    for link in client.v1.payment_links.list({"active": True, "limit": 100}).auto_paging_iter():
        meta = link.metadata or {}
        if meta.get("app") == APP_TAG and meta.get("price") == price:
            return link.url
    return client.v1.payment_links.create({
        "line_items": [{"price": price, "quantity": 1}],
        "submit_type": "donate",  # the checkout button says "Donate"
        # Stripe doesn't allow donations through Managed Payments (Stripe as the seller), which may be on
        # by default for the account (it is for Candle Premium). Turn it off for this link only.
        "managed_payments": {"enabled": False},
        "after_completion": {"type": "hosted_confirmation", "hosted_confirmation": {"custom_message": message}},
        "metadata": {"app": APP_TAG, "price": price},
    }).url


def write_link(url: str, path: Path = DONATE_PY) -> None:
    """Put the link into super8/donate.py (STRIPE_PAYMENT_LINK = "...")."""
    text = path.read_text()
    new, n = re.subn(r'^STRIPE_PAYMENT_LINK = ".*"$', f'STRIPE_PAYMENT_LINK = "{url}"', text, count=1,
                     flags=re.M)
    if n != 1:
        raise SystemExit(f"STRIPE_PAYMENT_LINK not found in {path}")
    path.write_text(new)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--currency", default="usd")
    parser.add_argument("--minimum", type=int, default=100, help="smallest donation in cents (default 100 = 1.00)")
    parser.add_argument("--preset", type=int, default=500, help="suggested amount in cents (default 500 = 5.00)")
    parser.add_argument("--tax-code", help="Stripe tax code for the product, if your account requires one")
    parser.add_argument("--message", default="Thank you for supporting Super8! ♥ Your donation keeps it free "
                                             "and open source.")
    parser.add_argument("--write", action="store_true", help="save the link in super8/donate.py")
    args = parser.parse_args(argv)
    if args.preset < args.minimum:
        parser.error("--preset must be at least --minimum")

    import stripe  # only this script needs it, not the app

    key = os.environ.get("STRIPE_API_KEY") or getpass.getpass("Stripe secret or restricted key (not shown): ")
    if not key.strip():
        parser.error("no key given")
    client = stripe.StripeClient(key.strip())
    try:
        live = client.v1.balance.retrieve().livemode
    except stripe.AuthenticationError:
        raise SystemExit("Stripe rejected that key; copy it again from Dashboard > Developers > API keys")
    except stripe.PermissionError:  # a restricted key without balance access: infer the mode from the key
        live = "_live_" in key
    print(f"Stripe ({'LIVE' if live else 'test'} mode)")

    product = ensure_product(client, args.tax_code)
    price = ensure_price(client, product, args.currency.lower(), args.minimum, args.preset)
    url = ensure_link(client, price, args.message)
    print(f"\nDonation link: {url}")
    if not live:
        print("Test mode: try it with card 4242 4242 4242 4242, any future date and CVC.")
    if args.write:
        write_link(url)
        print(f"Saved in {DONATE_PY.relative_to(DONATE_PY.parent.parent)}")
    else:
        print("Run again with --write to save it in super8/donate.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
