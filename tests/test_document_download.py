"""Asking for a document as a download, rather than to look at.

`/url` has always handed back an `inline` link, so the browser opens the file in
a tab. That is right for viewing and wrong for saving, and it fails unevenly:
a PDF opens in the browser's PDF viewer, which carries its own download button,
while an image is just painted on the page with no way to save it. HR could
download a candidate's PAN card and not their Aadhaar photo.

Downloading is the server's decision to state, not something the browser should
be left to infer from the content type.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.api.routes.documents import download_disposition

# The fixture and login helper live with the access tests; importing them here
# makes `client` available as a fixture without duplicating the app wiring.
from tests.test_document_access import _login, client  # noqa: F401


# -- what the route asks the storage layer for --------------------------------


def test_the_default_is_still_inline_so_viewing_is_unchanged() -> None:
    assert download_disposition("report.pdf", download=False).startswith("inline;")


def test_asking_to_download_says_attachment() -> None:
    # The one word that makes a browser save a file instead of rendering it.
    assert download_disposition("report.pdf", download=True).startswith("attachment;")


@pytest.mark.parametrize("filename", ["aadhaar.jpg", "pan.png", "offer.pdf", "scan.webp"])
def test_every_file_type_downloads_the_same_way(filename: str) -> None:
    # The bug was that this depended on the type: PDFs were saveable and images
    # were not. Nothing here may branch on the extension.
    assert download_disposition(filename, download=True) == f'attachment; filename="{filename}"'


def test_the_filename_is_carried_so_the_saved_file_is_not_a_uuid() -> None:
    assert 'filename="Aadhaar card.jpg"' in download_disposition("Aadhaar card.jpg", download=True)


# -- the endpoint ------------------------------------------------------------
#
# These need a disposable PostgreSQL, like the access tests they share a fixture
# with. The rule above is a pure function and runs everywhere.

needs_db = pytest.mark.skipif(
    not os.environ.get("CIRCLE_TEST_DATABASE_URL", ""),
    reason="CIRCLE_TEST_DATABASE_URL not set",
)



@needs_db
def test_the_url_endpoint_honours_the_download_flag(client: Any) -> None:
    _login(client)
    res = client.get("/api/documents/docaadhaar01/url", params={"download": "true"})
    assert res.status_code == 200
    assert res.json()["disposition"] == "attachment"


@needs_db
def test_without_the_flag_it_stays_inline(client: Any) -> None:
    _login(client)
    res = client.get("/api/documents/docaadhaar01/url")
    assert res.status_code == 200
    assert res.json()["disposition"] == "inline"


@needs_db
def test_downloading_still_needs_the_same_permission_as_viewing(client: Any) -> None:
    # A download flag must not become a way around the access check.
    assert client.get("/api/documents/docaadhaar01/url", params={"download": "true"}).status_code == 401
