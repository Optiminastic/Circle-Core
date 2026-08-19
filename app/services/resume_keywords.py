"""Resume text extraction + keyword matching against a job's keyword list.

Runs once, synchronously, inside POST /api/public/apply — the resume bytes
and the job document are already in memory there (same place `compute_fit`
already runs for screening answers). Both functions are best-effort: a
malformed/image-only (scanned) PDF must never block a candidate's
application, it just yields no extractable text and zero keyword matches.
"""

from __future__ import annotations

import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.logging import get_logger

logger = get_logger("curcle.resume_keywords")


def extract_text(pdf_bytes: bytes) -> str:
    """Best-effort text extraction from a PDF's raw bytes. Returns "" on any
    failure (encrypted, corrupt, or scanned/image-only with no text layer) —
    never raises, so a resume that can't be parsed still lets the candidate
    apply."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip()
    except (PdfReadError, ValueError, KeyError) as exc:
        logger.warning("Could not extract resume text: %s", exc)
        return ""
    except Exception:  # noqa: BLE001 - resume parsing must never block an application
        logger.exception("Unexpected error extracting resume text.")
        return ""


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """The subset of `keywords` found in `text`, case-insensitive, matched on
    word boundaries (so "React" doesn't match inside "Reaction"). Multi-word
    keywords (e.g. "Game Design") match as a literal phrase. Preserves the
    original casing/order of `keywords`, not `text`."""
    if not text or not keywords:
        return []
    matched: list[str] = []
    for keyword in keywords:
        term = keyword.strip()
        if not term:
            continue
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        if re.search(pattern, text, flags=re.IGNORECASE):
            matched.append(keyword)
    return matched
