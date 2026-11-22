"""Unit tests for app/datasets/service.py (Argo dataset domain service).

The service pairs on-disk images with same-stem .txt caption files and
tenant-scoped SQL rows. Every branch is exercised against tmp_path folders
and the shared SQLite session: the vision model is faked by patching
get_llm_provider, and the Pillow-dependent lines run through the fake PIL
module shared with test_dataset_validation (Pillow is an experimental extra,
not in requirements.txt).
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database.creation_models import DatasetImage, DatasetProject
from app.datasets.service import CAPTION_PROMPT, DatasetError, DatasetService, ImageInfo, _clean_caption
from app.llm import LLMError
from tests.test_dataset_validation import _image_bytes, _install_fake_pil

ORG = 1


class FakeVisionProvider:
    """Scripted stand-in for the vision model used by generate_captions."""

    name = "fake-vision"

    def __init__(self, responses: list[str] | None = None, error: str | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def vision_complete(self, prompt: str, image_b64: str) -> str:
        if self.error is not None:
            raise LLMError(self.error)
        self.calls.append((prompt, image_b64))
        if self.responses:
            return self.responses.pop(0)
        return "a red fox in a field"


@pytest.fixture()
def svc(tmp_path: Path) -> DatasetService:
    """A service rooted in the test's own tmp folder, never the repo data dir."""
    return DatasetService(str(tmp_path / "data"))


def _dataset(db_session: Session, svc: DatasetService, name="ds", organization_id=ORG) -> DatasetProject:
    return svc.create_dataset(db_session, name=name, organization_id=organization_id)


def _image(
    db_session: Session,
    svc: DatasetService,
    ds: DatasetProject,
    filename="a.png",
    content=b"fake-png-bytes",
) -> DatasetImage:
    return svc.add_image(db_session, ds.id, filename, content, organization_id=ORG)


class TestInitAndPaths:
    def test_init_creates_nested_data_dir(self, tmp_path):
        target = tmp_path / "a" / "b"
        DatasetService(str(target))
        assert target.is_dir()

    def test_dataset_dir_is_created_under_data_dir(self, svc, tmp_path):
        path = svc.dataset_dir(7)
        assert path == tmp_path / "data" / "dataset_7"
        assert path.is_dir()
        # A second call returns the same existing directory.
        assert svc.dataset_dir(7) == path

    def test_image_path_joins_dataset_dir_and_filename(self, svc):
        row = DatasetImage(filename="a.png")
        assert svc._image_path(3, row) == svc.dataset_dir(3) / "a.png"


class TestProjects:
    def test_create_dataset_persists_fields_and_makes_folder(self, db_session, svc):
        row = svc.create_dataset(
            db_session, name="foxes", description="training set", trigger_word="sks", organization_id=ORG
        )
        assert row.id == 1
        assert row.name == "foxes"
        assert row.description == "training set"
        assert row.trigger_word == "sks"
        assert row.organization_id == ORG
        assert (svc.data_dir / f"dataset_{row.id}").is_dir()

    def test_get_dataset_returns_row_or_none(self, db_session, svc):
        row = _dataset(db_session, svc)
        assert svc.get_dataset(db_session, row.id, ORG) is row
        assert svc.get_dataset(db_session, 999, ORG) is None

    def test_get_dataset_filters_by_organization(self, db_session, svc):
        row = _dataset(db_session, svc)
        assert svc.get_dataset(db_session, row.id, 2) is None
        # organization_id=None skips the tenant filter.
        assert svc.get_dataset(db_session, row.id, None) is row

    def test_list_datasets_orders_newest_first(self, db_session, svc):
        old = datetime(2026, 1, 5, tzinfo=UTC)
        new = old + timedelta(hours=1)
        a = DatasetProject(name="a", organization_id=ORG, created_at=new)
        b = DatasetProject(name="b", organization_id=ORG, created_at=old)
        db_session.add_all([a, b])
        db_session.commit()
        names = [d.name for d in svc.list_datasets(db_session, ORG)]
        assert names == ["a", "b"]

    def test_list_datasets_filters_by_organization(self, db_session, svc):
        mine = _dataset(db_session, svc)
        other = DatasetProject(name="other", organization_id=2)
        db_session.add(other)
        db_session.commit()
        ids = [d.id for d in svc.list_datasets(db_session, ORG)]
        assert ids == [mine.id]
        # organization_id=None skips the tenant filter and lists both rows.
        all_ids = sorted(d.id for d in svc.list_datasets(db_session, None))
        assert all_ids == sorted([mine.id, other.id])

    def test_delete_dataset_missing_returns_false(self, db_session, svc):
        assert svc.delete_dataset(db_session, 42, ORG) is False

    def test_delete_dataset_removes_folder_and_row(self, db_session, svc):
        row = _dataset(db_session, svc)
        folder = svc.dataset_dir(row.id)
        (folder / "a.png").write_bytes(b"x")
        assert svc.delete_dataset(db_session, row.id, ORG) is True
        assert not folder.exists()
        assert db_session.get(DatasetProject, row.id) is None


class TestAddImage:
    def test_unknown_dataset_raises_404(self, db_session, svc):
        with pytest.raises(DatasetError) as excinfo:
            svc.add_image(db_session, 999, "a.png", b"x", organization_id=ORG)
        assert excinfo.value.status_code == 404
        assert str(excinfo.value) == "Dataset not found"

    @pytest.mark.parametrize("filename", ["notes.txt", "photo.gif"])
    def test_unsupported_extension_rejected(self, db_session, svc, filename):
        ds = _dataset(db_session, svc)
        with pytest.raises(DatasetError) as excinfo:
            svc.add_image(db_session, ds.id, filename, b"x", organization_id=ORG)
        assert excinfo.value.status_code == 400
        message = str(excinfo.value)
        assert ".png" in message and ".webp" in message
        # Nothing is written or recorded for a rejected upload.
        assert list(svc.dataset_dir(ds.id).iterdir()) == []

    def test_path_traversal_name_is_flattened(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = svc.add_image(db_session, ds.id, "../../evil.png", b"x", organization_id=ORG)
        assert row.filename == "evil.png"
        assert (svc.dataset_dir(ds.id) / "evil.png").is_file()

    def test_empty_filename_falls_back_to_default(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = svc.add_image(db_session, ds.id, "", b"x", organization_id=ORG)
        assert row.filename == "image.png"

    def test_oversized_content_rejected_with_413(self, db_session, svc, monkeypatch):
        # 0.000001 MB is one byte once truncated to int; two bytes exceed it.
        monkeypatch.setattr(settings, "max_image_upload_mb", 0.000001)
        ds = _dataset(db_session, svc)
        with pytest.raises(DatasetError) as excinfo:
            svc.add_image(db_session, ds.id, "a.png", b"ab", organization_id=ORG)
        assert excinfo.value.status_code == 413
        assert str(excinfo.value) == "Image exceeds 0 MB limit"

    def test_writes_file_row_and_empty_caption(self, db_session, svc):
        ds = _dataset(db_session, svc)
        content = b"\x89PNG\r\nfake"
        row = svc.add_image(db_session, ds.id, "a.png", content, organization_id=ORG)
        folder = svc.dataset_dir(ds.id)
        assert (folder / "a.png").read_bytes() == content
        assert (folder / "a.txt").read_text(encoding="utf-8") == ""
        assert row.filename == "a.png"
        assert row.size_bytes == len(content)
        assert row.content_hash == hashlib.sha256(content).hexdigest()
        # Pillow is not installed in this environment, so dimensions stay 0.
        assert (row.width, row.height) == (0, 0)

    def test_duplicate_names_get_numbered_suffixes(self, db_session, svc):
        ds = _dataset(db_session, svc)
        first = svc.add_image(db_session, ds.id, "a.png", b"1", organization_id=ORG)
        second = svc.add_image(db_session, ds.id, "a.png", b"2", organization_id=ORG)
        third = svc.add_image(db_session, ds.id, "a.png", b"3", organization_id=ORG)
        assert [r.filename for r in (first, second, third)] == ["a.png", "a_1.png", "a_2.png"]

    def test_existing_caption_file_is_preserved(self, db_session, svc):
        ds = _dataset(db_session, svc)
        folder = svc.dataset_dir(ds.id)
        (folder / "a.txt").write_text("keep me", encoding="utf-8")
        svc.add_image(db_session, ds.id, "a.png", b"x", organization_id=ORG)
        assert (folder / "a.txt").read_text(encoding="utf-8") == "keep me"

    def test_dimensions_read_when_pil_available(self, db_session, svc, monkeypatch):
        _install_fake_pil(monkeypatch)
        ds = _dataset(db_session, svc)
        row = svc.add_image(db_session, ds.id, "a.png", _image_bytes(size=(320, 240)), organization_id=ORG)
        assert (row.width, row.height) == (320, 240)


class TestListImages:
    def test_unknown_dataset_raises_404(self, db_session, svc):
        with pytest.raises(DatasetError) as excinfo:
            svc.list_images(db_session, 999, ORG)
        assert excinfo.value.status_code == 404

    def test_orders_by_id(self, db_session, svc):
        ds = _dataset(db_session, svc)
        rows = [_image(db_session, svc, ds, filename=f"a{i}.png") for i in range(3)]
        listed = svc.list_images(db_session, ds.id, ORG)
        assert [r.id for r in listed] == [r.id for r in rows]


class TestReadImage:
    @pytest.mark.parametrize(
        ("filename", "media_type"),
        [("a.png", "image/png"), ("b.jpg", "image/jpeg"), ("c.jpeg", "image/jpeg"), ("d.webp", "image/webp")],
    )
    def test_returns_bytes_and_media_type(self, db_session, svc, filename, media_type):
        ds = _dataset(db_session, svc)
        row = _image(db_session, svc, ds, filename=filename, content=b"payload")
        data, mime = svc.read_image(db_session, ds.id, row.id, ORG)
        assert data == b"payload"
        assert mime == media_type

    def test_unknown_extension_falls_back_to_octet_stream(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = DatasetImage(dataset_id=ds.id, filename="photo.tiff")
        db_session.add(row)
        db_session.commit()
        (svc.dataset_dir(ds.id) / "photo.tiff").write_bytes(b"tiff-bytes")
        data, mime = svc.read_image(db_session, ds.id, row.id, ORG)
        assert data == b"tiff-bytes"
        assert mime == "application/octet-stream"

    def test_vanished_file_raises_404(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = _image(db_session, svc, ds)
        (svc.dataset_dir(ds.id) / "a.png").unlink()
        with pytest.raises(DatasetError) as excinfo:
            svc.read_image(db_session, ds.id, row.id, ORG)
        assert excinfo.value.status_code == 404
        assert "missing on disk" in str(excinfo.value)

    def test_image_from_other_dataset_raises_404(self, db_session, svc):
        first = _dataset(db_session, svc)
        second = _dataset(db_session, svc)
        row = _image(db_session, svc, first)
        with pytest.raises(DatasetError) as excinfo:
            svc.read_image(db_session, second.id, row.id, ORG)
        assert str(excinfo.value) == "Image not found"

    def test_missing_row_raises_404(self, db_session, svc):
        ds = _dataset(db_session, svc)
        with pytest.raises(DatasetError) as excinfo:
            svc.read_image(db_session, ds.id, 999, ORG)
        assert str(excinfo.value) == "Image not found"

    def test_unknown_dataset_raises_404(self, db_session, svc):
        # _get_image checks the dataset before looking up the image row.
        ds = _dataset(db_session, svc)
        row = _image(db_session, svc, ds)
        with pytest.raises(DatasetError) as excinfo:
            svc.read_image(db_session, 999, row.id, ORG)
        assert excinfo.value.status_code == 404
        assert str(excinfo.value) == "Dataset not found"


class TestDeleteImage:
    def test_removes_files_and_row(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = _image(db_session, svc, ds)
        folder = svc.dataset_dir(ds.id)
        (folder / "a.txt").write_text("caption", encoding="utf-8")
        assert svc.delete_image(db_session, ds.id, row.id, ORG) is True
        assert not (folder / "a.png").exists()
        assert not (folder / "a.txt").exists()
        assert db_session.get(DatasetImage, row.id) is None

    def test_missing_files_are_tolerated(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = _image(db_session, svc, ds)
        folder = svc.dataset_dir(ds.id)
        (folder / "a.png").unlink()
        (folder / "a.txt").unlink()
        assert svc.delete_image(db_session, ds.id, row.id, ORG) is True
        assert db_session.get(DatasetImage, row.id) is None


class TestSetCaption:
    def test_strips_caption_marks_human_and_writes_file(self, db_session, svc):
        ds = _dataset(db_session, svc)
        row = _image(db_session, svc, ds)
        updated = svc.set_caption(db_session, ds.id, row.id, "  a red fox \n", ORG)
        assert updated.caption == "a red fox"
        assert updated.captioned_by == "human"
        assert (svc.dataset_dir(ds.id) / "a.txt").read_text(encoding="utf-8") == "a red fox"

    def test_unknown_image_raises_404(self, db_session, svc):
        ds = _dataset(db_session, svc)
        with pytest.raises(DatasetError) as excinfo:
            svc.set_caption(db_session, ds.id, 999, "x", ORG)
        assert str(excinfo.value) == "Image not found"


class TestValidate:
    def test_report_for_clean_dataset(self, db_session, svc, monkeypatch):
        _install_fake_pil(monkeypatch)
        ds = _dataset(db_session, svc)
        # Far-apart phashes (64 bits differ) keep the pair classified unique.
        a = _image(db_session, svc, ds, filename="a.png", content=_image_bytes(seed="a", phash=0xAAAAAAAAAAAAAAA))
        b = _image(db_session, svc, ds, filename="b.png", content=_image_bytes(seed="b", phash=0x5555555555555555))
        svc.set_caption(db_session, ds.id, a.id, "a red fox", ORG)
        svc.set_caption(db_session, ds.id, b.id, "blue sky over water", ORG)
        report = svc.validate(db_session, ds.id, ORG)
        assert report["total_images"] == 2
        assert report["valid_images"] == 2
        assert report["captioned"] == 2
        assert report["missing_captions"] == 0
        assert report["score"] == 100
        assert report["average_resolution"] == [320, 320]
        assert report["findings"] == []

    def test_vanished_file_counts_as_invalid(self, db_session, svc, monkeypatch):
        _install_fake_pil(monkeypatch)
        ds = _dataset(db_session, svc)
        _image(db_session, svc, ds, filename="a.png", content=_image_bytes(seed="a"))
        _image(db_session, svc, ds, filename="b.png", content=_image_bytes(seed="b"))
        (svc.dataset_dir(ds.id) / "a.png").unlink()
        report = svc.validate(db_session, ds.id, ORG)
        assert report["total_images"] == 2
        assert report["valid_images"] == 1
        kinds = {(f["image_id"], f["kind"]) for f in report["findings"]}
        # Both images lack captions; the vanished file is additionally invalid.
        assert kinds == {("1", "missing_caption"), ("2", "missing_caption"), ("1", "invalid")}
        # 2 missing captions (5 each) plus one invalid image (15).
        assert report["score"] == 75

    def test_missing_caption_lowers_score(self, db_session, svc, monkeypatch):
        _install_fake_pil(monkeypatch)
        ds = _dataset(db_session, svc)
        _image(db_session, svc, ds, content=_image_bytes(seed="a"))
        report = svc.validate(db_session, ds.id, ORG)
        assert report["captioned"] == 0
        assert report["missing_captions"] == 1
        assert report["score"] == 95


class TestGenerateCaptions:
    def _patch_provider(self, monkeypatch, provider):
        monkeypatch.setattr("app.datasets.service.get_llm_provider", lambda: provider)

    def test_unknown_dataset_raises_404(self, db_session, svc, monkeypatch):
        self._patch_provider(monkeypatch, FakeVisionProvider())
        with pytest.raises(DatasetError) as excinfo:
            svc.generate_captions(db_session, 999, ORG)
        assert excinfo.value.status_code == 404

    def test_writes_captions_files_and_progress(self, db_session, svc, monkeypatch):
        provider = FakeVisionProvider(responses=["a red fox in a field", "blue sky over water"])
        self._patch_provider(monkeypatch, provider)
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds, filename="a.png", content=b"img-a")
        b = _image(db_session, svc, ds, filename="b.png", content=b"img-b")
        calls: list[tuple[int, int]] = []
        results = svc.generate_captions(db_session, ds.id, ORG, progress=lambda d, t: calls.append((d, t)))
        assert results == [
            {"id": a.id, "filename": "a.png", "caption": "a red fox in a field"},
            {"id": b.id, "filename": "b.png", "caption": "blue sky over water"},
        ]
        assert calls == [(1, 2), (2, 2)]
        # The default model name is recorded on the row and mirrored to disk.
        assert a.captioned_by == settings.vision_model
        folder = svc.dataset_dir(ds.id)
        assert (folder / "a.txt").read_text(encoding="utf-8") == "a red fox in a field"
        assert (folder / "b.txt").read_text(encoding="utf-8") == "blue sky over water"

    def test_explicit_model_is_recorded(self, db_session, svc, monkeypatch):
        self._patch_provider(monkeypatch, FakeVisionProvider())
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        svc.generate_captions(db_session, ds.id, ORG, model="my-vision")
        assert a.captioned_by == "my-vision"

    def test_prompt_and_base64_are_passed_to_provider(self, db_session, svc, monkeypatch):
        provider = FakeVisionProvider()
        self._patch_provider(monkeypatch, provider)
        ds = _dataset(db_session, svc)
        _image(db_session, svc, ds, content=b"img-a")
        svc.generate_captions(db_session, ds.id, ORG)
        assert provider.calls == [(CAPTION_PROMPT, base64.b64encode(b"img-a").decode("ascii"))]

    def test_trigger_word_is_prepended_when_absent(self, db_session, svc, monkeypatch):
        self._patch_provider(monkeypatch, FakeVisionProvider(responses=["a red fox", "sks, blue sky"]))
        ds = svc.create_dataset(db_session, name="ds", trigger_word="sks", organization_id=ORG)
        _image(db_session, svc, ds, filename="a.png")
        _image(db_session, svc, ds, filename="b.png")
        results = svc.generate_captions(db_session, ds.id, ORG)
        # First response lacks the trigger word and gets it; second already has it.
        assert [r["caption"] for r in results] == ["sks, a red fox", "sks, blue sky"]

    def test_human_captions_are_skipped_unless_overwritten(self, db_session, svc, monkeypatch):
        self._patch_provider(monkeypatch, FakeVisionProvider(responses=["new caption"]))
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        svc.set_caption(db_session, ds.id, a.id, "human text", ORG)
        results = svc.generate_captions(db_session, ds.id, ORG)
        assert results == []
        # The provider was never called and the human file is untouched.
        assert (svc.dataset_dir(ds.id) / "a.txt").read_text(encoding="utf-8") == "human text"

    def test_overwrite_replaces_human_caption(self, db_session, svc, monkeypatch):
        self._patch_provider(monkeypatch, FakeVisionProvider(responses=["new caption"]))
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        svc.set_caption(db_session, ds.id, a.id, "human text", ORG)
        results = svc.generate_captions(db_session, ds.id, ORG, overwrite=True)
        assert results == [{"id": a.id, "filename": "a.png", "caption": "new caption"}]
        assert a.captioned_by == settings.vision_model

    def test_model_captions_are_replaced(self, db_session, svc, monkeypatch):
        # Only human-edited captions are protected; model ones get refreshed.
        self._patch_provider(monkeypatch, FakeVisionProvider(responses=["fresh"]))
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        a.caption = "old"
        a.captioned_by = settings.vision_model
        db_session.commit()
        results = svc.generate_captions(db_session, ds.id, ORG)
        assert [r["caption"] for r in results] == ["fresh"]

    def test_vanished_file_is_skipped(self, db_session, svc, monkeypatch):
        provider = FakeVisionProvider()
        self._patch_provider(monkeypatch, provider)
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        (svc.dataset_dir(ds.id) / "a.png").unlink()
        results = svc.generate_captions(db_session, ds.id, ORG)
        assert results == []
        assert provider.calls == []
        assert a.caption == ""

    def test_llm_error_is_recorded_not_raised(self, db_session, svc, monkeypatch):
        self._patch_provider(monkeypatch, FakeVisionProvider(error="boom"))
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        results = svc.generate_captions(db_session, ds.id, ORG)
        assert results == [{"id": a.id, "filename": "a.png", "error": "boom"}]
        # The empty caption file created by add_image is left untouched.
        assert (svc.dataset_dir(ds.id) / "a.txt").read_text(encoding="utf-8") == ""

    def test_cancelled_stops_before_any_call(self, db_session, svc, monkeypatch):
        provider = FakeVisionProvider()
        self._patch_provider(monkeypatch, provider)
        ds = _dataset(db_session, svc)
        a = _image(db_session, svc, ds)
        results = svc.generate_captions(db_session, ds.id, ORG, cancelled=lambda: True)
        assert results == []
        assert provider.calls == []
        assert a.caption == ""


class TestModuleSurface:
    def test_image_info_fields(self):
        info = ImageInfo(id="1", filename="a.png", caption="", caption_edited=False, size_bytes=3)
        assert (info.id, info.filename, info.caption) == ("1", "a.png", "")
        assert (info.caption_edited, info.size_bytes) == (False, 3)

    def test_caption_prompt_shape(self):
        assert CAPTION_PROMPT.startswith("Write a single-line image caption")
        assert "Do not use full sentences" in CAPTION_PROMPT

    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("  a   red\nfox  ", "a red fox"),
            ("here is a cat", "a cat"),
            ("This is a dog", "a dog"),
            ("The image shows a bird", "a bird"),
            ("Sure, a fish", "a fish"),
            ('"a cat"', "a cat"),
        ],
    )
    def test_clean_caption(self, raw, clean):
        assert _clean_caption(raw) == clean
