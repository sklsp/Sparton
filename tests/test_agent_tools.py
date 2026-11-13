"""Tests for app.agent.tools: Athena tool functions run on a real ToolContext."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.agent.registry import ToolAccess, ToolContext, ToolRegistry
from app.agent.tools import (
    GenerateImageInput,
    GetInventoryInput,
    GetProductInput,
    GetProductsInput,
    GetSalesSummaryInput,
    ListDatasetsInput,
    ListDocumentsInput,
    ListGeneratedImagesInput,
    ListOpportunitiesInput,
    SearchDocumentsInput,
    StartTrainingInput,
    UpdateProductInput,
    generate_image,
    get_inventory,
    get_product,
    get_products,
    get_sales_summary,
    list_datasets,
    list_documents,
    list_generated_images,
    list_opportunities,
    register_all,
    search_documents,
    start_training,
    update_product,
)
from app.core.database.creation_models import (
    DatasetProject,
    Document,
    GeneratedImage,
    TrainingProject,
)
from app.core.database.domain_models import Inventory, Opportunity, Order, Product
from app.core.database.models import utcnow
from app.llm import DeterministicProvider


def _ctx(db_session, org=None, services=None):
    """A ToolContext the way the engine builds one: db + llm + tenant scope."""
    return ToolContext(
        db=db_session, llm=DeterministicProvider(), organization_id=org, services=services or {}
    )


def _product(db, org, sku, title, price=10.0, category="test", description=""):
    row = Product(organization_id=org, sku=sku, title=title, price=price, category=category, description=description)
    db.add(row)
    return row


class _FakeRagService:
    """Stands in for the experimental RAGService; records what the tool asked."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.calls = []

    def query(self, text, top_k=4):
        self.calls.append((text, top_k))
        return self._chunks[:top_k]


def _chunk(filename, index, score, text):
    return SimpleNamespace(filename=filename, chunk_index=index, score=score, text=text)


class _FakeComfyuiService:
    """Stands in for the experimental ComfyUIService; records what it was told."""

    def __init__(self, saved="out.png"):
        self._saved = saved
        self.calls = []

    def run_generation_sync(self, workflow_id, payload):
        self.calls.append((workflow_id, payload))
        return self._saved


class TestSearchDocuments:
    def test_it_forwards_the_query_to_the_rag_service_and_truncates_excerpts(self, db_session):
        rag = _FakeRagService(
            [
                _chunk("guide.pdf", 0, 0.123456, "v" * 500),
                _chunk("faq.md", 3, 0.987654, "short answer"),
            ]
        )
        ctx = _ctx(db_session, org=1, services={"rag": rag})

        result = search_documents(ctx, SearchDocumentsInput(query="vanilla", top_k=2))

        assert result["chunks"] == 2
        assert [r["source"] for r in result["results"]] == ["guide.pdf", "faq.md"]
        assert result["results"][0]["chunk"] == 0
        assert result["results"][0]["score"] == 0.123  # rounded to three decimals
        assert result["results"][1]["score"] == 0.988
        assert len(result["results"][0]["text"]) == 400  # truncated for the LLM
        assert rag.calls == [("vanilla", 2)]  # query and top_k forwarded to the service


class TestListDocuments:
    def test_it_lists_newest_first_within_the_callers_tenant(self, db_session):
        old = Document(
            organization_id=1, title="Old", content_hash="h1", chunk_count=4, created_at=utcnow() - timedelta(days=3)
        )
        mid = Document(
            organization_id=1, title="Mid", content_hash="h2", chunk_count=7, created_at=utcnow() - timedelta(days=2)
        )
        new = Document(
            organization_id=1, title="New", content_hash="h3", chunk_count=2, created_at=utcnow() - timedelta(days=1)
        )
        foreign = Document(organization_id=2, title="Foreign", content_hash="h4", chunk_count=9, created_at=utcnow())
        db_session.add_all([old, mid, new, foreign])
        db_session.commit()

        result = list_documents(_ctx(db_session, org=1), ListDocumentsInput(limit=2))

        assert result["count"] == 2
        assert [(d["id"], d["title"], d["chunks"]) for d in result["documents"]] == [
            (new.id, "New", 2),
            (mid.id, "Mid", 7),
        ]


class TestGetProducts:
    def test_search_filters_by_title_and_reports_inventory_levels(self, db_session):
        a = _product(db_session, 1, "SKU-A", "Vanilla Candle")
        b = _product(db_session, 1, "SKU-B", "Cocoa Candle")
        _product(db_session, 1, "SKU-C", "Mug")
        db_session.flush()
        db_session.add_all([Inventory(product_id=a.id, quantity=3), Inventory(product_id=b.id, quantity=8)])
        db_session.commit()

        result = get_products(_ctx(db_session, org=1), GetProductsInput(search="candle"))

        assert result["count"] == 2
        assert [p["sku"] for p in result["products"]] == ["SKU-A", "SKU-B"]
        assert result["products"][0]["inventory"] == 3  # from the inventory row
        assert result["products"][1]["title"] == "Cocoa Candle"

    def test_limit_is_respected_and_other_tenants_are_invisible(self, db_session):
        mine = [_product(db_session, 1, f"M-{i}", f"Mine {i}") for i in range(4)]
        _product(db_session, 2, "F-1", "Foreign")
        db_session.commit()

        result = get_products(_ctx(db_session, org=1), GetProductsInput(limit=3))

        assert [p["id"] for p in result["products"]] == [m.id for m in mine[:3]]
        assert all(p["sku"].startswith("M-") for p in result["products"])

    def test_without_an_organization_every_product_is_visible(self, db_session):
        _product(db_session, 1, "A-1", "Alpha")
        _product(db_session, 2, "B-1", "Beta")
        db_session.commit()

        result = get_products(_ctx(db_session), GetProductsInput())

        assert result["count"] == 2


class TestGetProduct:
    def test_it_returns_the_full_detail_of_one_product(self, db_session):
        p = _product(
            db_session, 1, "SKU-9", "Vanilla Candle", price=24.95, category="candles", description="smells of vanilla"
        )
        db_session.flush()
        db_session.add(Inventory(product_id=p.id, quantity=7))
        db_session.commit()

        result = get_product(_ctx(db_session, org=1), GetProductInput(product_id=p.id))

        assert result == {
            "id": p.id,
            "sku": "SKU-9",
            "title": "Vanilla Candle",
            "description": "smells of vanilla",
            "price": 24.95,
            "category": "candles",
            "status": "active",
            "inventory": 7,
        }

    def test_missing_or_foreign_product_comes_back_as_an_error(self, db_session):
        foreign = _product(db_session, 2, "F-1", "Foreign")
        db_session.commit()

        ctx = _ctx(db_session, org=1)
        assert get_product(ctx, GetProductInput(product_id=999)) == {"error": "Product 999 not found"}
        assert get_product(ctx, GetProductInput(product_id=foreign.id)) == {
            "error": f"Product {foreign.id} not found"
        }


class TestGetInventory:
    def test_it_orders_by_quantity_and_hides_other_tenants(self, db_session):
        low_a = _product(db_session, 1, "L-A", "Low A")
        low_b = _product(db_session, 1, "L-B", "Low B")
        ok = _product(db_session, 1, "OK", "Plenty")
        foreign_low = _product(db_session, 2, "F-L", "Foreign Low")
        db_session.flush()
        db_session.add_all(
            [
                Inventory(product_id=low_a.id, quantity=3, reorder_point=10),
                Inventory(product_id=low_b.id, quantity=8, reorder_point=10),
                Inventory(product_id=ok.id, quantity=40, reorder_point=5),
                Inventory(product_id=foreign_low.id, quantity=1, reorder_point=1),
            ]
        )
        db_session.commit()

        result = get_inventory(_ctx(db_session, org=1), GetInventoryInput())

        assert [i["sku"] for i in result["items"]] == ["L-A", "L-B", "OK"]  # lowest first, foreign invisible
        assert result["count"] == 3

    def test_low_stock_only_keeps_rows_at_or_below_the_reorder_point(self, db_session):
        low_a = _product(db_session, 1, "L-A", "Low A")
        ok = _product(db_session, 1, "OK", "Plenty")
        db_session.flush()
        db_session.add_all(
            [Inventory(product_id=low_a.id, quantity=3, reorder_point=10), Inventory(product_id=ok.id, quantity=40)]
        )
        db_session.commit()

        result = get_inventory(_ctx(db_session, org=1), GetInventoryInput(low_stock_only=True))

        assert [i["sku"] for i in result["items"]] == ["L-A"]


class TestGetSalesSummary:
    def test_it_sums_only_orders_inside_the_window(self, db_session):
        p = _product(db_session, 1, "S-1", "Candle")
        db_session.flush()
        recent_a = Order(product_id=p.id, quantity=1, total=10.5)
        recent_b = Order(product_id=p.id, quantity=3, total=20.25)
        stale = Order(product_id=p.id, quantity=1, total=999.0, created_at=utcnow() - timedelta(days=40))
        db_session.add_all([recent_a, recent_b, stale])
        db_session.commit()

        result = get_sales_summary(_ctx(db_session, org=1), GetSalesSummaryInput(days=30))

        assert result == {"period_days": 30, "order_count": 2, "total_revenue": 30.75}

    def test_it_is_scoped_to_the_callers_tenant(self, db_session):
        mine = _product(db_session, 1, "M-S", "Mine")
        theirs = _product(db_session, 2, "F-S", "Theirs")
        db_session.flush()
        db_session.add_all([Order(product_id=mine.id, total=100.0), Order(product_id=theirs.id, total=50.0)])
        db_session.commit()

        result = get_sales_summary(_ctx(db_session, org=1), GetSalesSummaryInput(days=30))

        assert result["order_count"] == 1  # the foreign order must not count
        assert result["total_revenue"] == 100.0  # ... nor leak into the revenue



class TestUpdateProduct:
    def test_it_changes_only_the_fields_it_was_given(self, db_session):
        p = _product(db_session, 1, "U-1", "Old Title", description="old text")
        db_session.commit()

        result = update_product(
            _ctx(db_session, org=1), UpdateProductInput(product_id=p.id, title="New Title", description="new text")
        )

        assert result == {"sku": "U-1", "updated_fields": ["title", "description"]}
        fresh = db_session.get(Product, p.id)
        assert (fresh.title, fresh.description) == ("New Title", "new text")
        assert (fresh.price, fresh.category) == (10.0, "test")  # fields not passed stay untouched

    def test_repeating_current_values_reports_nothing_updated(self, db_session):
        p = _product(db_session, 1, "U-2", "Same", description="same")
        db_session.commit()

        result = update_product(
            _ctx(db_session, org=1), UpdateProductInput(product_id=p.id, title="Same", description="same")
        )

        assert result["updated_fields"] == []

    def test_missing_or_foreign_product_raises_not_found(self, db_session):
        foreign = _product(db_session, 2, "F-U", "Foreign")
        db_session.commit()
        ctx = _ctx(db_session, org=1)
        with pytest.raises(ValueError, match="not found"):
            update_product(ctx, UpdateProductInput(product_id=999))
        with pytest.raises(ValueError):
            update_product(ctx, UpdateProductInput(product_id=foreign.id, title="x"))


class TestListOpportunities:
    def _opp(self, db, org, type_, title, score, when=None, summary=""):
        row = Opportunity(
            organization_id=org, type=type_, title=title, score=score, summary=summary, discovered_at=when or utcnow()
        )
        db.add(row)
        return row

    def test_it_filters_by_kind_orders_by_score_and_truncates_summaries(self, db_session):
        t_old = utcnow() - timedelta(days=2)
        t_new = utcnow() - timedelta(days=1)
        self._opp(db_session, 1, "gap", "Gap hi", 0.9)
        self._opp(db_session, 1, "trend", "Trend A", 0.7, when=t_old)
        self._opp(db_session, 1, "trend", "Trend B", 0.7, when=t_new)
        self._opp(db_session, 1, "gap", "Gap lo", 0.5, summary="s" * 300)
        db_session.commit()

        all_rows = list_opportunities(_ctx(db_session, org=1), ListOpportunitiesInput())
        # equal scores fall back to the most recently discovered first (Trend B before Trend A)
        assert [o["title"] for o in all_rows["opportunities"]] == ["Gap hi", "Trend B", "Trend A", "Gap lo"]

        gaps = list_opportunities(_ctx(db_session, org=1), ListOpportunitiesInput(kind="gap"))
        assert [o["title"] for o in gaps["opportunities"]] == ["Gap hi", "Gap lo"]
        long_summary = next(o for o in gaps["opportunities"] if o["title"] == "Gap lo")
        assert len(long_summary["summary"]) == 200  # truncated for the LLM

    def test_limit_is_respected_and_other_tenants_are_invisible(self, db_session):
        self._opp(db_session, 1, "gap", "Mine", 0.4)
        self._opp(db_session, 2, "trend", "Foreign", 0.99)
        db_session.commit()

        result = list_opportunities(_ctx(db_session, org=1), ListOpportunitiesInput(limit=5))

        assert [o["title"] for o in result["opportunities"]] == ["Mine"]  # the foreign 0.99 must not leak in


class TestListDatasets:
    def test_it_lists_newest_first_within_the_callers_tenant(self, db_session):
        old = DatasetProject(
            organization_id=1, name="Old set", trigger_word="oldword", created_at=utcnow() - timedelta(days=2)
        )
        new = DatasetProject(
            organization_id=1, name="New set", trigger_word="newword", created_at=utcnow() - timedelta(days=1)
        )
        foreign = DatasetProject(organization_id=2, name="Foreign set", trigger_word="fword")
        db_session.add_all([old, new, foreign])
        db_session.commit()

        result = list_datasets(_ctx(db_session, org=1), ListDatasetsInput())

        assert [(d["name"], d["trigger_word"]) for d in result["datasets"]] == [
            ("New set", "newword"),
            ("Old set", "oldword"),
        ]


class TestListGeneratedImages:
    def test_it_lists_newest_first_and_truncates_prompts_to_120_chars(self, db_session):
        old = GeneratedImage(
            organization_id=1,
            filename="a.png",
            prompt="p" * 200,
            workflow_slug="txt2img",
            created_at=utcnow() - timedelta(days=2),
        )
        new = GeneratedImage(
            organization_id=1,
            filename="b.png",
            prompt="short prompt",
            workflow_slug="ip2p",
            created_at=utcnow() - timedelta(days=1),
        )
        foreign = GeneratedImage(organization_id=2, filename="c.png", prompt="foreign")
        db_session.add_all([old, new, foreign])
        db_session.commit()

        result = list_generated_images(_ctx(db_session, org=1), ListGeneratedImagesInput())

        assert [i["filename"] for i in result["images"]] == ["b.png", "a.png"]  # newest first, foreign invisible
        long_prompt = next(i for i in result["images"] if i["filename"] == "a.png")
        assert len(long_prompt["prompt"]) == 120
        assert long_prompt["workflow"] == "txt2img"


class TestGenerateImage:
    def test_it_forwards_workflow_and_prompt_payload_to_the_service(self, db_session):
        service = _FakeComfyuiService(saved="out.png")
        ctx = _ctx(db_session, org=1, services={"comfyui": service})

        result = generate_image(
            ctx,
            GenerateImageInput(
                workflow_id="txt2img", prompt="a candle", negative_prompt="blurry", lora_name="vanilla_lora"
            ),
        )

        assert result == {"generated": "out.png"}
        assert service.calls == [
            ("txt2img", {"prompt": "a candle", "negative_prompt": "blurry", "lora_name": "vanilla_lora"})
        ]


class TestStartTraining:
    def test_it_queues_the_run_and_names_the_project(self, db_session):
        project = TrainingProject(organization_id=1, name="Vanilla LoRA")
        db_session.add(project)
        db_session.commit()

        result = start_training(_ctx(db_session, org=1), StartTrainingInput(project_id=project.id))

        assert result["queued"] is True
        assert result["project_id"] == project.id
        assert "Vanilla LoRA" in result["message"]

    def test_missing_or_foreign_project_raises_not_found(self, db_session):
        foreign = TrainingProject(organization_id=2, name="Foreign LoRA")
        db_session.add(foreign)
        db_session.commit()
        ctx = _ctx(db_session, org=1)
        with pytest.raises(ValueError, match="not found"):
            start_training(ctx, StartTrainingInput(project_id=999))
        with pytest.raises(ValueError):
            start_training(ctx, StartTrainingInput(project_id=foreign.id))


class TestRegisterAll:
    def test_every_tool_name_is_registered_with_its_access_class(self):
        registry = ToolRegistry()
        register_all(registry)

        assert {t.name for t in registry.list()} == {
            "documents.search",
            "documents.list",
            "ecommerce.get_products",
            "ecommerce.get_product",
            "ecommerce.get_inventory",
            "ecommerce.get_sales_summary",
            "ecommerce.update_product",
            "research.list_opportunities",
            "datasets.list",
            "generation.list_images",
            "generation.generate_image",
            "training.start_training",
        }
        # WRITE is the approval boundary: exactly those pause for a human.
        write_tools = {t.name for t in registry.list(access=ToolAccess.WRITE)}
        assert write_tools == {"ecommerce.update_product", "generation.generate_image", "training.start_training"}
        assert all(t.requires_approval for t in registry.list(access=ToolAccess.WRITE))
        assert not any(t.requires_approval for t in registry.list(access=ToolAccess.READ))
