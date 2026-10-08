---
name: live-api
description: Use before sending any request to the live NWIS, FabricData, or GeoConnex endpoints, including recording test fixtures, writing or running network-marked tests, checking rate-limit or auth headers, and writing up API behavior findings. Covers credential handling, quota limits, and what evidence to keep.
---

# Working against the live endpoints

These are production services that USGS runs for every user. Base URLs are in
`README.md`.

## Credentials

A leaked key ends up in git history, CI logs, or a public issue, where anyone can spend
its quota. Recorded fixtures and copied URLs are the easy ways for it to reach disk.

- Read the key from the `USGS_API_KEY` environment variable. Never print it or write it
    into a file, commit, issue, or chat message.
- USGS documents two forms: an `X-Api-Key` request header and an `api_key` query
    parameter. Use the header. The query form puts the key in the request URL, and the
    server may echo query parameters into the `next` links of the response body.
- Scrub at record time, before anything is written to disk. Store no request headers.
- GeoConnex takes no key. Never send it one.
- A server can echo the key back: a redirect's `Location` has carried it as a query
    parameter. Pipe every probe's output through a filter that replaces the key's value,
    and request a base URL with its trailing slash, since the bare form redirects.

```bash
<probe command> 2>&1 | python3 -c 'import os, sys; k = os.environ["USGS_API_KEY"]; sys.stdout.writelines(l.replace(k, "REDACTED") for l in sys.stdin)'
```

- Before finishing, scan for the current key. The command fails loudly when the variable
    is unset or empty, prints file names only, and keeps the key off the command line.
    No output means the current key is not in the working tree; it cannot find old or
    encoded keys.

```bash
: "${USGS_API_KEY:?}" && command grep -rlF -f <(printenv USGS_API_KEY) . --exclude-dir=.git --exclude-dir=.pixi
```

## Quota

- Keep probes small: a low `limit`, one page unless the question is about paging.
- Probe through `Transport(max_retries=0)` so one call sends one request. The default
    retries up to three times, and the caller never sees the intermediate responses.
- Read `X-RateLimit-Remaining` on each response and stop well before zero. If a service
    sends no `X-RateLimit-*` headers, keep to a handful of requests per task.
- Never provoke a 429. Test rate-limit handling against `httpx2.MockTransport` only.
- Make network tests for FabricData skip when the backend does not respond.

## Stable targets

- NWIS: monitoring location `01646500` (Potomac River near Washington, DC), and a small
    bounding box around Washington, DC.
- GeoConnex: the 22 two-digit HUCs in `hu02`, `01` to `22`.

## Fixtures

- Record from real responses. Keep the status, the response headers, and the body. Keep
    the request URL and params with credentials removed.
- Record the request date with each fixture. The NWIS API is v1 and USGS may change it.
- If `tests/fixtures/` does not exist yet, propose a layout before creating one.
- Note which services send `X-RateLimit-*` headers. The quota logic depends on it.

## Checking that the key is honored

Send one keyed and one unkeyed request to the same endpoint and compare the
`X-RateLimit-*` headers. A higher limit on the keyed response shows the key was honored.
Equal limits or missing headers are inconclusive; record them as such.

## Writing up findings

Write each finding as a neutral, reproducible observation:

1. The request: method, URL with credentials removed, and date.
1. What the response showed: status, the relevant headers, a short body excerpt.
1. What was expected, citing the OGC API spec or the USGS docs.

Keep what you observed apart from what you infer from it. `docs/design.md` Q-07 lists
claims that are still unverified. Do not state any of them as fact until a live response
confirms it.
