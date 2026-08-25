"""Argo — dataset domain service.

Adapted from Apollo `LoRAProjectService` (dataset half): project-scoped image
datasets with image + same-stem ``.txt`` caption pairing on disk (AI Toolkit's
expected layout) and tenant-scoped metadata rows in SQL.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database.creation_models import DatasetImage, DatasetProject
from app.datasets.validation import validate_dataset
from app.llm import LLMError, get_llm_provider

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp"]

CAPTION_PROMPT = (
    "Write a single-line image caption for training a LoRA model. Describe the "
    "subject, clothing, pose, setting, lighting and camera framing as a comma-"
    "separated list of short phrases. Do not use full sentences, do not add "
    "commentary, and do not start with 'a photo of'. Reply with the caption only."
)


class DatasetError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class ImageInfo:
    id: str
    filename: str
    caption: str
    caption_edited: bool
    size_bytes: int


class DatasetService:
    """Create datasets, manage images + captions, validate quality."""

    def __init__(self, data_dir: str | None = None) -> None:
        self.data_dir = Path(data_dir or settings.lora_data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------- paths ----------

    def dataset_dir(self, dataset_id: int) -> Path:
        from app.core.security.paths import safe_join

        path = safe_join(self.data_dir, f"dataset_{dataset_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _image_path(self, dataset_id: int, image_row: DatasetImage) -> Path:
        return self.dataset_dir(dataset_id) / image_row.filename

    # ---------- projects ----------

    def create_dataset(
        self,
        db: Session,
        *,
        name: str,
        description: str = "",
        trigger_word: str = "",
        organization_id: int | None = None,
        created_by: int | None = None,
    ) -> DatasetProject:
        row = DatasetProject(
            organization_id=organization_id,
            name=name,
            description=description,
            trigger_word=trigger_word,
            created_by=created_by,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        self.dataset_dir(row.id)  # ensure dir exists
        return row

    def get_dataset(self, db: Session, dataset_id: int, organization_id: int | None) -> DatasetProject | None:
        query = select(DatasetProject).where(DatasetProject.id == dataset_id)
        if organization_id is not None:
            query = query.where(DatasetProject.organization_id == organization_id)
        return db.execute(query).scalars().first()

    def list_datasets(self, db: Session, organization_id: int | None) -> list[DatasetProject]:
        query = select(DatasetProject).order_by(DatasetProject.created_at.desc())
        if organization_id is not None:
            query = query.where(DatasetProject.organization_id == organization_id)
        return list(db.execute(query).scalars())

    def delete_dataset(self, db: Session, dataset_id: int, organization_id: int | None) -> bool:
        row = self.get_dataset(db, dataset_id, organization_id)
        if row is None:
            return False
        import shutil

        shutil.rmtree(self.dataset_dir(dataset_id), ignore_errors=True)
        db.delete(row)
        db.commit()
        return True

    # ---------- images ----------

    def add_image(
        self,
        db: Session,
        dataset_id: int,
        filename: str,
        content: bytes,
        organization_id: int | None = None,
    ) -> DatasetImage:
        dataset = self.get_dataset(db, dataset_id, organization_id)
        if dataset is None:
            raise DatasetError("Dataset not found", status_code=404)

        from app.core.security.paths import sanitize_filename

        safe_name = sanitize_filename(filename, default="image.png")
        if not any(safe_name.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
            raise DatasetError(
                f"Unsupported image type; allowed: {', '.join(IMAGE_EXTENSIONS)}"
            )
        if len(content) > settings.max_image_upload_bytes:
            raise DatasetError(
                f"Image exceeds {settings.max_image_upload_mb:.0f} MB limit", status_code=413
            )

        target = self.dataset_dir(dataset_id) / safe_name
        stem, suffix = target.stem, target.suffix
        counter = 1
        while target.exists():
            target = self.dataset_dir(dataset_id) / f"{stem}_{counter}{suffix}"
            counter += 1
        target.write_bytes(content)

        caption_path = target.with_suffix(".txt")
        if not caption_path.exists():
            caption_path.write_text("", encoding="utf-8")

        content_hash = hashlib.sha256(content).hexdigest()
        width = height = 0
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(content)) as img:
                width, height = img.size
        except Exception:  # noqa: BLE001 — dimensions are advisory
            pass

        row = DatasetImage(
            dataset_id=dataset_id,
            filename=target.name,
            caption="",
            width=width,
            height=height,
            size_bytes=len(content),
            content_hash=content_hash,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list_images(self, db: Session, dataset_id: int, organization_id: int | None) -> list[DatasetImage]:
        if self.get_dataset(db, dataset_id, organization_id) is None:
            raise DatasetError("Dataset not found", status_code=404)
        query = (
            select(DatasetImage)
            .where(DatasetImage.dataset_id == dataset_id)
            .order_by(DatasetImage.id)
        )
        return list(db.execute(query).scalars())

    def read_image(self, db: Session, dataset_id: int, image_id: int, organization_id: int | None) -> tuple[bytes, str]:
        row = self._get_image(db, dataset_id, image_id, organization_id)
        path = self._image_path(dataset_id, row)
        if not path.is_file():
            raise DatasetError(f"Image file '{row.filename}' missing on disk", status_code=404)
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "application/octet-stream")
        return path.read_bytes(), media_type

    def delete_image(self, db: Session, dataset_id: int, image_id: int, organization_id: int | None) -> bool:
        row = self._get_image(db, dataset_id, image_id, organization_id)
        path = self._image_path(dataset_id, row)
        path.unlink(missing_ok=True)
        path.with_suffix(".txt").unlink(missing_ok=True)
        db.delete(row)
        db.commit()
        return True

    def set_caption(
        self, db: Session, dataset_id: int, image_id: int, caption: str,
        organization_id: int | None = None,
    ) -> DatasetImage:
        row = self._get_image(db, dataset_id, image_id, organization_id)
        row.caption = caption.strip()
        row.captioned_by = "human"
        path = self._image_path(dataset_id, row)
        path.with_suffix(".txt").write_text(row.caption, encoding="utf-8")
        db.commit()
        db.refresh(row)
        return row

    def _get_image(self, db: Session, dataset_id: int, image_id: int, organization_id: int | None) -> DatasetImage:
        if self.get_dataset(db, dataset_id, organization_id) is None:
            raise DatasetError("Dataset not found", status_code=404)
        row = db.get(DatasetImage, image_id)
        if row is None or row.dataset_id != dataset_id:
            raise DatasetError("Image not found", status_code=404)
        return row

    # ---------- quality ----------

    def validate(self, db: Session, dataset_id: int, organization_id: int | None) -> dict:
        rows = self.list_images(db, dataset_id, organization_id)
        entries = [
            {"id": str(r.id), "filename": r.filename, "caption": r.caption}
            for r in rows
        ]
        by_id = {str(r.id): r for r in rows}

        def read_bytes(image_id: str) -> bytes:
            row = by_id[image_id]
            return self._image_path(dataset_id, row).read_bytes()

        report = validate_dataset(entries, read_bytes)
        return report.to_dict()

    # ---------- AI captions ----------

    def generate_captions(
        self,
        db: Session,
        dataset_id: int,
        organization_id: int | None,
        *,
        model: str | None = None,
        overwrite: bool = False,
        progress=None,
        cancelled=None,
    ) -> list[dict]:
        """Caption uncaptioned images via the vision model."""
        provider = get_llm_provider()
        vision_model = model or settings.vision_model
        dataset = self.get_dataset(db, dataset_id, organization_id)
        if dataset is None:
            raise DatasetError("Dataset not found", status_code=404)

        results: list[dict] = []
        rows = self.list_images(db, dataset_id, organization_id)
        total = len(rows)

        for done, row in enumerate(rows, start=1):
            if cancelled and cancelled():
                break
            if row.caption.strip() and row.captioned_by == "human" and not overwrite:
                continue

            path = self._image_path(dataset_id, row)
            if not path.is_file():
                continue

            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            try:
                caption = provider.vision_complete(CAPTION_PROMPT, encoded)
            except LLMError as exc:
                logger.warning("[DATASET] Vision captioning failed for %s: %s", row.filename, exc.message)
                results.append({"id": row.id, "filename": row.filename, "error": exc.message})
                continue

            caption = _clean_caption(caption)
            if dataset.trigger_word and dataset.trigger_word.lower() not in caption.lower():
                caption = f"{dataset.trigger_word}, {caption}"

            row.caption = caption
            row.captioned_by = vision_model
            path.with_suffix(".txt").write_text(caption, encoding="utf-8")
            db.commit()
            results.append({"id": row.id, "filename": row.filename, "caption": caption})

            if progress:
                progress(done, total)

        return results


def _clean_caption(text: str) -> str:
    caption = " ".join(text.strip().split())
    caption = re.sub(r"^(here is |this is |the image shows |sure[,!] )", "", caption, flags=re.I)
    return caption.strip().strip('"').strip()


__all__ = ["CAPTION_PROMPT", "DatasetError", "DatasetService", "ImageInfo"]
