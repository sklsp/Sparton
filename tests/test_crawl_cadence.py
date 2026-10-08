"""The crawl cadence a plan sells is the cadence its shops get.

A shop stored its cadence when it was added, and nothing changed it after: a
shop added on Free stayed weekly after an upgrade to Pro, a cancelled Pro kept
its daily checks, and the API let a Free account ask for checks every 6 hours.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.database.ecommerce_models import Shop
from app.core.database.models import utcnow

SHOP_URL = "https://acme-homeware.myshopify.com"


@pytest.fixture()
def prices(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_123", raising=False)
    monkeypatch.setattr(settings, "stripe_price_business", "price_business_123", raising=False)


@pytest.fixture()
def shop(client, auth_headers):
    response = client.post("/shops", headers=auth_headers, json={"url": SHOP_URL, "name": "Acme"})
    assert response.status_code == 201, response.text
    return response.json()


def subscription(event_id: str, org_id: int, price_id: str, *, kind: str = "updated") -> dict:
    return {
        "id": event_id,
        "type": f"customer.subscription.{kind}",
        "data": {"object": {
            "id": "sub_1", "customer": "cus_1", "status": "active",
            "metadata": {"organization_id": str(org_id)},
            "items": {"data": [{"price": {"id": price_id}}]},
        }},
    }


def test_a_new_shop_follows_its_plan_and_cannot_go_faster(client, auth_headers, shop):
    assert shop["crawl_frequency_hours"] == 168  # Free: weekly
    response = client.patch(f"/shops/{shop['id']}", headers=auth_headers,
                            json={"crawl_frequency_hours": 6})
    assert response.status_code == 200
    assert response.json()["crawl_frequency_hours"] == 168, "Free must not get 6-hourly checks"


def test_an_upgrade_speeds_up_the_shops_that_exist(client, auth_headers, shop, db_session, prices):
    from app.billing.service import handle_event

    row = db_session.get(Shop, shop["id"])
    crawled = utcnow() - timedelta(days=2)
    row.last_crawled_at = crawled
    row.next_crawl_at = crawled + timedelta(days=7)
    db_session.commit()

    handle_event(db_session, subscription("evt_up", row.organization_id, "price_pro_123"))
    db_session.refresh(row)
    assert row.crawl_frequency_hours == 24
    # Due now: the last check was two days ago, and Pro checks daily.
    assert row.next_crawl_at.replace(tzinfo=None) == (crawled + timedelta(hours=24)).replace(tzinfo=None)


def test_a_cancellation_slows_them_down_again(client, auth_headers, shop, db_session, prices):
    from app.billing.service import handle_event

    org = db_session.get(Shop, shop["id"]).organization_id
    handle_event(db_session, subscription("evt_a", org, "price_business_123"))
    assert db_session.get(Shop, shop["id"]).crawl_frequency_hours == 12
    handle_event(db_session, subscription("evt_b", org, "price_business_123", kind="deleted"))
    db_session.expire_all()
    assert db_session.get(Shop, shop["id"]).crawl_frequency_hours == 168


def test_a_renewal_keeps_a_slower_cadence_the_customer_chose(client, auth_headers, shop,
                                                            db_session, prices):
    from app.billing.service import handle_event

    org = db_session.get(Shop, shop["id"]).organization_id
    handle_event(db_session, subscription("evt_c", org, "price_pro_123"))
    response = client.patch(f"/shops/{shop['id']}", headers=auth_headers,
                            json={"crawl_frequency_hours": 72})
    assert response.json()["crawl_frequency_hours"] == 72
    # Stripe sends an update on every renewal; the plan did not change.
    handle_event(db_session, subscription("evt_d", org, "price_pro_123"))
    db_session.expire_all()
    assert db_session.get(Shop, shop["id"]).crawl_frequency_hours == 72
