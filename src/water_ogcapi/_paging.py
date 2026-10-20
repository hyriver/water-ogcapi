"""Page parsing: a FeatureCollection response becomes a `Page`, and pages a `Pagination`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from water_ogcapi._metadata import json_object, parse_link
from water_ogcapi._results import Page, Pagination
from water_ogcapi.exceptions import ServiceError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from water_ogcapi._results import Check, Completeness, Link
    from water_ogcapi._transport import Response

__all__ = ["malformed_counts", "next_href", "parse_page", "summarize"]

JSON_TYPES = frozenset({"application/geo+json", "application/json"})


def _count(value: object) -> int | None:
    """Return a JSON count, or ``None`` for anything but a non-negative integer."""
    return value if type(value) is int and value >= 0 else None


def malformed_counts(body: Mapping[str, Any]) -> bool:
    """Whether ``numberMatched`` or ``numberReturned`` is present but not a usable count.

    A null reads as absent.
    """
    members = ("numberMatched", "numberReturned")
    return any(body.get(key) is not None and _count(body[key]) is None for key in members)


def _broken_next(link: object) -> bool:
    """Whether ``link`` is a ``next`` link without a string ``href``."""
    if not isinstance(link, dict):
        return False
    member = cast("dict[str, Any]", link)
    return member.get("rel") == "next" and not isinstance(member.get("href"), str)


def parse_page(
    resp: Response, params: Mapping[str, str], method: str, request_body: bytes | None
) -> Page:
    """Build a `Page` from a response whose body is a FeatureCollection.

    Raises
    ------
    ServiceError
        If the body is not a FeatureCollection with a ``features`` list, or its
        ``links`` member is not a list or holds a ``next`` link without a string
        ``href``. Either would otherwise read as the last page (D-34).
    """
    body = json_object(resp, method=method, request_body=request_body)
    features = body.get("features")
    links = body.get("links", [])
    if body.get("type") != "FeatureCollection" or not isinstance(features, list):
        msg = "response is not a FeatureCollection with a features list"
        raise ServiceError(
            msg,
            resp.url,
            status=resp.status,
            attempts=resp.attempts,
            method=method,
            request_body=request_body,
        )
    if not isinstance(links, list) or any(map(_broken_next, links)):
        msg = "response has a malformed links member or next link"
        raise ServiceError(
            msg,
            resp.url,
            status=resp.status,
            attempts=resp.attempts,
            method=method,
            request_body=request_body,
        )
    return Page(
        url=resp.url,
        params=params,
        status=resp.status,
        headers=resp.headers,
        links=tuple(filter(None, map(parse_link, links))),
        fetched_at=time.time(),
        number_matched=_count(body.get("numberMatched")),
        number_returned=_count(body.get("numberReturned")),
        feature_count=len(features),
        body=body,
        content=resp.content,
        method=method,
        request_body=request_body,
    )


def next_href(links: tuple[Link, ...]) -> str | None:
    """Return the ``next`` link to follow: the first of a JSON or unstated type, else the first."""
    nexts = [link for link in links if link.rel == "next"]
    for link in nexts:
        media_type = (link.type or "application/json").partition(";")[0].strip().lower()
        if media_type in JSON_TYPES:
            return link.href
    return nexts[0].href if nexts else None


def summarize(pages: list[Page], *, failed: bool, malformed: bool) -> Pagination:
    """Summarize a traversal: counts, the checks that passed, and completeness (D-36)."""
    reported = {p.number_matched for p in pages if p.number_matched is not None}
    matched = next(iter(reported)) if len(reported) == 1 else None
    returned = sum(p.feature_count for p in pages)
    sort_by = pages[0].params.get("sortby") if pages else None
    if failed:
        return Pagination(matched, returned, len(pages), "limited", "failure", sort_by)
    # The traversal ended on a page without a next link. A malformed count, a count that
    # disagrees with numberMatched, a page whose numberReturned differs from its features,
    # or pages that disagree with each other, leave the result unknown.
    checks: tuple[Check, ...] = ("end_of_links",)
    if matched is not None and matched == returned:
        checks += ("count",)
    short = any(p.number_returned not in (None, p.feature_count) for p in pages)
    mismatch = matched is not None and matched != returned
    completeness: Completeness = (
        "unknown" if malformed or short or len(reported) > 1 or mismatch else "complete"
    )
    return Pagination(matched, returned, len(pages), completeness, sort_by=sort_by, checks=checks)
