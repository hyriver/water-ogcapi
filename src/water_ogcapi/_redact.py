"""Credential redaction for URLs, shared by every type that holds one (D-04)."""

from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

__all__ = ["CREDENTIAL_PARAMS", "REDACTED", "redact_url"]

# Query parameters that carry a credential. A key in the URL reaches Response.url,
# error messages, and httpx2's INFO log, so it must travel in a header.
CREDENTIAL_PARAMS = frozenset({"api_key"})
REDACTED = "REDACTED"
_QUERY_ENTRY = re.compile(r"(?:^|(?<=[&;]))([^&;=]*)=([^&;]*)")


def _mask_entry(match: re.Match[str]) -> str:
    name = match[1]
    return f"{name}={REDACTED}" if unquote_plus(name).lower() in CREDENTIAL_PARAMS else match[0]


def redact_url(url: str) -> str:
    """Mask credential query values and drop userinfo and fragment from ``url``.

    Other query entries keep their bytes, so an opaque cursor still replays. A URL
    that needs no change is returned as given, and an unparsable one as ``REDACTED``.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    query = _QUERY_ENTRY.sub(_mask_entry, parts.query)
    netloc = parts.netloc.rpartition("@")[2]
    if (query, netloc, parts.fragment) == (parts.query, parts.netloc, ""):
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
