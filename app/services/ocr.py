"""Text extraction from uploaded document images, via Tesseract.

Self-hosted deliberately: joining documents carry Aadhaar numbers, and under the
DPDP Act that data should not leave our infrastructure for a third-party OCR
service.

Best-effort throughout, in the same spirit as `resume_keywords.extract_text` -
a document that cannot be read must never raise, it just yields no text and HR
types the values in by hand.

Tesseract is a system binary, not a pip package. It is installed in the
Dockerfile but will usually be absent on a developer's Windows machine, so
`ocr_available()` gates the whole feature and every caller must check it.
"""

from __future__ import annotations

import io
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

from app.core.logging import get_logger
from app.services.resume_keywords import extract_text as extract_pdf_text

logger = get_logger("curcle.ocr")

# Tesseract reports per-word confidence as 0-100, using -1 for non-word boxes.
_NO_CONFIDENCE = -1
_FULL_CONFIDENCE = 100.0
# A scanned PDF page at 300 DPI is what Tesseract is tuned for.
_PDF_RENDER_DPI = 300
# Only the first pages are OCR'd; ID documents are one or two pages and this
# caps the cost of someone uploading a 50-page scan.
_MAX_PDF_PAGES = 3

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]")

# OCR is CPU-heavy and now runs on the public upload path, where there is no
# queue to absorb it. One at a time per worker, and callers that can't get a
# slot quickly skip OCR rather than queue up behind it - a slow upload is worse
# than a missing pre-fill, and the container HEALTHCHECK has a 5s timeout.
_SLOT = threading.Semaphore(1)
_SLOT_WAIT_SECONDS = 2.0


@contextmanager
def slot(timeout: float = _SLOT_WAIT_SECONDS):
    """Reserve the single OCR slot. Yields False when the engine is busy."""
    acquired = _SLOT.acquire(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            _SLOT.release()

SOURCE_PDF_TEXT = "pdf-text"
SOURCE_TESSERACT = "tesseract"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class OcrResult:
    """Extracted text plus how confident the engine was, 0-100."""

    text: str
    mean_confidence: float
    source: str


_EMPTY = OcrResult(text="", mean_confidence=0.0, source=SOURCE_NONE)


@lru_cache(maxsize=1)
def ocr_available() -> bool:
    """True when the Tesseract binary and its Python bindings are both usable.

    Cached: the answer cannot change without restarting the process, and every
    request would otherwise shell out to check.
    """
    try:
        import pytesseract  # noqa: PLC0415 - optional dependency, probed at runtime

        from app.core.config import get_settings  # noqa: PLC0415 - avoids an import cycle

        # Windows installers don't update PATH for an already-running process,
        # so the binary is usually reachable only via an explicit path there.
        configured = get_settings().ocr_tesseract_cmd.strip()
        if configured:
            pytesseract.pytesseract.tesseract_cmd = configured

        pytesseract.get_tesseract_version()
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "not available"
        logger.info("OCR is unavailable (%s). Document parsing will be skipped.", type(exc).__name__)
        return False


def extract(data: bytes, content_type: str | None) -> OcrResult:
    """Read text out of an uploaded document.

    PDFs are tried through their text layer first - that is exact and costs
    nothing. Only a PDF with no text layer (i.e. a scan) is rasterised and sent
    to Tesseract. Images go straight to Tesseract.
    """
    if not data:
        return _EMPTY

    if _is_pdf(data, content_type):
        text = extract_pdf_text(data)
        if text.strip():
            return OcrResult(text=text, mean_confidence=_FULL_CONFIDENCE, source=SOURCE_PDF_TEXT)
        return _ocr_pdf_pages(data)

    return _ocr_image(data)


def _is_pdf(data: bytes, content_type: str | None) -> bool:
    # Trust the magic bytes over the declared content type; browsers and phone
    # uploads routinely mislabel.
    return data[:5] == b"%PDF-" or (content_type or "").lower() == "application/pdf"


def _ocr_image(data: bytes) -> OcrResult:
    if not ocr_available():
        return _EMPTY
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415 - optional dependency

        with Image.open(io.BytesIO(data)) as image:
            # Phone cameras record orientation in EXIF rather than rotating the
            # pixels. Tesseract reads raw pixels, so without this a sideways
            # photo of a card scores near zero - this one line matters more
            # than any amount of regex tuning.
            return _run_tesseract(ImageOps.exif_transpose(image))
    except Exception:  # noqa: BLE001 - a bad image must not break HR's review
        logger.exception("Could not OCR the uploaded image.")
        return _EMPTY


def _ocr_pdf_pages(data: bytes) -> OcrResult:
    if not ocr_available():
        return _EMPTY
    try:
        import pypdfium2  # noqa: PLC0415 - optional dependency

        # pypdfium2 renders in-process and ships Linux + Windows wheels, so a
        # scanned PDF works on a dev laptop with no system packages.
        pdf = pypdfium2.PdfDocument(data)
        scale = _PDF_RENDER_DPI / 72  # pdfium measures in points
        pages = [
            pdf[index].render(scale=scale).to_pil()
            for index in range(min(len(pdf), _MAX_PDF_PAGES))
        ]
    except Exception:  # noqa: BLE001 - unreadable or encrypted PDF
        logger.exception("Could not rasterise the PDF for OCR.")
        return _EMPTY

    results = [_run_tesseract(page) for page in pages]
    readable = [r for r in results if r.text.strip()]
    if not readable:
        return _EMPTY
    return OcrResult(
        text="\n".join(r.text for r in readable),
        mean_confidence=sum(r.mean_confidence for r in readable) / len(readable),
        source=SOURCE_TESSERACT,
    )


def _run_tesseract(image: object) -> OcrResult:
    """OCR one already-open PIL image, returning its text and mean word confidence."""
    try:
        import pytesseract  # noqa: PLC0415 - optional dependency

        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception:  # noqa: BLE001 - never propagate an OCR failure
        logger.exception("Tesseract failed on a page.")
        return _EMPTY

    words: list[str] = []
    confidences: list[float] = []
    for word, raw_confidence in zip(data.get("text", []), data.get("conf", [])):
        if not (word or "").strip():
            continue
        confidence = _as_confidence(raw_confidence)
        if confidence is None:
            continue
        words.append(word)
        # Indian ID cards are bilingual. Reading them in English leaves the
        # Devanagari half as low-confidence noise, which would drag every
        # Aadhaar into "unreadable". Grade only on the script we actually
        # parse, but keep every word in the text.
        if _ASCII_WORD_RE.search(word):
            confidences.append(confidence)

    if not words:
        return _EMPTY

    # image_to_data loses line breaks; rebuild them so the field extractors can
    # still use "label on one line, value on the next" layouts.
    text = _rebuild_lines(data)
    return OcrResult(
        text=text or " ".join(words),
        mean_confidence=sum(confidences) / len(confidences),
        source=SOURCE_TESSERACT,
    )


def _as_confidence(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value <= _NO_CONFIDENCE else value


def _rebuild_lines(data: dict) -> str:
    """Regroup Tesseract's word boxes back into lines using its own block/line ids."""
    lines: dict[tuple, list[str]] = {}
    keys = ("block_num", "par_num", "line_num")
    for index, word in enumerate(data.get("text", [])):
        if not (word or "").strip():
            continue
        try:
            key = tuple(data[k][index] for k in keys)
        except (KeyError, IndexError):
            continue
        lines.setdefault(key, []).append(word)
    return "\n".join(" ".join(words) for _, words in sorted(lines.items()))
