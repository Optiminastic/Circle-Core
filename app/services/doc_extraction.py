"""Reads an uploaded joining document and grades how well it parsed.

Sits between the route and the OCR/field modules so the route stays thin
(route -> service -> repository). Nothing here talks HTTP.

The grade answers the question HR actually has: "is this image clear enough to
trust?". Neither signal alone is enough - Tesseract is often confidently wrong
on a blurry digit, and a format check says nothing about a half-read address -
so the two are combined.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services import doc_fields, ocr
from app.storage.base import FileStorage

logger = get_logger("curcle.doc_extraction")

DOCS_TABLE = "documents"

# Tesseract's mean word confidence, 0-100.
_CLEAR_CONFIDENCE = 75.0
_UNREADABLE_CONFIDENCE = 50.0

QUALITY_CLEAR = "clear"
QUALITY_NEEDS_REVIEW = "needs_review"
QUALITY_UNREADABLE = "unreadable"

ENGINE = "tesseract"


def grade(confidence: float, validated: bool, has_text: bool) -> str:
    """How much HR should trust what came back."""
    if not has_text or confidence < _UNREADABLE_CONFIDENCE:
        return QUALITY_UNREADABLE
    if confidence >= _CLEAR_CONFIDENCE and validated:
        return QUALITY_CLEAR
    return QUALITY_NEEDS_REVIEW


def extract_bytes(data: bytes, content_type: str | None, doc_type: str) -> dict[str, Any]:
    """OCR a document already in memory and return its extraction block.

    Never raises on a bad document - an unreadable file comes back graded
    `unreadable` with empty fields, and the values get typed in instead.
    """
    result = ocr.extract(data, content_type)
    fields = doc_fields.extract_fields(doc_type, result.text)
    quality = grade(result.mean_confidence, fields.validated, bool(result.text.strip()))

    # Deliberately logs no extracted value - only the grade. These documents
    # carry Aadhaar and PAN numbers.
    logger.info(
        "Extracted '%s': quality=%s confidence=%.0f source=%s",
        doc_type,
        quality,
        result.mean_confidence,
        result.source,
    )

    return _block(
        quality=quality,
        warnings=fields.warnings,
        fields=fields.fields,
        confidence=result.mean_confidence,
        validated_fields=fields.validated_fields,
    )


def extract_submission(
    repo: DocumentRepository,
    storage: FileStorage,
    submission: dict[str, Any],
    doc_type: str,
) -> dict[str, Any]:
    """Fetch one submission's stored file and extract from it."""
    document_id = submission.get("documentId")
    meta = repo.get(DOCS_TABLE, document_id) if document_id else None
    if not meta or not meta.get("storageKey"):
        return _block(
            quality=QUALITY_UNREADABLE,
            warnings=["The uploaded file could not be found in storage."],
        )

    try:
        data, content_type = storage.get(meta["storageKey"])
    except Exception:  # noqa: BLE001 - a storage blip must not 500 HR's review
        logger.exception("Could not fetch document %s from storage.", document_id)
        return _block(
            quality=QUALITY_UNREADABLE,
            warnings=["The uploaded file could not be read from storage."],
        )

    return extract_bytes(data, content_type or meta.get("contentType"), doc_type)


def manual_block() -> dict[str, Any]:
    """An extraction block for values HR typed in themselves, with no OCR behind
    them (an unreadable image, or Tesseract not installed)."""
    return _block(
        quality=QUALITY_NEEDS_REVIEW,
        warnings=["Entered manually by HR."],
    )


def _block(
    *,
    quality: str,
    warnings: list[str],
    fields: dict[str, str] | None = None,
    confidence: float = 0.0,
    validated_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "engine": ENGINE,
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "quality": quality,
        "meanConfidence": round(confidence, 1),
        "fields": fields or {},
        "warnings": warnings,
        # Fields a machine check confirmed; the rest a human may correct.
        "validatedFields": list(validated_fields),
    }
