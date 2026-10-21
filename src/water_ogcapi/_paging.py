"""Page parsing: a FeatureCollection response becomes a `Page`, and pages a `Pagination`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from water_ogcapi._metadata import json_object, parse_link
from water_ogcapi._results import Page, Pagination
from water_ogcapi._transport import reject_credentials
from water_ogcapi.exceptions import ServiceError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from water_ogcapi._results import Check, Completeness, Link
    from water_ogcapi._transport import Response

__all__ = ["credential_link", "malformed_counts", "next_href", "parse_page", "summarize"]

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


def _carries_credential(href: str) -> bool:
    """Whether ``href`` holds a credential in its query, fragment, or userinfo."""
    try:
        reject_credentials(href, None)
    except ValidationError:
        return True
    try:
        return "@" in urlsplit(href).netloc
    except ValueError:
        return False


def credential_link(links: object) -> bool:
    """Whether a link object in ``links`` carries a credential in its ``href``.

    The raw ``href`` is checked, fragment and userinfo included, before `Link` masks
    it (L-11).
    """
    if not isinstance(links, list):
        return False
    for link in cast("list[Any]", links):
        href = cast("dict[str, Any]", link).get("href") if isinstance(link, dict) else None
        if isinstance(href, str) and _carries_credential(href):
            return True
    return False


def _feature_links(features: list[Any]) -> list[Any]:
    """Return the ``links`` member of every feature object in ``features``."""
    return [
        cast("dict[str, Any]", feature).get("links")
        for feature in features
        if isinstance(feature, dict)
    ]


def parse_page(
    resp: Response, params: Mapping[str, str], method: str, request_body: bytes | None
) -> Page:
    """Build a `Page` from a response whose body is a FeatureCollection.

    Raises
    ------
    ServiceError
        If the body is not a FeatureCollection with a ``features`` list, or its
        ``links`` member is not a list or holds a ``next`` link without a string
        ``href``. Either would otherwise read as the last page (D-34). Also if a link
        of the document or of a feature carries a credential, since ``body`` and
        ``content`` would keep it (D-53).
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
    if credential_link(links) or any(map(credential_link, _feature_links(features))):
        msg = "response has a link that carries a credential"
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
    # A page that left numberMatched out cannot be said to agree with the others.
    every = all(p.number_matched is not None for p in pages)
    matched = next(iter(reported)) if len(reported) == 1 and every else None
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
        "unknown"
        if malformed or short or len(reported) > 1 or mismatch or (reported and not every)
        else "complete"
    )
    return Pagination(matched, returned, len(pages), completeness, sort_by=sort_by, checks=checks)
