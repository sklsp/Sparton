"""Last uncovered lines of the API layer (quality item 2).

Covers the remaining branches of ``app/api/ecommerce.py`` (the product detail
route and the title search filter) and ``app/api/create.py`` (lazy dataset-
service construction, the real ComfyUI factory against an unreachable server,
and the DatasetError / LLMError mappings on two routes), pushing both modules
to full line coverage.
"""

from __future__ import annotations

import app.api.create as create_api
from app.datasets.service import DatasetError, DatasetService
from app.llm import LLMError


# --------------------------------------------------------------------------
# app/api/ecommerce.py: GET /products/{id} and the search filter
# --------------------------------------------------------------------------
def test_get_product_returns_row(client, auth_headers, seed_products):
    product = seed_products[2]
    response = client.get(f"/products/{product.id}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == product.id
    assert body["sku"] == "SKU-3"
    assert body["title"] == "Product 3"
    assert body["price"] == 30.0
    assert body["category"] == "test"
    assert body["status"] == "active"
    # seed_products pairs each product with an Inventory row of quantity i * 5.
    assert body["inventory"] == 15


def test_get_product_missing_is_404(client, auth_headers):
    response = client.get("/products/999999", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"


def test_get_product_other_org_is_404(client, auth_headers, seed_products):
    """Tenant scoping: another org's product id is indistinguishable from missing."""
    registered = client.post(
        "/auth/register",
        json={
            "email": "user-b@example.com",
            "password": "long-password-2",
            "organization_name": "Org B",
        },
    )
    assert registered.status_code == 201
    headers_b = {"Authorization": f"Bearer {registered.json()['token']}"}

    response = client.get(f"/products/{seed_products[0].id}", headers=headers_b)
    assert response.status_code == 404


def test_list_products_search_filters_by_title(client, auth_headers, seed_products):
    response = client.get("/products?search=Product%203", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert [p["title"] for p in body["products"]] == ["Product 3"]


# --------------------------------------------------------------------------
# app/api/create.py: lazy init, real ComfyUI factory, error mappings
# --------------------------------------------------------------------------
def test_dataset_service_is_lazy_initialized(create_domains_client, create_domain_headers, monkeypatch):
    """The module-level singleton is built on first use (create.py lines 40-42)."""
    # Reset the singleton so this request performs the construction even if an
    # earlier test in the same process already populated it.
    monkeypatch.setattr(create_api, "_dataset_service", None)

    response = create_domains_client.get("/datasets", headers=create_domain_headers)
    assert response.status_code == 200
    assert response.json() == {"count": 0, "datasets": []}
    assert isinstance(create_api._dataset_service, DatasetService)


def test_comfyui_status_reports_unreachable_server(
    create_domains_client, create_domain_headers, monkeypatch
):
    """Without a fake, the route builds a real ComfyUIService (create.py line 46)."""
    # Port 9 (discard): nothing listens there, so ping fails fast and
    # deterministically instead of depending on whether port 8188 is free.
    monkeypatch.setattr("app.core.config.settings.comfyui_base_url", "http://127.0.0.1:9")

    response = create_domains_client.get("/comfyui/status", headers=create_domain_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["base_url"] == "http://127.0.0.1:9"
    assert "error" in body


class _FailingDatasetService:
    """Stands in for DatasetService on the two error-mapping routes."""

    def delete_image(self, db, dataset_id, image_id, organization_id):
        raise DatasetError("Image not found", status_code=404)

    def generate_captions(self, db, dataset_id, organization_id, model=None, overwrite=False):
        raise LLMError("LLM down", status_code=502)


def test_delete_dataset_image_maps_dataset_error(
    create_domains_client, create_domain_headers, monkeypatch
):
    """DELETE /datasets/{id}/images/{image_id} maps DatasetError to its status (338-339)."""
    monkeypatch.setattr(create_api, "get_dataset_service", lambda: _FailingDatasetService())

    response = create_domains_client.delete("/datasets/1/images/2", headers=create_domain_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Image not found"


def test_generate_captions_maps_llm_error(create_domains_client, create_domain_headers, monkeypatch):
    """POST /datasets/{id}/captions maps LLMError to its status (381-382)."""
    monkeypatch.setattr(create_api, "get_dataset_service", lambda: _FailingDatasetService())

    response = create_domains_client.post(
        "/datasets/1/captions", json={"model": "test-model"}, headers=create_domain_headers
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "LLM down"
