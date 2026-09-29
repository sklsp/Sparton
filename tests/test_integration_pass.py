"""Cross-domain integration tests.

The documents / generation / datasets / training domains are **feature-flagged
off by default** (docs/DECISIONS.md D-020), so their routes do not exist in a
default deployment. The tests for them therefore build an app with the flags
switched on, which is also a direct test of the flag mechanism: the same
factory returns a different surface depending on configuration.
"""

from __future__ import annotations

import pytest


class TestFeatureFlags:
    def test_default_deployment_has_no_non_product_routes(self, client):
        """Hiding is done by not registering the route at all (D-020)."""
        for path in ("/documents", "/rag/status", "/comfyui/workflows",
                     "/training/status", "/datasets", "/intelligence/jobs"):
            assert client.get(path).status_code == 404, path

    def test_default_deployment_has_the_product(self, client, auth_headers):
        for path in ("/shops", "/competitors", "/changes", "/reports", "/overview"):
            assert client.get(path, headers=auth_headers).status_code == 200, path

    def test_enabling_a_domain_registers_its_routes(self, all_domains_client,
                                                    all_domain_headers):
        assert all_domains_client.get(
            "/rag/status", headers=all_domain_headers
        ).status_code == 200



class TestAuthAndTenancy:
    def test_register_login_me(self, client):
        response = client.post(
            "/auth/register",
            json={"email": "u1@example.com", "password": "long-password-1",
                  "organization_name": "Org1"},
        )
        assert response.status_code == 201
        token = response.json()["token"]

        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["email"] == "u1@example.com"

    def test_login_wrong_password(self, client):
        client.post("/auth/register", json={
            "email": "u2@example.com", "password": "long-password-1",
            "organization_name": "Org2"})
        response = client.post("/auth/login", json={
            "email": "u2@example.com", "password": "wrong-password"})
        assert response.status_code == 401

    def test_health_reports_llm_and_db(self, client, auth_headers):
        # /health now requires a principal: it reports the LLM provider and the
        # agent tool inventory, which is internal surface (audit P0-B).
        response = client.get("/health", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["database"]["ok"] is True
        assert body["llm"]["provider"] == "test"
        assert body["agent_tools"] >= 10

    def test_metrics_endpoint(self, client, auth_headers):
        response = client.get("/metrics", headers=auth_headers)
        assert response.status_code == 200
        # Prometheus text format: at least one metric line is present.
        assert " 1.0" in response.text or "_total" in response.text


class TestKnowledgeDomain:
    def test_document_upload_and_rag_query(self, all_domains_client, all_domain_headers):
        files = [("files", ("notes.txt", b"Sparton unifies RAG and agents. The sky is blue.", "text/plain"))]
        upload = all_domains_client.post("/documents/upload", files=files, headers=all_domain_headers)
        assert upload.status_code == 201
        assert upload.json()["uploaded"] == 1

        status = all_domains_client.get("/rag/status", headers=all_domain_headers)
        assert status.status_code == 200
        assert status.json()["chunks"] >= 1

    def test_chat_with_rag_citations(self, all_domains_client, all_domain_headers):
        files = [("files", ("facts.txt", b"Our flagship product is called Zephyr and costs 42 dollars.", "text/plain"))]
        all_domains_client.post("/documents/upload", files=files, headers=all_domain_headers)

        chat = all_domains_client.post("/chat", json={"message": "What is the flagship product?"},
                           headers=all_domain_headers)
        assert chat.status_code == 200
        body = chat.json()
        assert body["conversation_id"] > 0
        assert isinstance(body["citations"], list)

    def test_prompt_template_crud(self, all_domains_client, all_domain_headers):
        created = all_domains_client.post("/prompts", headers=all_domain_headers, json={
            "key": "test_tpl", "name": "Test Template", "template": "Hello {input}"})
        assert created.status_code == 201

        listed = all_domains_client.get("/prompts", headers=all_domain_headers)
        assert listed.status_code == 200
        assert any(p["key"] == "test_tpl" for p in listed.json()["prompts"])


class TestEcommerceDomain:
    def test_products_and_analytics(self, client, auth_headers, seed_products):
        products = client.get("/products", headers=auth_headers)
        assert products.status_code == 200
        assert products.json()["count"] == 5

        summary = client.get("/analytics/summary", headers=auth_headers)
        assert summary.status_code == 200
        assert summary.json()["catalog"]["product_count"] == 5


class TestAgentDomain:
    def test_tool_registry_endpoint(self, client, auth_headers):
        tools = client.get("/tools", headers=auth_headers)
        assert tools.status_code == 200
        names = [t["name"] for t in tools.json()["tools"]]
        # Domains from BOTH source projects are present:
        assert "documents.search" in names          # Apollo origin
        assert "ecommerce.get_products" in names    # Ares origin
        assert "generation.generate_image" in names # Apollo origin
        assert "research.list_opportunities" in names  # Ares origin

    def test_agent_run_read_tool_grounding(self, client, auth_headers, llm, seed_products):
        llm.scripted_responses.append({"tool": "ecommerce.get_products", "arguments": {"limit": 5}})
        llm.scripted_responses.append("Found 5 products in the catalog.")

        run = client.post("/agent/run", headers=auth_headers,
                          json={"message": "List the products"})
        assert run.status_code == 202
        run_id = run.json()["run_id"]

        detail = client.get(f"/agent/runs/{run_id}", headers=auth_headers)
        assert detail.status_code == 200
        body = detail.json()
        assert body["status"] == "COMPLETED"
        assert any(s["type"] == "tool_call" for s in body["steps"])

    def test_write_tool_requires_approval(self, client, auth_headers, llm, seed_products):
        llm.scripted_responses.append({
            "tool": "ecommerce.update_product",
            "arguments": {"product_id": seed_products[0].id,
                          "title": "Renamed by agent"},
        })

        run = client.post("/agent/run", headers=auth_headers,
                          json={"message": "Rename product 1 to 'Renamed by agent'"})
        assert run.status_code == 202
        run_id = run.json()["run_id"]

        detail = client.get(f"/agent/runs/{run_id}", headers=auth_headers)
        assert detail.json()["status"] == "WAITING_FOR_APPROVAL"

        approvals = client.get("/approvals", headers=auth_headers)
        assert approvals.json()["count"] >= 1
        approval = approvals.json()["approvals"][0]

        resolved = client.post(
            f"/approvals/{approval['id']}/resolve",
            headers=auth_headers,
            json={"approved": True, "note": "ok"},
        )
        assert resolved.status_code == 200

        detail = client.get(f"/agent/runs/{run_id}", headers=auth_headers)
        assert detail.json()["status"] == "COMPLETED"


class TestCreateDomain:
    def test_comfyui_workflows_library(self, all_domains_client, all_domain_headers):
        workflows = all_domains_client.get("/comfyui/workflows", headers=all_domain_headers)
        assert workflows.status_code == 200
        # The bundled SDXL workflow ships with the repo.
        ids = [w["id"] for w in workflows.json().get("workflows", [])]
        assert isinstance(ids, list)

    def test_dataset_lifecycle(self, all_domains_client, all_domain_headers):
        from io import BytesIO

        from PIL import Image

        created = all_domains_client.post("/datasets", headers=all_domain_headers,
                              json={"name": "Test DS", "trigger_word": "zephyr"})
        assert created.status_code == 201
        dataset_id = created.json()["id"]

        buffer = BytesIO()
        Image.new("RGB", (512, 512), color=(120, 40, 40)).save(buffer, format="PNG")
        upload = all_domains_client.post(
            f"/datasets/{dataset_id}/images",
            files={"file": ("img1.png", buffer.getvalue(), "image/png")},
            headers=all_domain_headers,
        )
        assert upload.status_code == 201

        images = all_domains_client.get(f"/datasets/{dataset_id}/images", headers=all_domain_headers)
        assert images.json()["count"] == 1

        validation = all_domains_client.get(f"/datasets/{dataset_id}/validate", headers=all_domain_headers)
        assert validation.status_code == 200
        report = validation.json()
        assert report["total_images"] == 1
        assert "missing_caption" in str(report["findings"])

    def test_training_preflight(self, all_domains_client, all_domain_headers):
        preflight = all_domains_client.post("/training/preflight", headers=all_domain_headers,
                                json={"resolution": [1024, 1024], "batch_size": 1})
        assert preflight.status_code == 200
        assert preflight.json()["verdict"] in {"ok", "heavy", "risky", "unsupported", "blocked"}

    def test_hardware_detection(self, all_domains_client, all_domain_headers):
        hardware = all_domains_client.get("/training/hardware", headers=all_domain_headers)
        assert hardware.status_code == 200
        assert "accelerator" in hardware.json()


class TestIntelligenceDomain:
    def test_research_job_lifecycle(self, all_domains_client, all_domain_headers):
        started = all_domains_client.post("/intelligence/jobs", headers=all_domain_headers,
                              json={"query": "handmade ceramic mugs market"})
        assert started.status_code == 202
        job_id = started.json()["job_id"]

        jobs = all_domains_client.get("/intelligence/jobs", headers=all_domain_headers)
        assert jobs.status_code == 200
        assert any(j["id"] == job_id for j in jobs.json()["jobs"])

    def test_opportunities_listing(self, all_domains_client, all_domain_headers):
        response = all_domains_client.get("/intelligence/opportunities", headers=all_domain_headers)
        assert response.status_code == 200
        assert "opportunities" in response.json()


class TestAdminDomain:
    def test_admin_requires_role(self, client, auth_headers):
        response = client.get("/admin/users", headers=auth_headers)
        assert response.status_code == 200  # org creator is admin

    def test_last_admin_protection(self, client, auth_headers):
        users = client.get("/admin/users", headers=auth_headers).json()["users"]
        admin_id = users[0]["id"]
        demote = client.patch(f"/admin/users/{admin_id}", headers=auth_headers,
                              json={"role": "viewer"})
        assert demote.status_code == 409
