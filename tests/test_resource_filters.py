"""_extract_filters (app/api/routes/resources.py): only an ALLOWLISTED field
name becomes a filter, and only when it's actually present and non-empty —
never an arbitrary client-supplied query param. See
app.domain.resource_search.FILTERABLE_FIELDS.
"""

from __future__ import annotations

from starlette.datastructures import QueryParams
from starlette.requests import Request

from app.api.routes.resources import _extract_filters


def _request(query_string: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/employees",
        "query_string": query_string.encode(),
        "headers": [],
    }
    return Request(scope)


def test_an_allowlisted_field_becomes_a_filter() -> None:
    req = _request("page=1&department=Engineering")
    assert _extract_filters("employees", req) == {"department": "Engineering"}


def test_a_non_allowlisted_field_is_ignored() -> None:
    req = _request("page=1&role=Manager")  # "role" isn't in employees' allowlist
    assert _extract_filters("employees", req) is None


def test_an_empty_value_is_ignored() -> None:
    req = _request("page=1&department=")
    assert _extract_filters("employees", req) is None


def test_a_resource_with_no_filterable_fields_returns_none() -> None:
    req = _request("page=1&status=Open")
    assert _extract_filters("jobs", req) is None


def test_query_params_directly_confirm_the_allowlist_gate() -> None:
    # Same check via Starlette's own QueryParams, in case Request wiring changes.
    params = QueryParams("department=Engineering&injectedField=whatever")
    assert dict(params) == {"department": "Engineering", "injectedField": "whatever"}
    # _extract_filters must still only pick "department" for employees.
    req = _request(str(params))
    assert _extract_filters("employees", req) == {"department": "Engineering"}
