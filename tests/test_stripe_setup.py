"""tools/stripe_setup.py against a fake Stripe client (no network, no key)."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("stripe_setup", ROOT / "tools" / "stripe_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class Page(SimpleNamespace):
    def auto_paging_iter(self):
        return iter(self.data)


class FakeStripe:
    """Just enough of StripeClient.v1 for the setup script, recording every write."""

    def __init__(self):
        self.products, self.prices, self.links, self.writes = [], [], [], []
        self.v1 = SimpleNamespace(
            products=SimpleNamespace(list=self._products_list, create=self._products_create,
                                     update=lambda pid, p: self.writes.append(("product.update", p))),
            prices=SimpleNamespace(list=self._prices_list, create=self._prices_create),
            payment_links=SimpleNamespace(list=self._links_list, create=self._links_create))

    def _products_list(self, p):
        return Page(data=[x for x in self.products if x.id in p["ids"]])

    def _products_create(self, p):
        self.writes.append(("product.create", p))
        self.products.append(SimpleNamespace(id=p["id"], tax_code=p.get("tax_code")))
        return self.products[-1]

    def _prices_list(self, p):
        return Page(data=[x for x in self.prices if x.lookup_key in p["lookup_keys"] and x.active])

    def _prices_create(self, p):
        self.writes.append(("price.create", p))
        if p.get("transfer_lookup_key"):
            for old in self.prices:
                if old.lookup_key == p["lookup_key"]:
                    old.lookup_key = None
        cua = SimpleNamespace(**{k: v for k, v in p["custom_unit_amount"].items() if k != "enabled"})
        self.prices.append(SimpleNamespace(id=f"price_{len(self.prices)}", lookup_key=p["lookup_key"],
                                           currency=p["currency"], custom_unit_amount=cua, active=True))
        return self.prices[-1]

    def _links_list(self, p):
        return Page(data=[x for x in self.links if x.active])

    def _links_create(self, p):
        self.writes.append(("link.create", p))
        self.links.append(SimpleNamespace(url=f"https://buy.stripe.com/test_link{len(self.links)}",
                                          metadata=p["metadata"], active=True))
        return self.links[-1]


def run(client, **kw):
    product = setup.ensure_product(client)
    price = setup.ensure_price(client, product, kw.get("currency", "usd"), kw.get("minimum", 100),
                               kw.get("preset", 500))
    return setup.ensure_link(client, price, "thanks")


def test_creates_a_pay_what_you_want_donation_link():
    s = FakeStripe()
    url = run(s)
    assert url == "https://buy.stripe.com/test_link0"
    price = next(p for k, p in s.writes if k == "price.create")
    assert price["custom_unit_amount"] == {"enabled": True, "minimum": 100, "preset": 500}
    assert "recurring" not in price  # donations, not a subscription
    link = next(p for k, p in s.writes if k == "link.create")
    assert link["submit_type"] == "donate" and link["line_items"] == [{"price": "price_0", "quantity": 1}]
    assert link["metadata"]["app"] == "super8"
    assert link["managed_payments"] == {"enabled": False}  # Stripe rejects donations via Managed Payments


def test_rerun_reuses_everything():
    s = FakeStripe()
    first = run(s)
    n = len(s.writes)
    assert run(s) == first and len(s.writes) == n  # nothing new created


def test_new_amount_makes_a_new_link_and_keeps_the_old_one_active():
    s = FakeStripe()
    old = run(s)
    new = run(s, minimum=200, preset=1000)
    assert new != old
    assert all(link.active for link in s.links)  # installed copies of Super8 keep a working button


def test_write_link_updates_donate_py(tmp_path):
    target = tmp_path / "donate.py"
    target.write_text('X = 1\nSTRIPE_PAYMENT_LINK = ""\nY = 2\n')
    setup.write_link("https://buy.stripe.com/abc12345", target)
    assert 'STRIPE_PAYMENT_LINK = "https://buy.stripe.com/abc12345"' in target.read_text()
    assert target.read_text().startswith("X = 1\n") and target.read_text().endswith("Y = 2\n")


def test_preset_below_minimum_is_rejected():
    with pytest.raises(SystemExit):
        setup.main(["--minimum", "500", "--preset", "100"])
