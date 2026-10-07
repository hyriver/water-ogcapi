# Design decisions

This page records the design decisions behind `water-ogcapi`, the mistakes that shaped
them, and the questions still open. Contributors and coding agents check it before
proposing a design change and update it in the same change that makes or reverses a
decision.

## Maintaining this page

- Search here before deciding. If a decision (`D-`) or lesson (`L-`) covers the case,
    follow it, or argue against it with evidence it does not already account for.
- A new decision gets a `D-` entry with its date. A mistake worth not repeating gets an
    `L-` entry that names the check catching it.
- To reverse a decision, set the old entry's status to `superseded by D-NN` and keep its
    text. The record of what was tried is the point of this page.
- An answered question (`Q-`) becomes a `D-` entry, and the question's status becomes
    `answered by D-NN`.
- Some decisions have a one-line summary under "Design rules" in `AGENTS.md`. Update
    that line in the same change.
- Never renumber. New entries take the next free number.
- The entries up to D-15, L-08, and Q-08 predate this page (August and September 2026)
    and carry no individual dates.

Each decision lists its status, the decision, why, what was rejected, and its limits.

## Scope

`water-ogcapi` is a lean, async-first client for the USGS OGC API Features endpoints:
NWIS, FabricData, and GeoConnex (base URLs in `README.md`). It complements
`dataretrieval` and offers a small runtime dependency footprint with no pandas, native
async, raw GeoJSON with the evidence of how it was fetched, GeoConnex and FabricData
coverage, and metadata caching.

Planned surface: bbox queries, CQL2 spatial predicates, CQL2-JSON and CQL2-Text
passthrough, identifier filters, direct item fetch, a metadata cache (D-07), typed
exceptions (validation errors list the valid options), a quota governor (D-06), and
page-level checkpoint and resume so a retry continues where it failed.

Layering rule for anything new:

- Protocol truth belongs in the client: bodies, request params, headers, links, CRS
    assertions, pagination state.
- Lossless normalization may belong in the client. An aggregated view is fine if nothing
    is coerced.
- Target-specific interpretation belongs downstream: column typing, datetime coercion,
    indexes, units, geometry objects, STAC granularity, chunk layout.
- Dependency weight decides packaging (core, extra, or companion package). It does not
    decide which layer owns a feature.

## Decisions

### D-01: Own async transport on httpx2

- **Status:** accepted.
- **Decision:** The client owns its async request loop (client, semaphore, retry with
    backoff, per-response header capture) on `httpx2`, Pydantic's maintained fork of
    httpx. `src/water_ogcapi/_transport.py` is the only module that imports it, so
    swapping the HTTP library is a one-file edit.
- **Why:** Both USGS hosts negotiate HTTP/2, and aiohttp has no HTTP/2 support.
- **Rejected:** aiohttp, for the reason above. Reusing `tiny-retriever`: every public
    entry point is synchronous and runs coroutines on a background thread, so calling it
    from `iter_pages()` blocks the caller's event loop, and its batch API has no
    per-response hook for the quota governor (D-06). An early plan offered "pin
    `tiny-retriever` or inline it"; the pin option never existed, which only reading its
    source showed.
- **Limits:** `h2` is listed as its own runtime dependency because conda-forge packages
    have no extras, so `httpx2[http2]` cannot be expressed there.

### D-02: `iter_pages()` is the primitive

- **Status:** accepted.
- **Decision:** `iter_pages()` is the core async generator. `collect()` is a convenience
    built on it. The sync wrapper (D-08) wraps the async core and duplicates none of it.
- **Why:** An all-pages tuple plus an aggregate dict doubles peak memory.
- **Rejected:** `collect()` as the primitive.

### D-03: Query methods return payload plus evidence

- **Status:** accepted. D-22 and D-23 settle Q-03 and Q-02; where the sketch below
    differs from them, they win.

- **Decision:** `iter_pages()` yields frozen `Page` objects and `collect()` returns a
    frozen `QueryResult`:

    ```python
    @dataclass(frozen=True, slots=True)
    class Page:
        url: str
        params: Mapping[str, str]  # redacted
        status: int
        headers: Mapping[str, str]  # redacted allowlist
        links: tuple[...]
        fetched_at: float
        body: dict | None  # only with keep_pages=True


    @dataclass(frozen=True, slots=True)
    class QueryResult:
        geojson: dict  # aggregated payload, nothing coerced
        pages: tuple[Page, ...]
        pagination: ...  # number_matched, number_returned, page_count, sort_by, completeness
        crs: ...  # requested, per-page Content-Crs, effective, how determined
    ```

- **Why:** Adding the wrapper now is cheap, and replacing an established dict return
    type later breaks every consumer. That asymmetry is the main argument for it.

- **What it gives:** postmortem material, the links and CRS provenance a caller needs,
    and an explicit `completeness` of `complete`, `limited`, or `unknown`.

- **Limits:** It does not prove completeness. Page bodies, timestamps, links, and
    headers cannot reveal that one unseen feature vanished and another appeared between
    pages, absent a server snapshot token or an independent list of expected
    identifiers. `completeness` can still report a plausible but wrong answer. Detection
    belongs at fetch time, in the count and identifier checks of D-05.

- **Rejected:** A fuller first version, cut after review. `schema`, `queryables`, and
    `collection` on every result: they are cached on the client already, and a TTL cache
    may predate the query, which weakens the provenance claim. Page bodies kept by
    default: that doubles memory (D-02), so bodies are opt-in with `keep_pages=True`. A
    separate `provenance` type: folded into `pagination` and `Page`.

### D-04: Credentials are redacted at construction

- **Status:** accepted.
- **Decision:** Any type that carries request headers or params (`Page`, `QueryResult`,
    exceptions) redacts credentials in its constructor, keeping headers from an
    allowlist. Callers never have to remember.
- **Why:** Otherwise every pickled, logged, or notebook-displayed result leaks the key.
- **Rejected:** A header denylist, which fails open on any header nobody listed.
- **Limits:** Header redaction covers the key only while it travels in a header. With
    the `api_key` query-parameter form, the key lands verbatim in `Response.url` and in
    every `ServiceError` message, so D-18 rejects that form.

### D-05: Follow `next` links; offset fan-out only where verified

- **Status:** accepted. D-26 names the services that allow fan-out.
- **Decision:** Pagination follows `next` links by default. Concurrent offset fan-out is
    enabled per service only after that service's result ordering is verified, gated by
    its capability profile (D-13), with count and identifier checks and a sequential
    fallback. Concurrency defaults to a conservative value.
- **Why:** OGC API Features Core describes paging through `next` links. It permits
    `numberMatched` to be absent and does not standardize `offset`. See L-01.
- **Limits:** Core does not require `next` links (no SHALL), so a missing `next` link
    alone does not prove the last page was reached. The transport's backoff has no
    jitter, so concurrent retries stay in lockstep. Add jitter if fan-out ever retries
    in bulk.

### D-06: Quota governor per service

- **Status:** accepted; the transport part exists, the governor does not.
- **Decision:** Where rate-limit headers exist, read `X-RateLimit-Remaining` on every
    response and slow down before it runs out. Handle `Retry-After` when present and
    never assume it. Services without the headers get a conservative static default.
- **Why:** Reacting after a 429 is too late. `Retry-After` is not documented for this
    API. GeoConnex is a non-USGS host with no USGS gateway, so it likely sends no
    `X-RateLimit-*` headers.
- **Rejected:** Assuming the headers exist on every service.
- **Current state:** The transport retries 429, 500, 502, 503, and 504 (`RETRY_STATUS`)
    with exponential backoff and honors `Retry-After` in both RFC 7231 forms, measuring
    an HTTP-date against the response's `Date` header so client clock skew cannot shift
    the wait. Other statuses raise at once. A `Retry-After` above 60 seconds
    (`MAX_SLEEP`) is raised to the caller, as `RateLimitError` on a 429 and
    `ServiceError` otherwise, since retrying earlier than asked burns the remaining
    attempts against a window that has not reopened.

### D-07: In-memory, success-only metadata cache

- **Status:** accepted; not yet written.
- **Decision:** Per-URL TTL. Writes only on 2xx. Revalidation with `ETag` and
    `Last-Modified`. Expired entries are kept so they can revalidate, and a 304 keeps
    the original payload. `refresh()` clears the cache and the collection index. No
    SQLite and no disk.
- **Why:** Credentials travel in headers, so cache keys never contain them. The
    transport returns a 304 as a response so revalidation works.

### D-08: Sync wrapper on a background event-loop thread

- **Status:** accepted; verified by a probe script, not yet written.
- **Decision:** The sync API drives the async core from one daemon thread running
    `loop.run_forever()`, through
    `asyncio.run_coroutine_threadsafe(coro, loop).result()`. Async generators such as
    `iter_pages()` are pumped one `__anext__()` at a time until `StopAsyncIteration`,
    because `run_coroutine_threadsafe` drives only coroutines. One `Transport` bound to
    the background loop serves every sync call, so connection reuse and HTTP/2 survive
    across calls.
- **Why:** The coroutine runs on another thread's loop, so it works when the caller
    already has a running loop, as in Jupyter.
- **Rejected:** `asyncio.run()`, which raises inside a running loop. `nest_asyncio`,
    which monkeypatches the loop.
- **Relation to D-01:** D-01 rejected `tiny-retriever` because it forces every caller
    through a thread hop. Here only the sync shim uses the hop, and async callers reach
    the core directly.

### D-09: No heavy dependencies on the core import path

- **Status:** accepted.
- **Decision:** No pandas, geopandas, xarray, Arrow, shapely, or STAC imports on the
    core path.
- **Open:** how frame conversion ships (Q-01).

### D-10: Zero HTTP at construction

- **Status:** accepted.
- **Decision:** Constructing a client or transport performs no I/O. `Transport` creates
    its HTTP client and semaphore on the first request.
- **Limits:** The transport then belongs to the event loop of that first request, and a
    request from another loop raises `RuntimeError`. Use one transport per loop, or call
    `aclose()` on the owning loop before that loop exits; closing pooled connections
    from another loop fails. The client and semaphore bind together, since binding the
    semaphore later would make the same failure depend on load.

### D-11: Polygon queries send a bbox

- **Status:** accepted.
- **Decision:** A polygon query extracts a bbox by walking the coordinate arrays in pure
    Python. The docs state that the bbox is an approximation and that exact containment
    is the caller's post-filter.
- **Why:** Keeps shapely off the core path (D-09).

### D-12: Redirects are not followed

- **Status:** accepted.
- **Decision:** `follow_redirects=False`. A 3xx other than 304 raises `ServiceError`
    naming the `Location` by scheme, host, and path only (L-11). A 304 is returned as a
    response, since D-07 revalidation needs it. A response hook raises on a redirect
    before httpx2 parses its `Location`: httpx2 builds the follow-up request even with
    redirects off, and a malformed `Location` would otherwise raise a retryable
    `RemoteProtocolError`.
- **Why:** httpx2 strips `Authorization` across hosts but not a custom key header, so
    following a redirect would send the key to another host. Returning the bodyless
    redirect as a success would surface later as a JSON decode error.

### D-13: Per-service capability profiles

- **Status:** accepted; values unverified (Q-07). D-26 replaces the GeoConnex sort key.
- **Decision:** Each service class declares a profile: auth header convention, page-size
    caps, sort key, whether offset fan-out is verified (D-05), and whether rate-limit
    headers exist (D-06).
- **Planned values:** NWIS reads the key from `USGS_API_KEY`, with 10,000 features per
    page by default and 50,000 at most. FabricData uses the NWIS key convention with a
    1,000-per-page cap and a tunable timeout and retry count. GeoConnex takes no key.
    GeoConnex collections have no `x-ogc-role: id` property, so its sort key is fixed to
    `uri`, which also removes per-query schema fetches for that service.
- **Rule:** The USGS key never goes to GeoConnex; a non-USGS host receiving a USGS
    credential is a leak. Do not hard-code the NWIS auth header form until a live
    response confirms it (L-03).

### D-14: Testing strategy

- **Status:** accepted.

- **Decision:** Offline tests intercept HTTP with `httpx2.MockTransport`, and an
    unexpected call fails the test. Live tests carry `@pytest.mark.network`, stay out of
    default CI, and CI never requires them to pass. 429 recovery is tested against mocks
    only; never induce rate limiting against the production API.

- **Required offline scenarios:**

    - construction under a mock with no registered handlers, proving zero-HTTP init;
    - one `/collections` handler accessed twice, the second access served from cache;
    - the ETag flow: clock advanced past the TTL, the second request carries
        `If-None-Match`, and a 304 keeps the original payload;
    - partial results: the first page implies more, later pages fail, and the error
        carries the fetched pages and the failure points;
    - a 429 with `Retry-After` and zero remaining, parsed values matching exactly;
    - the GeoConnex sort attribute returning `uri` with zero HTTP calls;
    - redaction: no credential in any `Page`, `QueryResult`, or exception repr;
    - fault injection against fixtures: truncated pages, absent `numberMatched`, a
        collection that changes mid-pagination.

- **Live targets:** NWIS monitoring location `01646500` (Potomac River near Washington,
    DC), a small bounding box around Washington, DC, and the 21 two-digit HUCs from
    GeoConnex. FabricData tests skip when the backend does not respond.

- **Planned benchmarks:** cold start to first feature, round trips for a single-page
    result, requests spent per 100,000 features, peak memory against result size, and
    recovery after a 429 mid-pagination (mock only).

- **Limits:** Fixtures stay green while upstream drifts, live tests stay out of CI, and
    FabricData failures turn into skips, so upstream drift is invisible by construction
    (Q-05).

### D-15: Clean build, salvaging per file

- **Status:** accepted.
- **Decision:** This repository was built fresh. An earlier prototype served as a
    reference: its cache, exceptions, and tooling config were worth porting, and its
    client and aiohttp-based test suite were dropped with the move to httpx2 (D-01).
- **Why:** The prototype's client had a synchronous public API under an async-first
    claim, and the defects recorded in L-01 to L-05.
- **Rejected:** Committing the prototype as a baseline and extending it.

### D-16: Documentation site on Zensical

- **Status:** accepted.
- **Date:** 2026-09-30.
- **Decision:** The site builds with Zensical from `zensical.toml` in the pixi `docs`
    environment. `pixi r docs` runs a strict build, and `.github/workflows/docs.yml`
    runs it on every PR and deploys `main` to GitHub Pages at
    `https://docs.hyriver.io/water-ogcapi/`. `docs/index.md` only includes `README.md`
    through `pymdownx.snippets`, so the home page and the README cannot diverge.
- **Why:** Chosen by the maintainer. The strict build fails on warnings, so a broken
    internal link or a missing snippet fails the PR. External URLs are not checked.
- **Limits:** Zensical was at 0.0.66 when adopted, so config keys may change between
    releases. Pages deploys only after the repository's Pages source is set to GitHub
    Actions. The workflow keeps no Zensical build cache, following Zensical's advice for
    CI.

### D-17: Docs pages come from their sources

- **Status:** accepted.
- **Date:** 2026-09-30.
- **Decision:** A docs page whose text has a canonical source elsewhere includes that
    source: the home page includes `README.md`, the contributing page `AGENTS.md`, and
    the license page `LICENSE`. mkdocstrings generates the API reference from the
    NumPy-style docstrings in `src/`. Example notebooks live in `docs/examples/`, each
    paired with a jupytext `py:percent` script for review and committed with its
    outputs. `pixi r nb-run` executes them locally, and `scripts/convert_notebooks.py`
    converts them to Markdown pages beside them before each build without running them.
    `zensical.toml` has no `nav` key, so the nav mirrors `docs/`. The Examples page
    shows one card per notebook, in a Material `grid cards` block, with a thumbnail
    committed in `docs/examples/images/`.
- **Why:** Each text has one source, so the published site matches the repository.
    Zensical renders no notebooks and runs no MkDocs hooks, so conversion is a pixi task
    that `docs` and `docs-serve` depend on. Running notebooks in the build would call
    the live APIs from CI (D-14). A page missing from an explicit nav still builds with
    no warning, even in strict mode, so an explicit nav would hide a new notebook
    silently.
- **Rejected:** An explicit `nav`, and running notebooks during the build, for the
    reasons above.
- **Limits:** The generated nav sorts pages alphabetically with folders last, so
    Examples follows License. The strict build passed with a relative link to `LICENSE`
    in the README, which has no target on the site, so links in included files must be
    absolute URLs. Zensical's mkdocstrings support is preliminary and has no backlinks.
    A deleted notebook's generated page stays in a local `docs/examples/` until removed
    by hand. Cards are kept by hand: the strict build fails on a card whose notebook is
    missing, and nothing flags a notebook without a card.

### D-18: The API key travels only in a header

- **Status:** accepted.
- **Date:** 2026-09-30.
- **Decision:** The key goes in the `X-Api-Key` request header. `Transport.get` raises
    `ValueError` for an `api_key` query parameter, in any letter case, whether it comes
    in `params` or already sits in the URL, before sending anything. It reads query
    names percent-decoded and split on both `&` and `;`, since some servers accept
    either separator.
- **Why:** A query parameter puts the key in `Response.url`, in `ServiceError` messages,
    in a redirect's echoed `Location`, and in the URL httpx2 logs at INFO for every
    request. A header stays out of all four; L-09 covers the error chain.
- **Rejected:** Redacting URLs wherever they appear, which has to find every copy,
    including log records the library does not own.
- **Limits:** Whether NWIS honors the header form is unverified (Q-07). If it honors
    only the query form, this decision needs revisiting.

### D-19: `timeout` bounds each attempt end to end

- **Status:** accepted.
- **Date:** 2026-09-30.
- **Decision:** `Transport(timeout=...)` caps each attempt, from sending the request to
    reading the last body byte, with `asyncio.timeout`. httpx2 applies the same value to
    each connect, read, write, and pool wait. A timed-out attempt is transient and
    retried. Waiting for a concurrency permit is not bounded; a caller that needs a
    deadline wraps the call.
- **Why:** httpx2 times each operation, so a server sending a body chunk every 29 s
    under `timeout=30` held a permit indefinitely.
- **Limits:** One call can take up to `(max_retries + 1) * timeout` plus the backoff
    waits.

### D-20: Package logger isolated from the root logger

- **Status:** accepted.
- **Date:** 2026-10-05.
- **Decision:** `water_ogcapi._logging` defines the `water_ogcapi` logger at DEBUG with
    `propagate = False` and a stderr handler at WARNING. `configure_logger()`, exported
    from the package, replaces that setup on each call: an argument left out returns to
    its default. It writes files as UTF-8, builds the new handlers before removing the
    old ones so a rejected call changes nothing, and touches only the handlers it named.
    A lock serializes calls, and a retired handler that fails to close does not stop the
    swap. The transport logs a retry at INFO, a 429 retry at WARNING, and a successful
    request at DEBUG. No record carries request headers (Q-08).
- **Why:** Isolation was chosen by the maintainer: a package that configures the root
    logger neither captures nor floods these records, and Jupyter shows each record
    once. Every call is declarative because the template mixed the two: a call that only
    raised verbosity turned file logging off while the console level carried over. Under
    a locale that cannot encode a record, such as cp1252 on Windows before Python 3.15,
    logging drops the record and prints a traceback, hence UTF-8.
- **Rejected:** loguru and structlog, which add runtime dependencies (D-09) and route
    records through their own pipelines. No handler plus propagation, the library
    default in the Python logging HOWTO, for the isolation reason above. Arguments that
    keep their value between calls, which need a sentinel to turn file logging off.
- **Limits:** Root handlers never see these records, so AWS Lambda's handler, pytest's
    `caplog`, and an application's own logging config miss them unless the caller
    attaches a handler to `logging.getLogger("water_ogcapi")`. The logger stays at
    DEBUG, so a DEBUG call builds a record even when no handler takes it. Handlers write
    on the event loop's thread; move file output behind a `QueueHandler` if logging ever
    stalls offset fan-out. A record logged on another thread while `configure_logger()`
    swaps handlers can reach the retired file handler, which reopens its file in append
    mode and leaks that handle.
- **Check:** `test_logs_name_each_retry_and_keep_the_key_out` and
    `tests/test_logging.py`.

### D-21: Versioning and deprecation policy

- **Status:** accepted.
- **Date:** 2026-10-07.
- **Decision:** Versions are `MAJOR.MINOR.PATCH`, read from git tags by hatch-vcs. The
    public API is every name in `water_ogcapi.__all__` and in the `__all__` of a public
    module such as `water_ogcapi.exceptions`, with the parameters and return types their
    docstrings document; `_`-prefixed modules are private. While the major version is 0,
    a minor release may break the public API and a patch release never does. A breaking
    change is committed with `!` after its type (`feat!:`), which git-cliff marks
    `[**breaking**]` in the changelog. A public name slated for removal raises a
    `DeprecationWarning` from the caller's line for at least one minor release before it
    goes. The package stays on 0.x until USGS serves the NWIS API under a path other
    than `/ogcapi/v0`. After 1.0, a breaking change needs a new major version.
- **Why:** USGS documents v0 as still under development, with an interface that may
    change. A 1.0 that promised a stable API over that would need a 2.0 at the first
    upstream change that reaches the service classes. Callers on 0.x pin the minor
    version, which keeps patch fixes flowing to them.
- **Rejected:** Shipping 1.0 once the library's own types stop changing, for the reason
    above. CalVer, which hides whether a release breaks anything.
- **Limits:** USGS may keep v0 through the whole support period, so 0.x may be the only
    series. A FabricData or GeoConnex change can still force a minor bump while NWIS is
    stable.

### D-22: Which operation returns which type

- **Status:** accepted.

- **Date:** 2026-10-07.

- **Decision:** A `Page` is one response document, and a `QueryResult` is an aggregate.

    | Operation      | Returns       | `Page.body`              | `Page.content`                    |
    | -------------- | ------------- | ------------------------ | --------------------------------- |
    | `iter_pages()` | yields `Page` | the decoded document     | the response bytes                |
    | item fetch     | `Page`        | the decoded Feature      | the response bytes                |
    | `collect()`    | `QueryResult` | `None` on each kept page | bytes only with `keep_pages=True` |

    Every `Page` carries, whatever its body: `url` (the request as sent, query included),
    `params` (redacted), `status`, `headers` (the redacted allowlist, `Content-Crs`
    included), `links`, `fetched_at`, `number_matched` and `number_returned` as the
    server reported them (`None` when absent), and `feature_count`, the features
    received. A body is a Feature or a FeatureCollection, told apart by its `type`, and
    nothing promises a `features` member.

    Only `QueryResult.pagination` carries `page_count` and `completeness`, and it is never
    `None`. Its counts describe what was fetched, so filtering `geojson` afterward
    changes none of them. `number_matched` is the value every reporting page agreed on;
    pages that disagree set it to `None` and `completeness` to `unknown`. `completeness`
    reads `complete` when traversal reached the end and every check passed, `limited`
    when it stopped early on a caller limit or a failure, with the stop reason recorded
    beside it, and `unknown` when it ended without evidence either way. An early stop
    wins: a result that stopped early reads `limited` even when its pages disagreed, and
    `number_matched` of `None` still records the disagreement.

- **Why:** `iter_pages()` cannot know the final page count while streaming, and an item
    fetch has no pagination. Returning a `Page` for an item reuses its evidence, CRS
    headers included, without inventing pagination. Per-page counts live on `Page` so
    they survive when `collect()` drops bodies.

- **Rejected:** A `QueryResult` with `pagination=None` for an item fetch, which makes
    every `collect()` caller narrow an optional and invites treating a Feature as a
    one-feature collection. A separate item type, whose evidence fields would drift from
    those of `Page`. Taking the first or last page's `numberMatched`, which hides a
    collection that changed mid-traversal.

- **Limits:** `complete` still cannot prove that no feature was swapped between pages
    (D-03).

### D-23: Kept pages hold bytes, so no page shares objects with the aggregate

- **Status:** accepted.
- **Date:** 2026-10-07.
- **Decision:** `collect(keep_pages=True)` keeps each page's response bytes in
    `Page.content` and sets `Page.body` to `None` (D-22). `Page.json()` decodes
    `content` into a fresh dict on each call and caches nothing, so no kept page shares
    an object with `QueryResult.geojson`. The bytes are the response body after HTTP
    content decoding (gzip removed) and before JSON decoding. `params`, `headers`, and
    `links` are immutable copies; `body` is the one mutable field, and the caller owns
    it. `repr` shows neither `body` nor `content`.
- **Why:** Editing the aggregate in place, as a notebook user normalizing properties
    would, must not rewrite the page evidence. On a synthetic 10,000-feature page of 3.0
    MB of JSON, the decoded dict took 11.7 MB (tracemalloc), so keeping bytes costs
    about a quarter of keeping a second decoded copy.
- **Rejected:** Documenting the aliasing. A `copy.deepcopy` (26 ms on that page) or a
    second decode (14 ms) at retention, each of which holds a second decoded tree for
    the life of the result. A cached decoded property, which brings that tree back.
- **Limits:** Kept bytes still grow with every page, so memory-capped callers stream
    with `iter_pages()`. Each `json()` call decodes again. The decode that builds the
    aggregate and `json()` must apply the same decoder, which an optional orjson (Q-09)
    has to respect. Real NWIS page sizes are unmeasured.

### D-24: A failed page is reported by the request that failed

- **Status:** accepted.

- **Date:** 2026-10-07.

- **Decision:** A failure point is the failed request's URL as sent, query included.
    Under fan-out the offset sits in that query, so it needs no field of its own. The
    transport raises every `ServiceError` with that URL, which a timeout or connection
    failure does not do yet (#7 changes it).

    `iter_pages()` raises `PaginationError` when one or more page requests fail. Its
    `failures` holds one `ServiceError` per failed request, each with its status,
    `attempts`, and `retry_after`, plus `remaining` on a `RateLimitError`. It holds no
    page, since the caller already has every page that was yielded. Under `next`-link
    traversal it holds one failure, and the pages after it are undiscovered: neither
    fetched nor failed. Under fan-out (#14) it is raised after every page that succeeded
    has been yielded, so a middle failure leaves the later successes with the caller
    (L-02).

    `collect()` raises `PartialResultsError`, a `PaginationError` subclass, when at least
    one page succeeded. Its `result` is a `QueryResult` of every page that succeeded,
    with `completeness` `limited` and a failure stop reason (D-22). When no page
    succeeded, `collect()` raises the `PaginationError`.

    Under `next`-link traversal, each page's `next` link is the resume point after that
    page, so a caller can checkpoint after every page without waiting for an exception.
    A page without one ends traversal (D-05), so it leaves nothing to resume. Under
    fan-out, the resume point is the set of failed and unsent requests, which #14
    defines. Page-level resume (#21) builds on both.

- **Why:** Offsets exist only under fan-out (D-05), and a URL is the failure point both
    modes share. A timeout or connection failure currently carries the URL the transport
    was called with, without the merged `params`, so it cannot replay a first-page
    request. A serverless function can be stopped without an exception, so the resume
    point has to exist after each page.

- **Rejected:** Offsets with a fan-out-only error, which gives sequential traversal
    weaker recovery. `iter_pages()` raising `PartialResultsError` with the fetched
    pages, which forces it to keep what it streamed (D-02).

- **Limits:** A resume replays against a collection that may have changed, and a cursor
    in a `next` link may expire.

### D-25: Frame conversion ships as an optional extra

- **Status:** accepted.
- **Date:** 2026-10-07.
- **Decision:** A `water-ogcapi[pandas]` extra adds a helper that returns a GeoDataFrame
    when any feature has a non-null geometry and a DataFrame otherwise. The core never
    imports pandas or geopandas (D-09), so the helper imports them when called.
- **Why:** Chosen by the maintainer. The core stays small, and a frame is one call away
    for callers who want one.
- **Rejected:** A companion package, which splits releases and docs for one helper.
    Return types that change with the installed environment (Q-01).
- **Limits:** conda-forge packages have no extras (D-01), so a conda user installs
    pandas and geopandas alongside the package.

### D-26: Two pagination strategies, picked per service

- **Status:** accepted.

- **Date:** 2026-10-07.

- **Decision:** `iter_pages()` takes a pagination strategy, and each service profile
    (D-13) names its default and the strategies it allows.

    - `next` follows the server's `next` links one page at a time (D-05). NWIS links carry
        an opaque `cursor`; FabricData and GeoConnex links carry `offset`. Every service
        allows it.
    - `sorted-offset` sorts by the collection's `x-ogc-role: id` property, read from
        `/queryables`, and requests pages by `offset`, so they can be fetched concurrently
        with D-05's count and identifier checks. `numberMatched` sets the page count.
        FabricData and GeoConnex allow it.

    NWIS allows only `next`. Where a profile allows both, the caller picks one per query,
    so the benchmarks (#24) can compare them. The `x-ogc-role: id` property replaces
    D-13's fixed `uri` sort key for GeoConnex.

    `sorted-offset` runs on a collection only when its `/queryables` names an
    `x-ogc-role: id` property and the first page reports `numberMatched`. Otherwise the
    query falls back to `next` before yielding anything. Offsets step by `limit`, kept
    at or below the profile's page cap (D-13). A page shorter than `limit` before the
    last one, a `numberMatched` that differs from the first page's, or an identifier
    seen twice stops the traversal with a `PaginationError` (D-24). Nothing falls back
    once a page has been yielded, since a streamed page cannot be taken back (D-02).

- **Why:** On NWIS, `sortby` returns only the first page: following its `offset` link
    returns 400. USGS confirmed on 2026-10-07 that this is intended, and that callers
    who need more than 50,000 sorted features sort them locally. The USGS docs guarantee
    no result order, so NWIS offset pages without `sortby` can overlap or skip. On
    FabricData `gagesii` (two pages, sorted by `staid`) and GeoConnex `gages` (three
    pages, sorted by `id`), the offset pages kept their `sortby` order across pages, the
    same on two runs. Keeping both strategies was the maintainer's choice, so they can
    be measured against each other.

- **Rejected:** `sortby` to page NWIS, which the server rejects after the first page.
    Offset fan-out on NWIS without `sortby`, since nothing fixes the order. A fixed
    `uri` sort key for GeoConnex: `gages` declares `x-ogc-role: id` on `id`.

- **Limits:** `sortby` comes from a draft OGC sorting extension outside Features Core,
    so a server can drop it in a release it counts as non-breaking. Each host was
    checked on one collection. The `id` role names the feature identifier, so its values
    should be unique and non-null; only the identifier check catches a collection where
    they are not. Whether a `sorted-offset` page costs the server more than a `next`
    page is unmeasured; #24 measures it.

### D-28: The roadmap page comes from the GitHub milestones

- **Status:** accepted.

- **Date:** 2026-10-07.

- **Decision:** `scripts/roadmap.py` writes `docs/roadmap.md` before each docs build
    from the repository's GitHub milestones and their issues: one section per milestone
    with a `<progress>` bar, the open issues listed, and the closed ones folded into
    `<details>`. Pull requests are left out. `docs.yml` also runs daily, so the deployed
    page follows issue changes without a commit. `README.md` links to the page and keeps
    no checklist of its own.

- **Why:** The milestones already track the work, so the page follows D-17 with one
    source per text. The build fails when GitHub is unreachable, by the maintainer's
    choice, so a deployed page never shows a stub.

- **Rejected:** A committed page that a scheduled job rewrites between marker comments,
    as pysentry.com does: the ruleset blocks pushes to `main`, so every refresh would
    need a PR. Fetching from the browser at page load, which needs JavaScript and spends
    each visitor's anonymous GitHub quota of 60 requests an hour.

- **Limits:** GitHub's milestone counters include pull requests, so its counts can run
    higher than the page's. A local build past 60 requests an hour needs a
    `GITHUB_TOKEN`.

## Lessons

### L-01: Offset pagination silently skips or truncates

- **What happened:** The prototype returned only the first page when `numberMatched` was
    absent, and stepped offsets by the requested page size, so a server that returned
    fewer records than requested made it skip records without any error.
- **Rule:** Follow `next` links (D-05). Where offsets are used, step by the count
    actually returned and treat an absent `numberMatched` as unknown.
- **Check:** fixtures where the server returns fewer features than requested, and where
    `numberMatched` is missing.

### L-02: A mixed-success batch misreported its own evidence

- **What happened:** A batch loop stopped at the first failing page, then reported that
    page and every later one as failed. If page 2 of 5 failed, pages 2 to 5 were all
    reported failed, and the successful responses for 3 to 5 were discarded unread. The
    only test covered a single trailing failure, so it could not catch this.
- **Rule:** A partial-results error reports exactly which pages succeeded and which
    failed. Evidence accuracy is a correctness guarantee (D-03).
- **Check:** a test where a middle page fails and later pages succeed.

### L-03: A test that restates the code proves nothing

- **What happened:** The prototype hard-coded the NWIS auth header name, and its test
    asserted the constant equaled itself. If the server ignores an unrecognized header,
    requests still succeed but count against the unkeyed quota, so throttling arrives
    far earlier than expected and no test notices.
- **Rule:** Test server-facing conventions against recorded live responses.
- **Check:** Compare the rate-limit headers of a keyed and an unkeyed request. A higher
    limit on the keyed response shows the key was honored. Equal limits or missing
    headers are inconclusive.

### L-04: Merging and parsing dropped protocol truth

- **What happened:** Merging pages dropped per-page links, timestamps, headers, and
    status, which made a wrong result impossible to audit. Parsing a collection dropped
    its CRS, storage CRS, links, temporal extent, and extra bounding boxes.
- **Rule:** Keep protocol truth in the client (Scope) and parse metadata losslessly.

### L-05: Dead surface and stale claims

- **What happened:** Two exception types were defined and exported but never raised. A
    cache docstring claimed the transport did not surface response headers while the
    client already read `ETag` and `Last-Modified` from them.
- **Rule:** Wire a public name in or delete it. When changing what a module exposes,
    grep the docstrings that describe it.
- **Check:** Grep for raise sites before exporting an exception. vulture at the lint
    threshold does not flag an unused class.

### L-06: httpx2 exception hierarchy traps

- `DecodingError` is a `RequestError` and not a `TransportError`, and `InvalidURL`
    subclasses `Exception` directly. Catching `TransportError` alone lets both escape
    untyped.
- `UnsupportedProtocol` and `LocalProtocolError` are `TransportError` subclasses, so a
    permanently bad URL is retried unless excluded (`FATAL_ERRORS` in `_transport.py`).
- **Check:** `test_only_connection_failures_are_retried` and
    `test_undecodable_body_is_typed_and_not_retried`.

### L-07: httpx2 request, header, and response quirks

- `Headers.items()` comma-joins repeated headers.
- Header values decode as ISO-8859-1, so `"²".isdigit()` is `True` while `int("²")`
    raises. Use `isdecimal()` for header integers.
- `httpx2.Response(...)` decodes its body in `__init__`, so a corrupt-body fixture must
    be built inside the mock handler.
- An HTTP-date in `Retry-After` without a zone is read as UTC. Reading it as local time
    puts the wait out by the UTC offset.
- `params=` replaces the URL's whole query whenever it is not `None`, even `{}`, so a
    `next` link passed with params would lose its paging state. `URL.copy_merge_params`
    keeps the entries but re-encodes them, turning `%FF` in a cursor into `%EF%BF%BD`
    and `x=1;y=2` into `x=1%3By%3D2`. `Transport.get` appends params to the raw query.
- **Check:** `test_digit_shaped_remaining_does_not_crash`,
    `test_parse_retry_after_zoneless_date_is_read_as_utc`, and
    `test_params_merge_into_the_url_query`.

### L-08: Connection limits do not bound HTTP/2 concurrency

- One HTTP/2 connection multiplexes many streams, so `httpx2.Limits(max_connections=)`
    does not cap requests in flight. `Transport` uses an explicit semaphore, held only
    while a request is in flight and never across a backoff wait.
- **Check:** `test_concurrency_is_bounded` and `test_backoff_does_not_hold_a_permit`.

### L-09: Redaction held for the error and failed one attribute away

- **What happened:** A pre-public review found `ServiceError` messages clean while
    `error.__cause__.request.headers` held the API key verbatim, since httpx2 redacts
    only `Authorization` and `Proxy-Authorization`. h11 also quotes an illegal header
    value in `LocalProtocolError`, so a key read with a trailing newline reached the
    message the transport copied.
- **Rule:** A transport error chains nothing from the HTTP library (neither `__cause__`
    nor `__context__`), and its message names the failure without request data.
- **Check:** `test_transport_errors_keep_the_key_out`.

### L-10: A language's number parser is wider than the header grammar

- **What happened:** `Retry-After` went through `float()`, which also accepts `nan`
    (read as a zero wait, so every retry fired at once), `inf` (a wait no caller
    survives), `1e400`, `+5`, and `7_000`. A 5,000-digit `X-RateLimit-Remaining` passed
    `isdecimal()` and then broke `int()` with an untyped `ValueError`.
- **Rule:** Parse header numbers against the RFC grammar (ASCII `1*DIGIT`), bound their
    length before converting, and cap delta-seconds at 2\*\*31 as RFC 9111 section 1.2.2
    directs.
- **Check:** `test_parse_retry_after_scalars`, `test_parse_count`, and
    `test_huge_remaining_count_stays_typed`.

### L-11: A redirect target carried the key

- **What happened:** A server answered a request that sent the key in `X-Api-Key` with a
    redirect whose `Location` held the key as a query parameter. The transport copied
    that `Location` into the `ServiceError` message, and raising inside the `except`
    block kept the raw `Location` in `__context__`.
- **Rule:** An error names a redirect target by scheme, host, and path only, and is
    raised outside any `except` block whose exception holds request or response data
    (L-09).
- **Check:** `test_redirect_keeps_the_key_out`.

## Open questions

### Q-01: How frame conversion ships

**Status:** answered by D-25.

Core stays pandas-free (D-09). The proposal is a `water-ogcapi[pandas]` extra with a
helper that returns a GeoDataFrame when any geometry is non-null and a DataFrame
otherwise. The alternative is a companion package. Return types that change with the
installed environment are rejected as API design.

### Q-02: Page bodies alias the aggregate

**Status:** answered by D-23.

`frozen=True` does not freeze nested dicts. If `pages[].body` holds the same feature
dicts as `geojson`, the page evidence can be mutated through the aggregate. Either copy
on retention or document that bodies alias.

### Q-03: Which operation produces which fields

**Status:** answered by D-22.

`iter_pages()` cannot know the final `page_count` or `completeness` while streaming, and
a single-item fetch has no pagination at all. Proposal: `iter_pages()` yields `Page`,
`collect()` returns `QueryResult`, and a single-item fetch returns its own smaller type
or a `QueryResult` with `pagination=None`. Settle before coding.

### Q-04: Failure points in `PartialResultsError`

**Status:** answered by D-24.

Offsets exist only under fan-out (D-05). Under `next`-link traversal a failure point is
a page URL or cursor. Either generalize the field to failed page URLs or document the
error as fan-out-only.

### Q-05: Detecting upstream drift

USGS's versioning page says v0 is still under development and its interface and data may
change, while aiming not to break working queries. Proposal: a scheduled weekly CI run
of the network tests, plus a committed OpenAPI snapshot diffed on a schedule. A release
should also name the API snapshot it supports.

### Q-06: Versioning policy

**Status:** answered by D-21.

0.x semantics, what 1.0 means relative to USGS stabilizing the API, and a deprecation
policy.

### Q-07: Unverified API facts

Do not state these as fact until a live response confirms them:

- The NWIS auth header. USGS docs name an `X-Api-Key` header or an `api_key` query
    parameter; an earlier note said a lowercase `api_key` header.
- The rate-limit tiers, and whether `Retry-After` accompanies a 429.
- Which services send `X-RateLimit-*` headers.
- The page-size caps in D-13.

### Q-08: Release security gate

Partly built. No log record carries request headers (D-20). CI runs `pixi r audit`,
which resolves the runtime dependencies from PyPI and checks them with pip-audit. The
`key-shaped-string` pre-commit hook fails lint on any committed text holding a token
shaped like a USGS key: 40 letters and digits mixing upper case, lower case, and digits.
It misses an encoded key, a key of another shape, and a key glued to `/`, `-`, or `_`:
that boundary keeps base64 images in notebook outputs from matching. Left to review:
env-var key handling, the redaction invariant (D-04), and redirect behavior (D-12).
Every error raised in `Transport.get` keeps a traceback frame whose `headers` local
holds the caller's key, so an error reporter that captures frame locals needs its own
scrubbing. Recorded fixtures carry the same credential risk as `QueryResult`, so scrub
auth at record time.

### Q-09: orjson for decoding large responses

`Response.json()` decodes each page with `json.loads`, and a page can hold 10,000
features (D-13). tiny-retriever already uses orjson behind an optional `json` extra,
detected at import time with `importlib.util.find_spec`, though for serializing; here
the hot path is decoding.

- **For:** orjson decodes `bytes` directly, which is what `Response.content` holds, and
    its own benchmarks report several times faster decoding than `json` on large
    documents. As an optional extra it keeps the core at two direct dependencies (D-09).
    `orjson.JSONDecodeError` subclasses `json.JSONDecodeError`, so callers catch one
    type either way. PyPI has wheels through CPython 3.15; conda-forge builds stop at
    3.14 as of 2026-09-30.
- **Against:** Behavior would depend on the environment. `json.loads` accepts `NaN` and
    `Infinity` and orjson rejects them, so a page carrying `NaN` would parse without the
    extra and fail with it, the kind of environment-dependent result Q-01 rejects. Two
    decode paths double the decode tests. As a compiled extension it gates new Python
    versions on its wheels. Network time may dwarf decode time, which would make the
    gain invisible.
- **To decide:** Record the expected speedup first, then measure decode time, peak
    memory, and total wall time for real NWIS and FabricData pages at the default page
    size, with and without orjson. Adopt it only if decoding is a meaningful share of
    wall time. If adopted, make both paths treat `NaN` the same way.

## Questions for USGS

Sent on 2026-10-07. The water data team and the FabricData operator answered the same
day, and live probes settled the questions that were not sent.

1. **Paging.** Answered. On the water data endpoints, `sortby` returns only the first
    page by design, and callers who need more than 50,000 sorted features sort them
    locally (D-26). The USGS versioning docs guarantee no result order, `next` links
    carry an opaque cursor, and no response reports `numberMatched`. A `limit` above
    50,000 is rejected with a 400.
1. **Keys and rate limits.** Mostly answered. The water data endpoints honor `X-Api-Key`
    and report limits to keyed requests in `X-RateLimit-*` headers. FabricData takes no
    key and has reported its limit in those headers since 2026-10-07. Still open: does
    a 429 carry `Retry-After`?
1. **Metadata caching.** Settled by probes and not sent. No `/collections`,
    `/queryables`, or `/schema` response carries `ETag` or `Last-Modified`, so D-07's
    revalidation never fires against these hosts.
1. **Schema and queryables.** Settled by probes and not sent. On the time-series
    collections, `queryables` adds `id` and the monitoring-location fields to `schema`,
    and no property in both differs in type. `queryables` lists what can be filtered
    and sorted; `schema` lists what comes back.
