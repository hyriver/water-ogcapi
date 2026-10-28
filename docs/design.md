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

`water-ogcapi` is a lean client for the USGS OGC API Features endpoints, blocking by
default over an async core (D-48): NWIS, FabricData, and GeoConnex (base URLs in
`README.md`). It complements `dataretrieval` and offers a small runtime dependency
footprint with no pandas, native async, raw GeoJSON with the evidence of how it was
fetched, GeoConnex and FabricData coverage, and metadata caching.

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
    `collection` on every result: they belong in the client's metadata cache (D-07), and
    a TTL cache may predate the query, which weakens the provenance claim. Page bodies
    kept by default: that doubles memory (D-02), so bodies are opt-in with
    `keep_pages=True`. A separate `provenance` type: folded into `pagination` and
    `Page`.

### D-04: Credentials are redacted at construction

- **Status:** accepted. Since 2026-10-15, `redact_url()` lives in the leaf module
    `_redact.py`, so `ServiceError` masks its URL too; before, it relied on upstream
    rejection.
- **Decision:** Any type that carries request headers or params (`Page`, `QueryResult`,
    exceptions) redacts credentials in its constructor, keeping headers from an
    allowlist. Callers never have to remember.
- **Why:** Otherwise every pickled, logged, or notebook-displayed result leaks the key.
- **Rejected:** A header denylist, which fails open on any header nobody listed.
- **Limits:** Header redaction covers the key only while it travels in a header. With
    the `api_key` query-parameter form, the key lands verbatim in `Response.url` and in
    every `ServiceError` message, so D-18 rejects that form.

### D-05: Follow `next` links; offset fan-out only where verified

- **Status:** accepted. D-35 replaces offset fan-out with partitioned fan-out on NWIS
    only.
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

- **Status:** accepted; built in #18, as D-49 records.
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
    attempts against a window that has not reopened. The governor (D-49) paces every
    attempt from the counts on the responses.

### D-07: In-memory, success-only metadata cache

- **Status:** accepted; built in #17.
- **Decision:** Per-URL TTL. Writes only on 2xx. Revalidation with `ETag` and
    `Last-Modified`. Expired entries are kept so they can revalidate, and a 304 keeps
    the original payload. `refresh()` clears the cache and the collection index. No
    SQLite and no disk.
- **Why:** Credentials travel in headers, so cache keys never contain them. The
    transport returns a 304 as a response so revalidation works.
- **Current state:** `collections()`, `queryables()`, and `schema()` keep the parsed
    document per URL for one hour (`_cache.TTL`) on the monotonic clock. A 304 renews
    the entry and replaces any validator it carries. A 304 to a request that sent no
    validators raises `ServiceError`. Each call returns a deep copy, since nested
    `extra` values are plain lists and dicts (D-33). Concurrent calls for one URL wait
    on one request, behind a lock per URL that is dropped when the event loop changes.
- **Limits:** No response from the three services carries `ETag` or `Last-Modified` (the
    NWIS probes of 2026-10-07 and every fixture), so revalidation never fires there and
    the cache works on its TTL alone. CloudFront in front of the USGS hosts serves
    metadata up to a day old, whatever this cache holds.

### D-08: Sync wrapper on a background event-loop thread

- **Status:** accepted; amended by D-46.
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

- **Status:** accepted; D-30 sets the values and drops the FabricData key. D-26 replaces
    the GeoConnex sort key, D-35 drops sort keys and the offset fan-out flag, and D-41
    adds the default page sizes.
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

- **Status:** accepted. D-35 drops the GeoConnex sort scenario, and D-30 counts 22
    two-digit HUCs.

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

- **Benchmarks:** cold start to first feature, round trips for a single-page result,
    requests spent per 100,000 features, peak memory against result size, and recovery
    after a 429 mid-pagination, all against mocks. `scripts/bench.py` measures them, and
    [Performance](performance.md) records a run.

- **Limits:** Fixtures stay green while upstream drifts, live tests stay out of CI, and
    FabricData failures turn into skips, so upstream drift is invisible to them; the
    weekly drift run (D-63) sees it.

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
    committed in `docs/examples/images/`. The `navigation.indexes` feature makes it the
    Examples section's own page, so the section header opens the cards and the nav lists
    only the notebooks (2026-10-23).
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

- **Status:** accepted; D-30 confirms NWIS honors the header.
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
- **Limits:** Q-07 left open whether NWIS honors the header form; D-30 confirmed it
    does.

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
    request at DEBUG. No record carries request headers (Q-08). At import, the package
    raises the `hpack` logger to INFO unless the caller set its level first, since hpack
    logs every HTTP/2 header value at DEBUG, the `X-Api-Key` included; h2 marks only
    `authorization` and short cookies as sensitive.
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
    mode and leaks that handle. A caller who sets `hpack` to DEBUG gets the key in those
    records.
- **Check:** `test_logs_name_each_retry_and_keep_the_key_out` and
    `tests/test_logging.py`.

### D-21: Versioning and deprecation policy

- **Status:** accepted; D-31 records that its 1.0 condition is met.
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

- **Status:** accepted; amended by D-36 and D-38.

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

- **Status:** accepted; amended by D-38, D-52, and D-56.

- **Date:** 2026-10-07.

- **Decision:** A failure point is the failed request's URL as sent, query included.
    Under fan-out the offset sits in that query, so it needs no field of its own. The
    transport raises every `ServiceError` with that URL, a timeout or connection failure
    included.

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
    modes share. The URL the transport was called with lacks the merged `params`, so it
    cannot replay a first-page request. A serverless function can be stopped without an
    exception, so the resume point has to exist after each page.

- **Rejected:** Offsets with a fan-out-only error, which gives sequential traversal
    weaker recovery. `iter_pages()` raising `PartialResultsError` with the fetched
    pages, which forces it to keep what it streamed (D-02).

- **Limits:** A resume replays against a collection that may have changed, and a cursor
    in a `next` link may expire.

### D-25: Frame conversion ships as an optional extra

- **Status:** superseded by D-58.
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

- **Status:** superseded by D-35.

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

### D-27: Python packages come from PyPI

- **Status:** accepted.

- **Date:** 2026-10-07.

- **Decision:** Every pixi environment installs its Python packages from PyPI: the
    runtime dependencies through the editable `water-ogcapi` entry in
    `[tool.pixi.pypi-dependencies]`, and the tooling through `[dependency-groups]`.
    conda-forge supplies Python and system libraries. `[tool.pixi.dependencies]` holds
    only system libraries such as GDAL or libnetcdf; the project needs none, so the
    table is absent.

- **Why:** `httpx2` and `h2` sat in `[tool.pixi.dependencies]` as well as in
    `[project] dependencies`. pixi installed the conda-forge builds and counted them as
    satisfying the PyPI requirements, so every environment tested conda-forge's packages
    while `pip install` gets PyPI's wheels, and the `h2` security floor had to be raised
    in two places. With one source per package, the test environments run the wheels a
    `pip install` gets.

- **Rejected:** Listing each runtime dependency in both tables, for the reasons above.
    Tooling as `optional-dependencies`, which publishes `dev`, `lint`, and the other
    tool sets as installable extras of the package.

- **Limits:** No pixi environment tests the planned conda-forge package, which resolves
    everything from conda-forge. D-01's separate `h2` entry stays for that recipe.

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

### D-29: What a `Page` keeps of a request and response

- **Status:** accepted; amended by D-53. D-51 keeps the paths these types pickle under.
- **Date:** 2026-10-08.
- **Decision:** `Page` redacts in its constructor. Its `url` and each `Link.href` mask
    the value of a credential query parameter (`api_key`, read percent-decoded, in any
    letter case, after `&` or `;`) as `REDACTED`, and drop userinfo and fragment. Other
    query entries keep their bytes, so a cursor still replays. `params` masks the value
    of a parameter named `api_key` in any letter case. `headers` keeps `age`,
    `cache-control`, `content-crs`, `content-type`, `date`, `etag`, `last-modified`,
    `retry-after`, `x-api-umbrella-request-id`, `x-ratelimit-limit`, and
    `x-ratelimit-remaining`, with lowercase names, and drops the rest. `params` and
    `headers` are read-only `dict` subclasses, so they pickle and pass through
    `dataclasses.asdict`, and `links` and `pages` accept only `Link` and `Page` objects.
    `QueryResult` holds no headers or params of its own, so its pages carry its
    redaction.
- **Why:** Each kept header has a consumer: CRS provenance (#22), the metadata cache
    (D-07), a caller checking the counts the quota governor read from the transport's
    responses (D-49), and the gateway's request id, which identifies a request in a
    report to USGS. A masked value keeps the evidence that a credential was sent.
- **Rejected:** Dropping a credential entry from the URL, which hides that it was sent.
    `MappingProxyType` for `params` and `headers`, which neither pickles nor passes
    through `dataclasses.asdict`. Redacting `body`, `content`, or `geojson`, which
    rewrites the payload D-03 keeps verbatim.
- **Limits:** Redaction goes by name, so a key under another parameter name passes
    through, and so does a key inside a body. A server echoing a query-form key into its
    `next` links would put it in `body`; D-18 keeps the key out of the query string, so
    no server receives a query-form key to echo. The read-only dicts guard their own
    methods; `dict.__setitem__(page.params, ...)` or a second `__init__` call still
    writes, as `object.__setattr__` does on any frozen dataclass.

### D-30: Live checks of the Q-07 API facts

- **Status:** accepted.

- **Date:** 2026-10-08.

- **Decision:** Probes on 2026-10-07 and 2026-10-08 and the fixture recording
    (`tests/fixtures/README.md`) settle each Q-07 item.

    | Fact                               | Verdict      | Evidence                                                                                                                                                                               |
    | ---------------------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
    | NWIS honors the key in `X-Api-Key` | confirmed    | Keyed responses carry `X-RateLimit-*` headers, and a new key's first response read `Remaining: 3999`. Unkeyed responses carry none.                                                    |
    | A lowercase `api_key` header       | not tested   | The USGS keys page names only `X-Api-Key` and the `api_key` query parameter.                                                                                                           |
    | NWIS limits for a key              | confirmed    | `X-RateLimit-Limit` reads 4000 on metadata endpoints and 1000 on `/items`. The keys page counts requests per hour.                                                                     |
    | NWIS limit without a key           | inconclusive | Unkeyed responses carry no `X-RateLimit-*` headers.                                                                                                                                    |
    | FabricData limit                   | inconclusive | `X-RateLimit-Limit: 300` on keyed and unkeyed responses alike, with no window stated. `Remaining` fell from 299 to 291 over nine unkeyed requests.                                     |
    | `Retry-After` on a 429             | inconclusive | Never provoked. The docs are silent and USGS has not answered.                                                                                                                         |
    | Services sending `X-RateLimit-*`   | confirmed    | NWIS on keyed responses only, FabricData on every response since 2026-10-07, GeoConnex on none of 11.                                                                                  |
    | NWIS page cap                      | confirmed    | `limit=50001` returns 400 "Limit of 50000 exceeded".                                                                                                                                   |
    | FabricData page cap                | confirmed    | The OpenAPI document declares `limit` up to 1000, default 10, and `limit=1001` returns 400.                                                                                            |
    | GeoConnex page cap                 | partly       | The OpenAPI document declares `limit` up to 10,000, default 500. `limit=10001` returned 200 on a query matching 157 features, so whether a larger result is cut at 10,000 is untested. |

    The profiles (D-13) take these values. NWIS sends the key in `X-Api-Key` (D-18) to
    `https://api.waterdata.usgs.gov/ogcapi/v1/`, trailing slash included, and caps pages
    at 50,000. FabricData sends no key and caps pages at 1,000. GeoConnex caps pages at
    10,000. The quota governor (D-06) reads the headers on NWIS and FabricData and uses
    its static default on GeoConnex. The GeoConnex `hu02` collection holds 22 regions,
    `01` to `22`, so D-14's count of 21 two-digit HUCs is one short.

- **Why:** FabricData reported the same limit with and without the key, so no response
    showed the key doing anything there, and the maintainer chose to send none on
    2026-10-07. The USGS versioning page names v1 as current, and `/ogcapi/v0/` still
    answers with the same landing document. The bare `/ogcapi/v1` answers 307 to
    `http://` with the header key copied into the query (L-11), and the form with the
    trailing slash returns 200.

- **Rejected:** D-13's plan for FabricData to use the NWIS key convention, which sends a
    credential where no response showed an effect.

- **Limits:** Only NWIS documents its window. FabricData's is unknown, and the
    maintainer's reading is 300 a minute. `Remaining` is approximate: it rose between
    consecutive NWIS requests on 2026-10-07. The 10,000 cap keeps GeoConnex requests at
    or below the declared maximum, so the untested behavior above it is never reached.
    Every value here can change without notice. The weekly drift run (D-63) sees a
    changed cap or OpenAPI declaration; the header facts stay unobserved until a probe
    repeats them. On 2026-10-13 a key whose limit USGS had raised on request read
    `X-RateLimit-Limit: 3000` on `/items` and on metadata, and requests to both took
    numbers from one `Remaining` sequence: a raised key appears to replace the
    per-endpoint limits with one hourly budget (six requests). A response served from
    CloudFront's cache (`x-cache: Hit`) carries the cached response's `X-RateLimit-*`,
    and `Remaining` lags a burst of requests by a few seconds.

### D-31: 1.0 no longer waits on NWIS

- **Status:** accepted.
- **Date:** 2026-10-08.
- **Decision:** D-21's condition for leaving 0.x is met: USGS serves NWIS under
    `/ogcapi/v1`, and the package targets it (D-30). The package may release 1.0 once
    the maintainer judges its own public API settled. D-21's version scheme and
    deprecation rule stand.
- **Why:** D-21 held 0.x because USGS documented v0 as under development. The USGS
    versioning page names v1 current and promises no breaking changes within a version,
    with a new version running alongside the old one for a while. Chosen by the
    maintainer.
- **Rejected:** Moving the trigger to the next NWIS path, which holds 0.x for as long as
    v1 lasts.
- **Limits:** FabricData (pygeoapi 0.23.4) and GeoConnex (pygeoapi 0.24.dev0) publish no
    versioning promise, so a change there can still force a major release after 1.0.

### D-32: Releases publish from a `v` tag through trusted publishing

- **Status:** accepted.
- **Date:** 2026-10-09.
- **Decision:** `pixi r release vX.Y.Z [notes-file]` refuses unless `HEAD` is already on
    `origin/main`, then creates an annotated tag whose message is the release summary
    and pushes only that tag. The push runs `.github/workflows/release.yml`: git-cliff
    writes the GitHub release notes, headed by the tag message, from the commits since
    the previous tag; `uv build` builds the sdist and wheel, and each one must install
    alone, import, and report the tag's version; `astral-sh/attest-action` writes PEP
    740 attestations; and `uv publish` uploads both from the `pypi` environment through
    PyPI trusted publishing, following Astral's `trusted-publishing-examples`. Only tags
    shaped like PEP 440 final, `rc`, `a`, or `b` versions run it. The upload waits for
    both the build and the release notes. Every action is pinned to a commit SHA, each
    job gets only the permissions it uses, and no job restores a cache. The GitHub
    releases page is the changelog. A release candidate such as `v0.1.0rc1` claims the
    PyPI name early; the first final release is the first version people are asked to
    test, usable and with the tutorials in place. A pre-release tag publishes the same
    way, marked as a pre-release on GitHub with its tag message as the notes.
    git-cliff's `tag_pattern` matches only `vX.Y.Z`, so a final release's notes start at
    the previous final and cover every commit since, release candidates included.
- **Why:** Trusted publishing stores no PyPI token: PyPI accepts a short-lived OIDC
    token issued only to this repository, this workflow file, and the `pypi`
    environment. A PyPI version number is spent once uploaded, so a failed summary has
    to stop the upload. Tagging only a merged commit keeps an unreviewed commit from
    reaching GitHub through its tag. SHA pins and the absent cache keep a moved action
    tag or a poisoned cache out of the job that publishes. The `v` prefix matches the
    other HyRiver packages.
- **Rejected:** A PyPI API token in a repository secret, which stays valid until someone
    revokes it. A tracked `CHANGELOG.md`, which needs a commit on `main` before every
    tag. A final `0.0.1` to claim the name, which presents an unusable version as a
    release; the maintainer chose a release candidate. git-cliff's `ignore_tags` for
    release candidates, which still started a final's notes at the release candidate.
- **Limits:** The `pypi` environment has no required reviewer and no ruleset protects
    `v*` tags, so anyone with write access can publish by pushing a tag. The GitHub
    release is created before the upload, so a failed upload leaves a release with no
    package on PyPI. A PyPI pending publisher reserves no name, so until the release
    candidate uploads, another account can register `water-ogcapi` and invalidate it.
    While a release candidate is the only version, `pip install water-ogcapi` and uv
    install it, since both take a pre-release when no final version exists; the README
    says it is not usable yet.

### D-33: Metadata types keep every member the server sent

- **Status:** accepted.
- **Date:** 2026-10-09.
- **Decision:** `Collections`, `Collection`, `Extent`, `SpatialExtent`,
    `TemporalExtent`, `Schema` (for both `/queryables` and `/schema`), `Property`, and
    `Link` are frozen dataclasses built by `from_dict` and turned back into JSON by
    `to_dict`. Each carries the members it names as typed fields and every other member
    in a read-only `extra`, deep-copied both ways, so `from_dict(doc).to_dict() == doc`.
    A field is `None` when its member is absent or has a shape the field cannot hold,
    such as a null, a string where a list belongs, or a bbox of 5 numbers; that member
    stays in `extra` as sent. A list with one unparsable item stays whole in `extra`.
    `Collection.from_dict` raises `ValueError` without a string `id`, `Link.from_dict`
    without a string `href`, and `Collections.from_dict` for an entry without an `id`.
    `extra` is left out of the hash, so every type stays hashable.
- **Why:** L-04. Keeping a malformed member in `extra` matches how the transport treats
    a malformed header (`parse_count` returns `None`), and a USGS schema change stays
    visible to the caller while parsing goes on. A collection with no `id` cannot enter
    the collection index, so that case raises. pystac's `extra_fields` and `to_dict`
    follow the same pattern. The fixture test checks the round trip and also that no
    member a field names landed in `extra`, which a round trip alone cannot see.
- **Rejected:** Storing the raw document behind typed accessors, which is lossless by
    construction and leaves the round trip test nothing to prove (L-03). Raising on any
    malformed member, which turns one odd member into a failed metadata fetch.
- **Limits:** `to_dict` returns `href` values redacted (D-04), so a link that carried a
    credential does not round trip. Redaction reaches only parsed links: a `Link.extra`
    member, or a `links` list kept whole in `extra` because one entry lacks `href`,
    passes through like a key inside a body (D-29). `extra` is read-only at its top
    level; nested values are plain dicts and lists. `Property` types five JSON Schema
    keywords; `enum`, `example`, and the rest stay in its `extra`.

### D-34: `iter_pages()` follows `next` links within the base URL's origin

- **Status:** accepted; amended by D-40, and by D-53, under which a page whose link
    carries a credential fails before it is yielded.
- **Date:** 2026-10-12.
- **Decision:** `OGCService.iter_pages()` requests `{base_url}/collections/{id}/items`
    with the caller's params plus `f=json`, then follows each page's `next` link until a
    page has none (D-05). A page shorter than requested or without `numberMatched` ends
    nothing (L-01). Among several `next` links it takes the first whose type is GeoJSON,
    JSON, or unstated, else the first. It follows a link only when the link's scheme,
    host, and port, with default ports filled in, match `base_url`. Otherwise it raises
    `PaginationError` after yielding the page that carried the link, with one
    `ServiceError` whose URL is the refused link. A `next` link whose `href` matches one
    already followed, or the first request as sent, raises the same way, and so does one
    carrying a credential in its query (L-11), which the transport would refuse with a
    `ValueError` that drops the pages already fetched. A response fails like a failed
    request (D-24) unless its body is a FeatureCollection with a `features` list, a
    `links` member that is a list when present, and a string `href` on every `next`
    link.
- **Why:** The key travels in a header (D-18) that httpx2 does not strip across hosts
    (D-12), so following a link to another host, or to `http://`, would send the key
    there. A pygeoapi deployment with a wrong `server.url` writes such links. FabricData
    links name port 443 (`api.water.usgs.gov:443`), hence the default ports. A 200
    holding an HTML gateway page, a cut-off body, or a JSON error object, or a page
    whose `next` link has a null `href`, would otherwise read as the last page, and
    `collect()` would report a finished, possibly empty, result. A caller collecting
    every page never stops iterating, so a repeated link would spend the NWIS quota of
    1,000 `/items` requests an hour (D-30) without end.
- **Rejected:** Following any link, the leak path of L-11. Ending the traversal at a
    refused link, which reads as a finished traversal.
- **Limits:** The cycle check compares `href` text, so a loop whose links differ only in
    form, such as reordered query parameters, goes undetected. A subdomain of the base
    host counts as another host.

### D-35: Only NWIS fans out, by splitting a query into pieces

- **Status:** accepted; D-56 adds the split keywords and the concurrency.
- **Date:** 2026-10-12.
- **Decision:** Every service pages through `next` links (D-05), and `sorted-offset` is
    dropped. Concurrency exists only as partitioned fan-out on NWIS (#14): the NWIS
    class splits a query into independent pieces by `datetime` range or by site list and
    runs them concurrently, each following its own cursor. An interval includes both
    ends, so adjacent pieces get disjoint bounds. FabricData and GeoConnex never fan
    out, and no profile (D-13) carries a fan-out flag for them.
- **Why:** FabricData and GeoConnex page with `offset` links and report `numberMatched`
    on every page (fixtures), so one sequential traversal there is complete and
    checkable. Chosen by the maintainer. NWIS reports no `numberMatched` and guarantees
    no order across offset pages (D-26). USGS's efficiency guidance recommends queries
    sized near one page, split by year or by site and run in parallel, since every
    `next` page re-runs the query. A live check on `daily` returned the shared day in
    both of two adjacent `datetime` ranges.
- **Rejected:** D-26's sorted-offset fan-out on FabricData and GeoConnex, which rests on
    `sortby`, a draft extension, and whose throughput FabricData's 300 requests a minute
    caps whatever the concurrency (inferred, unmeasured). Offset fan-out on NWIS (D-26).
- **Limits:** A query with neither a `datetime` range nor a site list, such as a bbox
    alone, has nothing to split and runs in sequence. Pieces run against a collection
    that may change between them, as pages do (D-03). A failed piece is reported by the
    URL of its failed request (D-24).

### D-36: `complete` means the traversal ended and the checks that ran passed

- **Status:** accepted; amended by D-54 and D-57. Amends D-22.
- **Date:** 2026-10-13.
- **Decision:** `collect()` reads `complete` when the last page had no `next` link
    (D-05) and every check that could run passed. `Pagination.checks` lists the checks
    that passed: `end_of_links` always, and `count` when the pages agree on
    `numberMatched` and the features received equal it. NWIS reports no `numberMatched`,
    so its `complete` rests on `end_of_links` alone (D-57 adds `unique_ids`). A count
    that differs from `numberMatched`, a page whose `numberReturned` differs from its
    features, a count member that is present but not a non-negative integer (a null
    reads as absent), or pages that disagree, read `unknown` with `end_of_links` still
    listed. A failed page reads `limited` with stop reason `failure` and no checks. The
    `limit` stop reason is reserved for a caller cap, which no method offers yet.
- **Why:** Chosen by the maintainer. A server that omits `numberMatched` gives no second
    witness, and reading its end-of-links as `unknown` would make NWIS never complete.
    Listing the checks tells the caller how strong a `complete` is.
- **Rejected:** `complete` only when a count matched, which marks every NWIS result
    `unknown`. A `bool` for the count check, which hides which evidence backed
    `complete`.
- **Limits:** `complete` still cannot prove that no feature moved between pages (D-03).

### D-37: CRS provenance comes from `Content-Crs`, else the default

- **Status:** accepted. Since 2026-10-20 the types are `CRS` and `CRSSource`,
    capitalized as the acronym.
- **Date:** 2026-10-13.
- **Decision:** `QueryResult.crs` is a `CRS` with the first request's `crs` parameter
    (`requested`), each page's `Content-Crs` (`page_crs`), the CRS of the geometries
    (`effective`), and `source`. `effective` is the header when every page names the
    same CRS (`content_crs`), and `None` when pages name different CRSs or only some
    name one (`unknown`). Without a header on any page, each page's own request query
    decides: CRS84 when no request asked for another CRS (`default`), and `None` when
    every request asked for the same other CRS (`unconfirmed`) or the requests disagree
    (`unknown`). Asking for CRS84 counts as asking for the default.
- **Why:** A server that applies `crs` must name the CRS in `Content-Crs` (OGC API
    Features Part 2, requirement 15; abstract test 4 expects the requested CRS there),
    so a request no page confirmed may have been ignored. All four recorded fixtures
    send `Content-Crs` as CRS84. The OGC API Features default is CRS84 when nothing says
    otherwise.
- **Rejected:** Reading `effective` from the request, which reports a CRS the server may
    not have applied. Assuming CRS84 for an unconfirmed request, which mislabels a
    server that applies `crs` and omits the header. Raising on disagreement, which
    discards pages the caller can still use.
- **Limits:** `requested` reads only `crs`, so a `bbox-crs` or `filter-crs` is not
    recorded. A `default` source is an assumption, since no header confirms it.

### D-38: A request is identified by its method, URL as sent, and body

- **Status:** accepted; amended by D-39, D-44, and D-52, which replaces `_traverse()`
    with `_run()`. Amends D-22 and D-24.
- **Date:** 2026-10-13.
- **Decision:** `Transport.request(method, url, params=, headers=, content=)` sends
    every request, and `get()` calls it. Every `ServiceError` and every `Page` carries
    the request's `method` and `request_body` (the bytes sent, `None` without a body)
    beside its URL. One private traversal, `OGCService._traverse()`, takes the first
    request's URL, method, params, body, and headers (a `Request`, D-44), and follows
    `next` links (D-34) with the same method, body, and headers; `iter_pages()` sends a
    `GET`, or a `POST` for a CQL2-JSON filter (D-39). The failure point of D-24, and the
    resume point after each page, become the method, the URL as sent, and the body. The
    transport retries a `POST` like a `GET`.
- **Why:** CQL2 over `POST` (#10) must re-send its body to each `next` URL: the
    2026-10-12 NWIS check showed that a `GET` there drops the filter and returns other
    sites with a 200. A URL alone would replay as that unfiltered query, which neither
    `completeness` nor CRS provenance detects. One traversal keeps the origin, cycle,
    and credential checks in one place for every request shape. This package posts only
    CQL2 queries, which change nothing on the server, so a retry is safe. Chosen by the
    maintainer after the 2026-10-13 project review.
- **Rejected:** A second traversal loop for `POST` inside #10, which copies those
    checks. Keeping the URL as the only identity and amending D-24 after resume (#21)
    had built on it.
- **Limits:** The request's own headers, such as its content type, are not recorded
    (D-20), so a replay must supply them. A `POST` that changes server state needs
    retries off, which the shared HyRiver client has to make opt-in.

### D-39: Query forms are keyword arguments of `iter_pages()` and `collect()`

- **Status:** accepted. Amends D-38: `iter_pages()` also sends a `POST`.
- **Date:** 2026-10-14.
- **Decision:** `iter_pages()` and `collect()` take the same query keywords beside
    `params`: `bbox`, `geometry` (sent as its bbox, D-11), `filter` (a `str` goes out as
    CQL2-Text in the `filter` parameter of a `GET`, a mapping as a CQL2-JSON `POST`
    body), and `ids` with `id_property` (a CQL2 `in` over `POST`). An item fetch is its
    own method, `item()`, returning a `Page` (D-22). Selectors combine with AND, as OGC
    API Features combines parameters. `bbox` with `geometry`, `ids` with `filter`, and a
    keyword whose parameter `params` also holds, raise `ValueError` before any request.
    A bbox goes out with every digit and no exponent.
- **Why:** Every query form streams through the primitive (D-02), combines with
    `datetime` or `limit` in one request, and leaves two signatures for the sync class
    (#12) to copy. The identifier property is explicit, since `id`,
    `monitoring_location_id`, and FabricData's `staid` mean different things. Formatting
    to six decimals, as the prototype did, can move a bbox edge inward and drop the
    features on it.
- **Rejected:** One method per query form (`bybox`, `byid`, `bycql`, `byfilter`,
    `bygeometry`), as the prototype and `pynhd.ogcapi` had, which needs an iterating and
    a collecting variant of each and cannot combine forms. A query object passed to both
    methods, which adds a public type for a few keywords. Translating CQL2-Text to
    CQL2-JSON to combine `ids` with a text filter.
- **Limits:** `bbox-crs` and `filter-crs` go through `params`. A comma-separated `GET`
    for NWIS site lists is left to NWIS fan-out (#14). An unkeyed check on 2026-10-14
    (one page each, filter applied per `numberMatched`) found CQL2-Text over `GET`
    working on NWIS, FabricData, and GeoConnex, and CQL2-JSON over `POST` on FabricData
    and GeoConnex. Re-sending the body to `next` links is checked live on NWIS only.

### D-40: Every service request goes through `OGCService._send()`

- **Status:** accepted; amended by D-52, which replaces `_traverse()` with `_run()`, and
    by D-53, under which the service's headers replace a request header of the same
    name. Amends D-34.
- **Date:** 2026-10-14.
- **Decision:** `collections()`, `item()`, and `_traverse()` send through one private
    method, `_send()`, which adds the headers `_headers()` returns and refuses, before
    sending, any URL outside the scheme, host, and port of `base_url`, the first URL of
    a traversal included. The base service's `_headers()` returns none; a service class
    overrides it to add its key (D-18). A traversal reports a refused URL as a
    `PaginationError` (D-24), as D-34 did for a `next` link.
- **Why:** A service class adding its key at each request site would add it three times,
    and a later site could miss it. D-34 checked only `next` links, so a resume (#21)
    replaying a stored URL from another service would send the key to that host before
    any check.
- **Rejected:** Binding keys to hosts inside `Transport`, which stays a general tool.
    Overriding `collections()`, `item()`, and `iter_pages()` in each service class.
- **Limits:** The check compares origins, so a URL on the same host outside the path of
    `base_url` passes.

### D-41: Service profiles set the page sizes

- **Status:** accepted. Amends D-13.
- **Date:** 2026-10-15.
- **Decision:** `ServiceProfile` holds a service's base URL, its default and maximum
    `limit`, the environment variable holding its key (D-18), and, since D-49, the
    window its `X-RateLimit-Limit` counts over, which the governor reads (D-06), or
    `None` where it sends no such headers. Each service class sets `profile`; the base
    `OGCService` has none and sends no `limit`. An items query adds the profile's
    `default_limit` when `params` hold no `limit`, and raises `ValueError` before
    sending when `limit` is not an integer from 1 to `max_limit`. The defaults are
    10,000 on NWIS (D-13), 1,000 on FabricData, and 500 on GeoConnex.
- **Why:** FabricData's server default of 10 spends 100 of its 300 requests on 1,000
    features, a third of a minute's budget on the maintainer's reading (D-30).
    GeoConnex's 500 is its server default, sent explicitly so a server change does not
    move it, and its HUC polygons make a 10,000-feature page large. Chosen by the
    maintainer. NWIS and FabricData answer a `limit` above the cap with a 400 (D-30),
    and raising first names the cap.
- **Rejected:** A sort key and a fan-out flag in the profile (D-35); the NWIS class
    carries fan-out (#14). Clamping an oversized `limit`, which changes the request
    without saying so.
- **Limits:** The caps are D-30's live values and can change without notice (D-63). Only
    the first request's `limit` is checked; a `next` link's own query is sent as the
    server wrote it.

### D-42: NWIS reads its key once and has a fixed base URL

- **Status:** accepted. Since 2026-10-16 `Transport.request()` also refuses a key that
    is not printable ASCII (D-43); `NWIS` keeps its check so the error comes at
    construction. Since 2026-10-26 a pickle leaves the key out. Since 2026-10-28 a used
    service pickles too (D-64).
- **Date:** 2026-10-15.
- **Decision:** `NWIS(api_key=None)` takes its key from the argument, else from
    `USGS_API_KEY`, read once at construction. An empty string sends no key, and a key
    that is not printable ASCII raises `ValueError` there, since httpx2's
    `UnicodeEncodeError` for it holds the whole key in its `repr`. `_headers()` adds the
    key to every request as `X-Api-Key` (D-18, D-40). Service classes take no `base_url`
    argument and send only to their profile's base URL. A caller who needs another host
    uses `OGCService`, which sends no key. A pickle of the async or blocking `NWIS`
    leaves the key out, and loading one reads `USGS_API_KEY` in the loading process.
- **Why:** With no `base_url` argument, D-13's rule that a USGS key never reaches a
    non-USGS host holds by construction, and `_send()` keeps it to that origin (D-40).
    Reading the environment once fixes the key for the life of the instance, so every
    page of a traversal carries the same key. A pickled service held the key in
    plaintext, which cloudpickle carries to Dask workers in any closure that captured it
    (#43 review); the pickle rule was chosen by the maintainer.
- **Rejected:** Reading the environment on each request. A `base_url` argument, which
    sends the environment's key to whatever host it names. Refusing to pickle a service.
    Writing a passed key into `os.environ` so every process reads it there, which hands
    the key to every subprocess and lets a second instance replace the first's key.
- **Limits:** The instance holds the key unredacted in a private attribute; the default
    `repr` does not show it. A pickle leaves out the per-process state, though not what
    an injected transport recorded (D-64). A passed `api_key` does not survive a pickle,
    `""` included, and a process without `USGS_API_KEY` loads a service that sends no
    key. `copy.deepcopy` reads the environment the same way and drops the same state;
    `copy.copy` of the blocking `NWIS` shares its async service and keeps the key.
    Unkeyed NWIS responses carry no `X-RateLimit-*` headers (D-30), so the governor
    (#18) uses its static default there.

### D-43: `Transport` is public

- **Status:** accepted. Since 2026-10-16 `request()` refuses a header value that is not
    printable ASCII (see Limits).
- **Date:** 2026-10-15.
- **Decision:** The package exports `Transport`. A caller tunes the timeout, retries,
    backoff, concurrency, and HTTP/2 by passing one to a service class, and several
    services can share one. `Response` stays private.
- **Why:** The service classes tell callers to pass
    `Transport(timeout=...,   max_retries=...)`, and the sync class (#12) and the
    HyRiver shared client take one. Without the export, a private import is the only
    route (D-21). Chosen by the maintainer.
- **Rejected:** `timeout=` and `max_retries=` keywords on each service class, which
    repeat the transport's parameters in three signatures and leave sharing a transport
    without a public route.
- **Limits:** The constructor, `request()`, `get()`, and `aclose()` become public API
    under D-21. `request()` returns the private `Response`, which a caller reads through
    its documented attributes only. A caller can pass any header, so `request()` raises
    `ValueError` before sending for a header value that is not printable ASCII, naming
    the header and never its value: httpx2's `UnicodeEncodeError` for a non-ASCII value
    holds the whole value in its `repr`, and the check in `NWIS` (D-42) covers only its
    own key.

### D-44: `_query.py` builds the request, and `_pieces()` is the fan-out seam

- **Status:** accepted; amended by D-52, which replaces `_traverse()` with `_run()`, and
    D-56, which runs the pieces concurrently. Amends D-38 and D-39. D-50 moved page
    parsing out of `_client.py` after all.
- **Date:** 2026-10-15.
- **Decision:** `_query.py` builds the first request of an items query with no I/O:
    `items_request()` validates the query forms (D-39) and returns a `Request` holding
    the URL, method, params, body, and the CQL2 content type. `iter_pages()` passes it
    to `_pieces()`, which returns it whole on the base service, and traverses each piece
    in order through `_traverse(request)`. NWIS fan-out (#14) overrides `_pieces()` to
    split by `datetime` range or site list, and resume (#21) replays a stored `Request`.
- **Why:** The 2026-10-15 project review found that `iter_pages()` built its request
    inline, so #14 could fan out only by overriding `iter_pages()` whole, signature,
    docstring, and validation included, which D-40 rejected. `_client.py` held query
    building, page parsing, the traversal summary, and the class in 735 lines. Chosen by
    the maintainer.
- **Rejected:** Moving page parsing (`_page()`, `_pagination()`) out as well; neither
    #14 nor #21 adds to it.
- **Limits:** The base `iter_pages()` runs pieces one after another; #14 adds the
    concurrency. `summarize()` (`_pagination()` before D-50) summarizes one traversal,
    so across pieces it takes `sort_by` from the first page and its `numberMatched`
    agreement check does not hold.

### D-45: Input errors become a `ValidationError` that is also a `ValueError`

- **Status:** accepted; built in #20, amended by D-47.
- **Date:** 2026-10-15.
- **Decision:** #20 adds `ValidationError`, a subclass of both `WaterOGCAPIError` and
    `ValueError`, and every check that rejects caller input before sending raises it:
    the query forms (D-39), `limit` (D-41), path segments, an `api_key` in `params`
    (D-18), a malformed key (D-42), a malformed header value (D-43), the `Transport`
    arguments, and the `configure_logger` arguments. Its message lists the valid options
    where a finite set exists. An unencodable CQL2-JSON value stays a `TypeError`.
- **Why:** The 2026-10-15 project review counted about 12 sites raising bare
    `ValueError`, and every public Raises section promises it. Subclassing `ValueError`
    keeps those contracts and lets `except WaterOGCAPIError` catch input errors too, so
    #20 adds a type without breaking a caller. Chosen by the maintainer.
- **Rejected:** A `ValidationError` under `WaterOGCAPIError` alone, which breaks every
    documented `ValueError` and any caller catching it.
- **Limits:** Errors parsing a server's document (`_metadata.py`, `_results.py`) are not
    caller input and stay `ValueError`.

### D-46: The sync API is a `water_ogcapi.sync` module of blocking twins

- **Status:** accepted. Amends D-08. D-48 moves the blocking classes to the package root
    and the async ones to `water_ogcapi.aio`. D-56 settles the concurrent pieces of #14.
    Since 2026-10-26 a blocking call made on the loop's own thread raises, and so does
    one whose loop thread has stopped.
- **Date:** 2026-10-16.
- **Decision:** `water_ogcapi.sync` holds `OGCService`, `NWIS`, `FabricData`, and
    `GeoConnex` with the names and constructors of the async classes. Each builds its
    own async service and runs every call on one event loop in a daemon thread, which
    the first sync call in the process starts and every sync service shares. A typed
    decorator copies each async method's signature and docstring onto its blocking twin,
    and `iter_pages` returns an iterator that fetches one page per step. A call waits in
    0.1 s slices, and an exception raised while it waits, such as a notebook interrupt,
    requests cancellation of the call's task and its request before propagating; the
    task finishes cancelling on the loop afterward. `close()` closes the service's
    transport, created or passed, and a sync service sharing it reconnects on its next
    request. A blocking call made on the loop's thread, as from a logging handler or
    custom transport the loop runs, raises `RuntimeError` before it sends anything, and
    a call whose loop thread has stopped raises `RuntimeError` too.
- **Why:** A transport binds to the loop of its first request (D-10), so `*_sync`
    methods on `OGCService` would break the first async call after a sync one in the
    same notebook (2026-10-12 review). Matching names make switching an import change. A
    blocked wait misses an interrupt that only sets the interrupt flag, as
    `_thread.interrupt_main()` and ipykernel on Windows do. A passed transport is bound
    to the background loop, where the caller cannot close it. A call on the loop's
    thread waits for work only that thread can run, so it hung forever (2026-10-26
    probe); synchronicity raises in the same case. asyncio re-raises a `SystemExit` from
    a task out of `run_forever()`, which ended the thread before the call's result was
    set, so the caller polled forever (2026-10-26 probe). Names and the `close()` rule
    chosen by the maintainer.
- **Rejected:** `*_sync` methods on the async classes. Top-level names with a suffix or
    prefix (`NWISSync`, `SyncNWIS`). A wrapper around a caller's async service, which
    binds that caller's transport to the background loop. Fetching the next page while
    the caller handles the current one, which leaves a request running when the caller
    abandons the iterator. Leaving a passed transport open, as `OGCService.aclose()`
    does, and a blocking `Transport.close()`, which adds public API. Closing the async
    generator in the blocking iterator's `finally` and waiting for it, as synchronicity
    does: the wait hung at interpreter exit on 3.13, on the loop's thread, in a fork
    child closing an inherited split iterator, and under a lock the loop needed, and an
    interrupt during a step surfaced as `RuntimeError` from `aclose()` (2026-10-26
    probes).
- **Limits:** A `Transport` passed to a sync service binds to the background loop, so it
    cannot also serve an async service on the caller's loop. The thread stops only when
    code it runs raises `SystemExit` or `KeyboardInterrupt`, and it does not restart, so
    every later blocking call in the process raises. A transport closes only through
    `close()` or `with`. A child made by `os.fork()` starts its own loop, where a
    transport bound in the parent raises (D-10). Each iterator step runs in its own
    task, so an async iterator under the sync API must hold no task group or
    `asyncio.timeout` across a `yield`. An abandoned iterator's async generator closes
    through the loop's finalizer once nothing references it, which a held traceback
    delays. `close()` takes the same path and returns before that cleanup finishes.
    #14's concurrent pieces put requests in flight between steps, so #14 cancels them
    when its generator closes and revisits this entry.

### D-47: Queries are checked against cached metadata before the first page request

- **Status:** accepted.
- **Date:** 2026-10-19.
- **Decision:** `iter_pages()`, `collect()`, and `item()` take `validate=True`. Before
    the first page request each checks the collection against the cached collection
    index, and `iter_pages()` and `collect()` also check `id_property` and each `params`
    name outside `QUERY_PARAMS`, pygeoapi's 16 reserved names, against the collection's
    cached `/queryables`. An unknown name raises `ValidationError` (D-45) listing the
    valid ones. A copy cached before the call that lacks the name is revalidated once
    first, since the server may have added it; a failed revalidation raises
    `ServiceError` and leaves the copy as it was. `validate=False` skips the check.
    Concurrent checks share one request per metadata URL (D-07). Chosen by the
    maintainer.
- **Why:** pygeoapi, which all three services run, drops a query parameter its provider
    does not know. On 2026-10-19, `no_such_property=x` returned the whole collection
    from GeoConnex `hu02` (22 features) and FabricData `gagesii` (9,322). NWIS answers
    400 to an unknown property, but on the other two a typo widens the query and spends
    quota. The check costs at most two metadata requests per collection per hour.
- **Rejected:** Diagnosing a server's 400 or 404 after the fact, which sends no extra
    request and misses the silent widening. A check with no way to skip it: OGC lets a
    server accept a property its `/queryables` leaves out. Checking `sortby` and
    `properties` values or CQL2 property references: `/queryables` lists filter
    properties, and NWIS `daily` refuses `sortby=id` although `id` is a queryable.
- **Limits:** A server parameter outside `QUERY_PARAMS` needs `validate=False`. A failed
    metadata request raises `ServiceError` before any page request. `item()` checks only
    the collection. Each unknown name costs one revalidation of a cached document.

### D-48: The blocking classes are the top-level names, the async ones are in `aio`

- **Status:** accepted. Supersedes the module layout of D-46. Since 2026-10-20 a plain
    `import water_ogcapi` binds `aio` and `exceptions`, both in `__all__`, so
    `water_ogcapi.aio.NWIS` works and type-checks.
- **Date:** 2026-10-19.
- **Decision:** `water_ogcapi` exports the blocking `OGCService`, `NWIS`, `FabricData`,
    and `GeoConnex`, and `water_ogcapi.aio` exports the async classes under the same
    names. `water_ogcapi.sync` is removed. `Page`, `QueryResult`, `Transport`,
    `ServiceProfile`, the metadata types, and the exceptions stay at the top level for
    both. The rest of D-46 stands: each blocking class owns its async service and runs
    it on the background loop. Chosen by the maintainer.
- **Why:** Most users work in scripts and notebooks. With the async classes at the top
    level, `NWIS().collect("daily")` without `await` returns an un-awaited coroutine and
    a `RuntimeWarning`, the first thing a new user meets. With the blocking classes
    there, an async user who forgets `aio` gets a call that works and blocks the event
    loop, a mistake that user can diagnose. `redis` uses the same layout
    (`redis.asyncio`). No release has carried a query API, so the deprecation cycle of
    D-21 does not apply.
- **Rejected:** Keeping the async classes at the top level with `water_ogcapi.sync`
    (D-46). `water_ogcapi.asyncio` as the namespace, since
    `from water_ogcapi import   asyncio` shadows the standard library module in the
    caller's file. Prefixed names such as `AsyncNWIS`, the shape D-46 rejected for the
    blocking side.
- **Limits:** A blocking call made inside a coroutine blocks that event loop until it
    returns. The async core stays the primitive (D-02), so each blocking call pays one
    thread hop (D-08). Static analysis reads each generated twin as an attribute, so the
    griffe extension `scripts/griffe_twins.py` documents it on the API page with the
    async method's parameters and docstring, and fails the docs build if it finds none.

### D-49: The governor paces every attempt over the last eighth of the quota

- **Status:** accepted. Amends D-06, D-41, and D-43.

- **Date:** 2026-10-20.

- **Decision:** Every `OGCService` builds a `Governor` from its profile's
    `rate_limit_window` and passes it to `Transport.request()`, which awaits `wait()`
    before every attempt, retries included, outside the concurrency permit, and calls
    `observe()` on every response, a refused redirect included. With `remaining` of
    `limit` left, a start waits this many seconds after the previous one, from 0 at an
    eighth of the quota (`RAMP`) up to 60 (`MAX_SLEEP`), and 60 at zero:

    ```text
    window / remaining - 8 * window / limit
    ```

    Each `X-RateLimit-Limit` value keeps its own reading, and the widest interval read in
    the last `window` seconds applies. A response with `x-cache: Hit` or a nonzero
    `age`, or without both counts, changes no reading. While no reading from the last
    `window` seconds applies, starts are 1 s apart (`STATIC_INTERVAL`), the only spacing
    GeoConnex, unkeyed NWIS, and an `OGCService` without a profile get. A 429's
    `Retry-After` holds back every later start, up to 60 s, and that 429's counts are
    skipped. A wait rechecks when it wakes, so a reading or a 429 that arrives during it
    applies, and a start is booked only when the request goes, so a cancelled wait books
    nothing. `ServiceProfile.rate_limit_window` replaces the `rate_limit_headers` flag:
    3,600 s on NWIS, the hour api.data.gov documents, 60 s on FabricData, and `None`,
    the default, for a service that sends no `X-RateLimit-*` headers. A wait longer than
    the static interval is logged at INFO.

- **Why:** The retry loop consumes the 429 and 5xx responses, and a retry after a 0.5 s
    backoff would jump a paced queue, so the hook sits in the transport, as D-01
    planned. No service sends a reset header. Under a rolling window, spacing of
    `window / remaining` cannot run the count out, and subtracting `8 * window / limit`
    leaves the first seven eighths unpaced with no jump where the ramp starts: on a
    default NWIS key, 7.2 s at 100 left, 43 s at 50, and the cap from 41. At a steady
    rate the ramp settles at 89% of the quota. Against a mock rolling quota of 1,000 an
    hour, a 2,500-page traversal sent 992 and 954 requests in its first two hours, with
    no 429. A default NWIS key reports 1000 on `/items` and 4000 on metadata (D-30), and
    a metadata reading must not relax a low items one. A CDN hit carries the cached
    response's counts (D-30). The 1 s default keeps a host with no documented limit
    under 3,600 requests an hour, and D-06 promises it to every service without the
    headers. The 60 s cap keeps a long query moving at one request a minute, and if the
    count still runs out, the transport's 429 handling takes over. A 429's `Retry-After`
    is the server's own wait, which its zero count would stretch to 60 s. The maintainer
    chose the transport hook, the 1 s default, the cap, the ramp, pacing a service without
    a profile, and the single profile field, after advice from Codex, Gemini, and Fable.

- **Rejected:** The governor in `OGCService._send()`, which leaves the transport as it
    was but reads only the final response and leaves retries unpaced. State in
    `Transport` keyed by origin, which merges keyed and unkeyed traffic and different
    keys. A ramp that starts at the full quota, which settles at 62% of a rolling quota
    (Fable's derivation). A constant spacing of `window / (0.8 * limit)` from the first
    request, 4.5 s on a default NWIS key for every query. A static default of 5 s or 0.2
    s. Raising an error once the wait passes 60 s, which stops a query the window may
    still serve.

- **Limits:** State is per service instance, so two instances sharing a key, or another
    program using it, each see the server's count but not each other's requests.
    `Remaining` lags a burst by seconds (D-30), and the first seven eighths go unpaced,
    so concurrent requests can start on a stale reading near the ramp. A request that
    waits for a transport permit after its slot starts late, so up to `max_concurrent`
    can start together. A slower response with a higher count replaces a newer, lower
    reading. A raised limit keeps the old limit's reading live for a full window. Below
    about 41 left on a default NWIS key, the cap keeps the spacing short of what the
    rolling window needs. FabricData's 60 s window rests on one probe. A response whose
    headers arrive and whose body times out is not read. `request()` and `get()` take
    the governor as a public keyword (D-43). GeoConnex sends no rate-limit headers, so
    its requests start 1 s apart: at its default `limit` of 500 (D-41), a first
    validated query for 100,000 features sends 202 requests and takes at least 201 s
    ([Performance](performance.md#requests-per-100000-features)), and a larger `limit`
    cuts that.

### D-50: `_client.py` holds the service class, and response parsing lives beside its types

- **Status:** accepted. Amends D-44.
- **Date:** 2026-10-20.
- **Decision:** The package keeps flat private modules at its root. `_metadata.py` holds
    the parsers of metadata responses (`json_object()`, `parse_collections()`,
    `parse_schema()`, `collection_ids()`, `property_names()`), and `_paging.py` holds
    page parsing and the traversal summary (`parse_page()`, `next_href()`,
    `malformed_counts()`, `summarize()`). `_client.py` keeps `ServiceProfile`,
    `OGCService`, the origin check, and the reserved query names.
- **Why:** `_client.py` had reached 850 lines holding three concerns: metadata response
    parsing, page parsing and completeness accounting, and the service class. A reader
    looking for how a page is checked now opens a 126-line module. The moved functions
    are unchanged apart from their names, which drop the underscore because they cross a
    module boundary. Chosen by the maintainer, after advice from Codex.
- **Rejected:** One private package (`_utils/` or `_internal/`) holding modules with
    plain names, which moves the privacy marker up one level, lengthens every import and
    doc path, and changes about 45 test references and `scripts/griffe_twins.py`'s
    `pkg["_sync"]` lookup without changing what a reader must follow. Private
    subpackages by layer, such as `_http/`, before #14, #21, or the HyRiver shared
    client give a boundary a consumer; `_redact.py` serves the result and error types,
    so it would not sit in an HTTP package.
- **Limits:** D-44's reason for keeping page parsing in place, that neither #14 nor #21
    adds to it, still holds; the move is for reading. `_metadata.py` imports
    `ServiceError`, so the metadata types now load the exception module.

### D-51: Pickled results keep their module paths across releases

- **Status:** accepted. Amends D-29.
- **Date:** 2026-10-20.
- **Decision:** A pickle names each type by its defining module, such as
    `water_ogcapi._results.Page`. Those paths, listed as `PICKLED_PATHS` in
    `tests/test_pickles.py`, stay importable across releases: a module move leaves an
    alias at the old path, and a newly pickled type joins the list. The exported classes
    keep their real `__module__`.
- **Why:** pickle imports a class from the path it recorded, so a later release loads a
    saved `QueryResult` only while that path resolves. The test pickles a result, the
    metadata types, `ServiceProfile`, and each public exception, and fails when the set
    of paths changes, so a move cannot land without its alias. Chosen by the maintainer.
- **Rejected:** Setting `__module__ = "water_ogcapi"` on the exported classes, as httpx
    does. Pickles would name the public paths and modules could move freely, but
    `inspect.getsource` and Jupyter's `Page??` raise `OSError` for every exported class
    (checked on Python 3.12 and 3.15), and the private `ReadOnlyDict` would still need a
    pinned path. Promising nothing across versions.
- **Limits:** The promise covers paths only. A frozen slots dataclass pickles its fields
    as a list in field order, so adding, removing, or reordering a field still breaks an
    old pickle of that type. Only the types the test pickles are pinned.

### D-52: A query resumes from a `Checkpoint` of pending and held requests

- **Status:** accepted; amended by D-56. Amends D-24, D-38, and D-44.
- **Date:** 2026-10-21.
- **Decision:** A `Checkpoint` holds the `base_url` and collection of a query, its
    `pending` requests (the `next` link of the chain in progress or the request that
    failed, then each piece not yet started), the identity of every request already
    answered (`held`: method, URL as sent with credentials masked, and body, D-38), and
    `malformed` (a held page had a malformed count). Each page `iter_pages()` yields
    with more to fetch carries the checkpoint after it in `Page.checkpoint`; a
    `PaginationError` carries the one with the failed request first, as does the
    `QueryResult` of a `PartialResultsError`. `iter_pages(resume=checkpoint)` and
    `collect(resume=result_or_checkpoint)` send the pending requests as stored, refuse
    any held request before sending it, and refuse a query keyword or a checkpoint of
    another service or collection. `collect()` takes the `QueryResult` when pages were
    held, so the earlier pages and features always come with their checkpoint, and a
    `Checkpoint` only when none were. `Request` and `Checkpoint` are public and pickle;
    there is no JSON form. A `Request` refuses a credential in its URL, userinfo, or
    params, and any header but `Accept` and `Content-Type`, and freezes its mappings. A
    refused request (a repeat or an off-origin URL) goes last in the checkpoint, so a
    resume runs the other pieces and then refuses it again; with no other piece left, it
    leaves no checkpoint. A credential `next` link leaves none either, since no
    `Request` can hold it. Any other failure, one with attempts 0 such as a transport
    closed while the request waited included, keeps the failed request first. The
    checkpoints of one run share one append-only record of held identities (`Held`),
    each seeing the first `count`, so a checkpoint per page costs no copy; it pickles as
    a `frozenset` of its own identities. One private runner,
    `OGCService._run(checkpoint)`, serves a fresh query and a resume; it replaces
    `_traverse()`.
- **Why:** D-24 made the resume point exist after every page, since a process can stop
    without an exception, and #14's fan-out leaves several unfinished requests, which
    one `Page` cannot name. Codex and Gemini both reviewed a first version that resumed
    from a held `Page` or `QueryResult` (`91fb915`) and found that its first request was
    sent before the repeat check, that a resume from one page forgot the earlier ones,
    and that a split query lost its unsent pieces. A checkpoint stores the request
    headers exactly, holds no feature, and names every unfinished request. The identity
    includes the body because two CQL2 `POST` pieces can share a URL. Chosen by the
    maintainer after the 2026-10-21 review (`.agents/reviews/2026-10-21/summary.md`).
- **Rejected:** Resuming from a `Page` or `QueryResult` with no checkpoint type (the
    first version), which rebuilt the headers from the body and carried every feature.
    `collect(resume=checkpoint, previous=result)` (Codex), which allows a mismatched
    pair. A `service.resume(result)` method, which adds a method to every service and
    twin without changing the model. A JSON form, which needs a byte encoding for bodies
    and a format contract.
- **Limits:** The server may have changed the collection or expired the cursor in a
    `next` link (D-24), and nothing detects either; an expired cursor fails as a page
    failure with the held pages kept. Pieces run one after another; #14 adds the
    concurrency, and its checkpoints must list every chain in flight. `held` grows by
    one identity a page and is never trimmed. The whole-project review of 2026-10-21
    (Codex, Fable) found that the first refusal rule, no checkpoint after any request
    with attempts 0, dropped the unsent pieces and a retryable closed-transport failure,
    and that a copied `held` per page used 330 MiB for 4,000 kept pages.

### D-53: A response whose link carries a credential fails before it is kept

- **Status:** accepted. Amends D-29, D-34, and D-40.
- **Date:** 2026-10-21.
- **Decision:** `parse_page()` and `item()` raise `ServiceError` when any link object in
    the document or in one of its features carries a credential in its raw `href`
    (query, fragment, or userinfo), checked before `Link` masks it. `_send()` adds the
    service's headers last and drops a request header of the same name in any case, so a
    `Request` that skipped its own checks, such as one unpickled from a tampered file,
    cannot replace the key. The page is never yielded, so its `body` and `content` never
    reach the caller, a `QueryResult`, or a pickle.
- **Why:** D-29 kept `body` and `content` unredacted on the ground that the key never
    travels in a query (D-18), so no server can echo it. L-11 showed a gateway can copy
    the key into a URL, and the 2026-10-21 review (Codex) found that a stamped `next`
    link was refused while the page holding it was yielded and kept. Checking the raw
    `href` also covers a fragment, which `redact_url()` drops before the traversal's own
    check sees it (Gemini).
- **Rejected:** Scrubbing the key out of `body` and `content`, which changes the bytes
    D-23 promises as received and still misses a key elsewhere in the document.
- **Limits:** Only link objects are checked, those of the document and of each feature,
    query, fragment, and userinfo. The whole-project review of 2026-10-21 (Codex) found
    the first version missed feature links and userinfo. A key echoed into a feature
    property, or inside another parameter's value after a stray `?`, still reaches the
    body.

### D-54: Pages that report `numberMatched` beside pages that do not read `unknown`

- **Status:** accepted; amended by D-57. Amends D-36.
- **Date:** 2026-10-21.
- **Decision:** `summarize()` sets `number_matched` only when every page reported the
    same value. When some pages report it and others do not, `number_matched` is `None`,
    the `count` check does not run, and `completeness` is `unknown`.
- **Why:** The first rule took the reporting pages as agreeing, so a resume whose held
    pages reported 10 and whose new pages reported nothing could read `complete` on a
    count no page after the gap confirmed (Gemini, 2026-10-21 review).
- **Rejected:** Running the count check on the reporting pages alone.
- **Limits:** NWIS reports no `numberMatched` on any page, so its results keep only the
    `end_of_links` check and, when every feature has an id, D-57's `unique_ids`.

### D-55: CI pins the pixi version

- **Status:** accepted.
- **Date:** 2026-10-21.
- **Decision:** Every `prefix-dev/setup-pixi` step passes `pixi-version`, now `v0.82.1`.
    A pixi upgrade in CI is a one-line edit per step, made in its own PR.
- **Why:** Without the input, setup-pixi installs the latest pixi on each run, so a
    release changes CI with no commit to show it. pixi 0.82.1 (2026-10-20) is a security
    release that patches `rustls-webpki` and `webpki-root-certs`, and its notes ask
    every user to upgrade at once; a pin makes that upgrade visible and keeps it in
    place. Chosen by the maintainer.
- **Rejected:** Leaving the version unset, which tracks the latest release, including a
    broken or compromised one.
- **Limits:** Dependabot bumps the action's SHA and leaves `pixi-version` alone, so a
    later pixi security release needs a manual bump. Local environments follow whatever
    pixi each contributor installs.

### D-56: NWIS splits a query by calendar period or site list, and pieces run concurrently

- **Status:** accepted; amended by D-57. Amends D-24, D-35, D-44, D-46, and D-52.

- **Date:** 2026-10-21.

- **Decision:** `iter_pages()` and `collect()` take `split_datetime` (`"year"` or
    `"month"`) and `sites_per_piece` (a positive int), one at a time. The base
    `_pieces()` refuses both, and `NWIS._pieces()` splits:

    - `split_datetime` splits the closed `datetime` interval in `params` at calendar
        boundaries in UTC. Each piece ends one microsecond before the next begins
        (`.../2024-12-31T23:59:59.999999Z`, then `2025-01-01T00:00:00Z/...`), every bound
        is written as a UTC timestamp, and a date reads as its midnight. An interval
        inside one period goes whole.
    - `sites_per_piece` splits a comma-separated `monitoring_location_id` parameter, or a
        CQL2-JSON `in` filter such as `ids` sends, into lists of that size. A value listed
        twice goes in one piece.

    Pieces are built before the query check (D-47), so a malformed split sends nothing.
    `_run()` starts pending requests in order, up to the transport's `max_concurrent` at
    once, each following its own `next` links, and yields pages in the order their
    requests finished, which a done callback records. A chain's `next` link starts once
    the caller asks for the page after the one that carried it. A request stays pending
    until its page is yielded, so every checkpoint lists the failed requests, those in
    flight or fetched and not yet yielded, the waiting `next` links, the requests never
    started, and then the refused ones. A request held or already in flight is refused
    as a repeat. After a failure, including one that finished while the caller held a
    page, no new piece starts and every refused request left in the queue is settled
    unsent, so refused requests alone still leave no checkpoint (D-52); the chains in
    flight run to their ends, and one `PaginationError` then names every failure.
    `iter_pages()` closes `_run()` with itself (`contextlib.aclosing`), and closing
    cancels the requests in flight and waits for them.

- **Why:** USGS's efficiency guidance recommends queries near one page, split by year or
    by site and run in parallel (D-35). Splitting is opt-in, since NWIS reports no
    `numberMatched` and a query's size is unknown until it runs: a 20-year `daily` query
    for one site is one page whole and 20 requests split by year. Live probes on
    2026-10-21 showed a date-only bound read as the midnight instant: on `continuous`,
    `2026-09-01/2026-09-01` returned 1 of the day's 288 five-minute values, and a
    `daily` date compares as its midnight. Date-only pieces would drop all but midnight
    of each period's last day, and pieces sharing a bound both return the value at it.
    Feature ids cannot remove that duplicate: the `daily` queryables say an `id` changes
    whenever a record is refreshed. Bounds compare to the microsecond: an end of
    `00:04:59.999999Z` left out 00:05, and a start of `00:00:00.000001Z` left out 00:00,
    so a microsecond gap loses nothing. The first version's one-second gap lost a value
    with fractional seconds in a probe of the 2026-10-21 review (Codex). Two keywords
    name their units; `split=100` reads as a piece count. Stopping new pieces after a
    failure caps what an outage or a bad key costs at `max_concurrent` chains, and the
    checkpoint keeps the rest. Starting a `next` link only when the caller asks keeps an
    unsplit query at one request per step (D-46). The keywords and the failure rule were
    chosen by the maintainer after advice from Codex. The whole-project review of
    2026-10-21 (Codex, Fable) also found a piece starting after a failure that finished
    between steps, pages leaving their arrival order, and a checkpoint with two refused
    requests that resumed forever; each fix has a test that fails without it.

- **Rejected:** Splitting every query with an interval or a site list, which multiplies
    the requests of small queries. One `split` keyword taking a period or an int.
    Caller-supplied pieces. Shared inclusive bounds with de-duplication by feature id,
    which ids that change on refresh defeat and which leaves page counts and bytes
    disagreeing with the features. CQL2 half-open time predicates (Codex), unverified on
    NWIS and composed with the caller's own filter. Fetching a chain's next page while
    the caller holds the current one (D-46). Starting every piece after a failure. A
    task group, which cannot span the blocking API's steps (D-46).

- **Limits:** A time between a piece's end and the next start would need a precision
    finer than the microsecond, which NWIS bounds cannot express. A piece need not be
    near one page: a year of 5-minute `continuous` values is about 105,000 rows, so
    `"month"` suits them. Feature order varies between runs. `summarize()` reads the
    pieces as one traversal (D-44), and with no `numberMatched` on NWIS only
    `end_of_links` and D-57's `unique_ids` apply. A long `monitoring_location_id` list
    can still exceed the GET URL cap near 8 KB; `ids` sends it in a POST body. Pieces
    run against a collection that may change between them (D-35). A blocking iterator's
    `close()`, like dropping it, leaves its async generator to the loop's finalizer, so
    its pieces are cancelled after `close()` returns, and later still when a held
    traceback keeps a dropped iterator alive (D-46).

### D-57: A repeated feature id reads `unknown`

- **Status:** accepted. Amends D-36, D-54, and D-56.
- **Date:** 2026-10-23.
- **Decision:** `summarize()` reads the `id` of every feature `collect()` received. When
    every feature has a string or integer `id` and none repeats, `Pagination.checks`
    lists `unique_ids`. A repeated `id` makes `completeness` `unknown`. A feature with
    no `id`, or with a float, boolean, or other `id`, skips the check. Ids compare with
    their type, so `"1"` and `1` differ.
- **Why:** Chosen by the maintainer. On an offset service, a feature inserted before the
    current offset shifts the last feature of one page onto the next. An insert plus a
    delete leaves `numberMatched` and the count as they were, so the count check passes
    on a result that holds a duplicate (D-03). On NWIS, which reports no
    `numberMatched`, the repeat is the only witness. NWIS `daily` ids change when a
    record is refreshed (D-56), which can hide a repeat but never invents one.
- **Rejected:** Dropping the repeated feature, which D-56 rejects for ids that change on
    refresh and which leaves page counts disagreeing with the features.
- **Limits:** A deleted feature that no page holds leaves no id to repeat, so only a
    `numberMatched` change reveals it. `iter_pages()` yields pages without the check.
    The check holds every id of the result in memory, beside the features themselves.

### D-58: Frame conversion returns an Arrow table

- **Status:** accepted; amended by D-59 and D-60. Supersedes D-25.
- **Date:** 2026-10-23.
- **Decision:** `QueryResult.to_arrow()` returns a `pyarrow.Table`. pyarrow and shapely
    come with the `arrow` extra, and only the method imports them (D-09). Each property
    becomes a column, typed by pyarrow across all features. When any feature has an
    `id`, an `id` column comes first and holds it; a property `id` that is null or equal
    to it is dropped, and any other raises. When any feature has a geometry, a
    `geometry` column holds each as WKB, tagged as a GeoArrow `geoarrow.wkb` extension
    whose metadata names CRS84, and a `crs.effective` other than CRS84 raises, as does a
    property named `geometry`. Without any geometry, no `geometry` column is added and
    neither check runs.
- **Why:** Chosen by the maintainer, who weighs polars and DuckDB as successors to
    pandas. Arrow is the interchange they all read: `geopandas.GeoDataFrame.from_arrow`
    takes the GeoArrow column with its CRS, `polars.from_arrow` keeps it as an extension
    type, and `Table.to_pandas()` gives pandas, so the package leaves the frame library
    to the caller. GeoArrow reads coordinates as x/y, and an OGC server answering a
    request for a latitude-first CRS such as EPSG:4326 sends latitude first (OGC API
    Features Part 2); telling the two apart takes pyproj, so only CRS84, which all three
    services send by default, converts. Raising for any other CRS extends the
    maintainer's call to raise on an unsettled one. Research of 2026-10-23: polars
    geometry lives in `polars-st` (one maintainer, LGPL) or an unreleased GeoPolars
    rewrite, and reaching geopandas from polars needs pyarrow anyway. FabricData sends a
    property `id` of null beside each feature id, so the feature id fills the column.
- **Rejected:** D-25's helper returning a GeoDataFrame or DataFrame, which ties callers
    to pandas. A polars frame with `polars-st` geometry. `pa.Table.from_pylist`, which
    infers the schema from the first row and drops a property only later features carry.
- **Limits:** pyarrow is 28 to 54 MB per platform. A column whose values pyarrow cannot
    combine, such as an `id` or property that is an integer in one feature and a string
    in another, raises `pyarrow.ArrowException`. conda-forge packages have no extras, so
    a conda user installs pyarrow and shapely alongside. A query that asks for another
    CRS does not convert; convert the CRS84 result and reproject the frame. shapely 2.0
    (GEOS 3.11) rejects a third coordinate in GeoJSON, so the extra needs shapely 2.1.
    D-59 sets the development lock's macOS floor.

### D-59: Python 3.13 is the floor, and the lock covers three platforms

- **Status:** accepted. Amends D-58.
- **Date:** 2026-10-23.
- **Decision:** `requires-python` is `>=3.13`. The offline tests run in `test313` and
    `test315`, ruff targets `py313`, and pyright checks as 3.13. The `dev`, `typecheck`,
    and `docs` environments run 3.15 and `lint` runs 3.13, so no environment uses 3.14.
    `pixi r audit` resolves for the `lint` Python, and 3.13 installs backports that 3.15
    skips, such as `typing-extensions` under anyio. Python 3.15 comes from the main
    conda-forge channel. The pixi platforms are `linux-64`, `osx-arm64` (macOS 15 or
    later), and `win-64`.
- **Why:** Chosen by the maintainer once Python 3.15.0 was final, which also retires the
    `python_rc` channel and the typing-extensions pin it needed. Intel Macs are out of
    the development lock by the maintainer's call, and pyproj's Intel wheel needs macOS
    15 anyway. The macOS floor is the oldest that every wheel in the lock supports:
    rasterio, under contextily in `dev` for the example maps' basemaps, ships Apple
    silicon wheels for macOS 15 and later, and pyproj for 14; below that pixi builds
    their sdists, which need GDAL and PROJ.
- **Limits:** The package is pure Python, so `pip` installs it on an Intel Mac; only the
    pixi environments skip it. CI runs on Ubuntu alone, so the `osx-arm64` and `win-64`
    lock entries are solved and never installed there. `_transport.py` still catches
    `OverflowError`: probes on 2026-10-23 found `parsedate_to_datetime` raising it for
    an HTTP-date with a huge year on 3.13.0 through 3.13.13 and on 3.14.0, and
    `ValueError` on 3.13.17 and 3.15.

### D-60: `to_arrow()` takes any CRS pyproj reads and writes it as GeoArrow expects

- **Status:** accepted. Amends D-58.
- **Date:** 2026-10-23.
- **Decision:** The `arrow` extra adds pyproj. When features have geometry, `to_arrow()`
    reads `crs.effective` with `pyproj.CRS.from_user_input` and raises when it is `None`
    or pyproj cannot read it. The `geometry` column's metadata gives the CRS as PROJJSON
    (`CRS.to_json_dict()`), with no `crs_type`. When the CRS's first axis is named
    latitude, northing, or southing, as EPSG:4326's is, the first two coordinates of
    every vertex are swapped (`shapely.transform(..., include_z=None)`, which keeps a
    third). The WKB is ISO-flavored.
- **Why:** Chosen by the maintainer after reading geopandas 1.2's `to_arrow` and
    `from_arrow` and the GeoArrow 0.2 spec. The spec asks producers for PROJJSON ("for
    maximum compatibility") and ISO WKB, and reads coordinates as (longitude, latitude)
    or (easting, northing) whatever the CRS declares, while OGC API Features Part 2
    sends them in the CRS's axis order. geopandas writes the same PROJJSON and ISO WKB,
    so a CRS84 table's metadata now matches its own export. Live on 2026-10-23: a `crs`
    request for OGC CRS83 on NWIS and EPSG:3857 on GeoConnex `gages` came back with
    those CRSs in `Content-Crs`, longitude or easting first, and pyproj reads both URLs
    with an east-pointing first axis, so neither is swapped.
- **Rejected:** Keeping CRS84 only with a fixed PROJJSON constant, which needs no pyproj
    but refuses every other CRS a service offers. Swapping on the first axis's
    direction, which also swaps polar stereographic eastings that point north or south
    (EPSG:3031, EPSG:3413; Codex review of 2026-10-23).
- **Limits:** The swap trusts a server to follow Part 2's axis order. None of the three
    services advertises a latitude-first CRS: their collections list CRS84, CRS83,
    CRS27, and, on GeoConnex `gages`, EPSG:3857, so the swap is pinned by offline tests
    only. pyproj adds about 5 to 10 MB to the extra.

### D-61: Pages decode with `json.loads`

- **Status:** accepted. Answers Q-09.
- **Date:** 2026-10-26.
- **Decision:** Pages keep decoding with `json.loads`, and no extra adds orjson. orjson
    is a dev dependency for `scripts/bench.py --decode` only.
- **Why:** The bar, set before the run: adopt orjson only if `json.loads` takes at least
    10% of a page's wall time and orjson cuts that wall time by at least 5%. Three live
    NWIS `continuous` pages of 10,000 features (6.8 MiB each) decoded in 19 ms with
    `json.loads` and 9 ms with orjson, and three FabricData `gagesii` pages of 1,000
    (0.9 MiB) in 2 ms and 1 ms (medians of 20). `iter_pages` returned the NWIS pages in
    1.5 to 3.6 s and the FabricData pages in 0.5 to 1.0 s, its own `json.loads`
    included, so decoding was 0.5 to 1.3% of an NWIS page's time and 0.2 to 0.5% of a
    FabricData page's, and orjson would save under 1%. orjson also grows peak RSS more:
    31 MiB against 24 MiB per NWIS page, 3.6 against 2.6 to 2.7 MiB per FabricData page.
    Both decoders returned equal documents for all six pages.
    [Performance](performance.md#decoding) has the table. Predicted before the run:
    orjson two to three times faster, lower peak memory, decoding 1 to 4% of a page's
    time, and a verdict to keep `json.loads`. The speedup and the verdict held; the
    share came in lower, and orjson's memory went the other way.
- **Rejected:** An optional orjson extra, which makes a page carrying `NaN` parse or
    fail by environment (Q-09) for under 1% of wall time.
- **Limits:** One machine (Apple M2 Max) and one fetch per page. The saving is the
    difference of the two decoders on the same bytes, with no end-to-end run decoding
    through orjson: NWIS pages of one size came back in 1.5 to 3.6 s, a spread that
    would hide a 10 ms difference. Page times varied sevenfold, and the fastest still
    left decoding under 2%. A source that serves pages in milliseconds, such as a local
    cache, would change the share; measure again before adopting then.

### D-62: The release security gate

- **Status:** accepted. Answers Q-08.
- **Date:** 2026-10-26.
- **Decision:** Three checks guard the USGS key before a release. CI runs
    `pixi r audit`, which resolves the runtime dependencies and the `arrow` extra (D-58)
    from PyPI and fails on any advisory rated low or above. The `key-shaped-string`
    pre-commit hook fails lint on committed text holding a token shaped like a USGS key.
    A security review of key handling, redaction, logging, and redirects runs before the
    first release, and again on any change to the key, redaction, or redirects, as
    `REVIEW.md` sets for its top tier.
- **Why:** The review for #43 read all of `src/` against `REVIEW.md`'s first failure and
    probed it offline. A redirect whose `Location` holds the key leaves no key in the
    `str`, `repr`, formatted traceback, or pickle of the error; an `http://` `next` link
    is refused with the key sent only to the HTTPS origin; and with the root, `httpx2`,
    `httpcore2`, and `water_ogcapi` loggers at level 1 no record holds the key. It found
    one leak: a pickled `NWIS` held the key, which a pickle now leaves out (D-42). The
    same review found that `scripts/bench.py --fetch` sent the key to any `next` link
    and could write a page echoing it; it now fetches through the services and checks
    each page before writing.
- **Rejected:** A key pattern that also matches a key glued to `/`, `-`, or `_`, which
    matches base64 images in notebook outputs.
- **Limits:** The hook misses an encoded key, a key of another shape, and a key glued to
    `/`, `-`, or `_`. Every error raised in `Transport.get` keeps a traceback frame
    whose `headers` local holds the caller's key, so an error reporter that captures
    frame locals needs its own scrubbing. A caller who sets the `hpack` logger to DEBUG
    logs the key (D-20), and offline probes skip the HTTP/2 encoder, so that layer rests
    on reading. Redaction works by parameter name, and a key echoed in a URL path stays
    (D-29, D-53). Recorded fixtures carry the same risk as `QueryResult`, so scrub auth
    at record time.

### D-63: Weekly drift run against the live services

- **Status:** accepted. Answers Q-05.
- **Date:** 2026-10-27.
- **Decision:** `.github/workflows/drift.yml` runs every Monday at 07:00 UTC and on
    request. Its `network-tests` job runs the network-marked tests with the
    `USGS_API_KEY` repository secret, a key used nowhere else. Its `openapi` job runs
    `scripts/openapi_drift.py` (`pixi r -e test315 drift`), which fetches each service's
    OpenAPI document with no key, keeps the whole document minus its prose
    (descriptions, summaries, titles, and examples) with `info` cut to the title and
    version, and fails on any difference from `tests/fixtures/<service>/openapi.json` or
    on a missing snapshot. `--update` rewrites the snapshots with the day's date;
    `tests/fixtures/README.md` says when to run it. The workflow has `contents: read`
    and no `pull_request` trigger, so no fork run sees the secret, and `ci.yml` stays
    offline (D-14). A failed run reaches the maintainer through GitHub's notification
    and files nothing. Release notes name the service version each snapshot carries, the
    `info.version` field of each `tests/fixtures/<service>/openapi.json`. A service that
    does not answer fails the run and keeps its snapshot.
- **Why:** D-14 made drift invisible: fixtures stay green and live tests stay out of CI.
    A weekly run turns an upstream change into a failed run within seven days and spends
    a handful of requests. The snapshot keeps everything except prose, so a wording edit
    does not fail the run, while a new or removed collection path, a changed `limit`
    maximum (D-30's caps), a changed parameter or request body, or a version bump does.
    Keeping everything else, in place of a list of fields, was chosen after two review
    rounds each found a dropped field the client depends on. The documents are public,
    so the fetch sends no key and cannot copy one into a snapshot (D-13). Fail-only was
    chosen by the maintainer.
- **Rejected:** Committing the full documents, about 3 MB that is mostly descriptions,
    where every wording edit is drift. Running the network tests in `ci.yml`, which
    hands the secret to every push and reaches USGS on each one. Filing an issue from
    the workflow, which needs `issues: write` and a step that recognizes a drift already
    filed.
- **Limits:** GitHub disables a scheduled workflow after 60 days without a commit on the
    default branch, and runs it late or not at all under load. CloudFront served the
    NWIS document from cache (`x-cache: Hit`), so a change can take a cache lifetime to
    reach the run. The snapshot misses a change in a description that changes meaning,
    and it keeps a `$ref` as the reference, so a change inside a referenced document
    (the OGC `sortby` parameter, the CQL2 schema) is invisible. The documents declare
    the `POST` body as `application/json` while the client sends
    `application/query-cql-json` (D-39), which the servers accept; the snapshot guards
    the declaration, and the GeoConnex `ids` network test guards the acceptance. Each
    job stops after 20 minutes. A FabricData outage skips its network test (D-14) and
    fails its OpenAPI fetch. The planted-diff check ran offline against a mocked service
    and once live against an edited snapshot, never through the scheduled run itself.

### D-64: A pickled service keeps its settings and rebuilds its per-process state

- **Status:** accepted. Amends D-42.
- **Date:** 2026-10-28.
- **Decision:** A pickle of a `Transport`, a `Governor`, or an async service leaves out
    the state that belongs to the process that made it: the transport's HTTP client,
    semaphore, and event loop; the governor's start times and rate-limit readings; and
    the service's metadata cache and its locks with their loop. Each is rebuilt on first
    use. `copy.deepcopy` goes through the same hooks and drops the same state. A
    `copy.copy` of an async service drops only its own cache and locks and shares the
    original's transport and governor, loop binding and readings included (checked by
    probe). A blocking service pickles its async service the same way, and services
    pickled together keep sharing one transport. So a service that has run queries loads
    in another process, such as a Dask or `multiprocessing` worker, and `NWIS` reads its
    key there as D-42 sets.
- **Why:** The client, semaphore, and locks are bound to an event loop, which does not
    pickle, so a service holding them raised on pickle (D-42) and a worker had to build
    its own. The cache entries and governor readings hold `time.monotonic()` times,
    which another host's clock does not share: a 60 s hold read on a host with a shorter
    uptime would stall its first request for the difference.
- **Rejected:** Carrying the cache with wall-clock times, which saves each worker one or
    two metadata requests. Rebuilding the state in `__setstate__`, which would create
    the client outside a running loop.
- **Limits:** Each loaded copy fetches its own metadata and starts at the static
    interval until a response carries counts (D-49), so several workers pace
    independently against one quota. A transport shared by several services stays shared
    only among the services of one pickle. An `httpx2` transport the caller injects
    travels as built, with whatever it holds: a mock handler's recorded requests carry
    their headers, the key included, and a connection pool does not pickle. A service
    pickle names private module paths that D-51 does not pin, so it loads in a worker
    running the same release. A `fork` child that inherits a used service unpickled, as
    a `multiprocessing.Process` argument does under the fork start method, raises D-10's
    `RuntimeError` on its first request (checked by probe); pass the service where it is
    pickled, such as a `Pool.map` argument or a Dask task, or build it in the worker. A
    worker whose `USGS_API_KEY` is not printable ASCII raises `ValidationError` at load.

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

### L-12: The first tag failed at the upload check

- **What happened:** `v0.1.0rc1` built and got its GitHub pre-release, then the upload
    failed. `pypa/gh-action-pypi-publish` v1.14.0, pinned from a template, checks
    distributions with Twine 6.1.0 and packaging 25.0, which reject the
    `Metadata-Version: 2.5` that hatchling writes. v1.14.2 bundles Twine 7.0.0 and
    packaging 26.2, which accept it. Nothing before the tag ran the action's own check.
- **Rule:** Pin release tooling to its latest release, and exercise the upload path on a
    local build before the first tag. D-32 now uploads with `uv publish`, the tool that
    built the distributions.
- **Check:** `uv publish --dry-run --trusted-publishing never --token dummy dist/*` on a
    local `uv build`.

### L-13: A check read the caller's params while the request sent another reading

- **What happened:** `iter_pages()` built its request from a copy of `params`, then
    passed the caller's mapping to `_validate()`, which read its names after awaiting
    the collection index. A task that swapped `{"typo": "x"}` for a queryable's name
    during that await had the new names checked and `typo` sent, and the query read
    `complete` (D-47). The review of that fix found the same class in
    `Transport.request()`: it checked the caller's `params` for a credential, then read
    them again after each pacing and backoff wait, so an `api_key` added during a retry
    wait reached the URL (D-18). The alignment review found it in `Request` too, with no
    await: a mapping read once for the credential check and again for the stored copy
    could name `api_key` only the second time, and a checkpoint kept it (D-52).
- **Rule:** A check and every send read one private copy, taken before the check.
- **Check:** `test_params_changed_during_the_check_are_checked_as_sent`,
    `test_params_changed_during_a_retry_wait_are_sent_as_checked`, and
    `test_a_request_stores_the_params_it_checked`.

## Open questions

### Q-01: How frame conversion ships

**Status:** answered by D-25, which D-58 supersedes.

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

**Status:** answered by D-63.

USGS's versioning page says v0 is still under development and its interface and data may
change, while aiming not to break working queries. Proposal: a scheduled weekly CI run
of the network tests, plus a committed OpenAPI snapshot diffed on a schedule. A release
should also name the API snapshot it supports.

### Q-06: Versioning policy

**Status:** answered by D-21.

0.x semantics, what 1.0 means relative to USGS stabilizing the API, and a deprecation
policy.

### Q-07: Unverified API facts

**Status:** answered by D-30.

Do not state these as fact until a live response confirms them:

- The NWIS auth header. USGS docs name an `X-Api-Key` header or an `api_key` query
    parameter; an earlier note said a lowercase `api_key` header.
- The rate-limit tiers, and whether `Retry-After` accompanies a 429.
- Which services send `X-RateLimit-*` headers.
- The page-size caps in D-13.

### Q-08: Release security gate

**Status:** answered by D-62.

Partly built. No log record carries request headers (D-20). CI runs `pixi r audit`,
which resolves the runtime dependencies and the `arrow` extra (D-58) from PyPI with uv,
through `scripts/audit-requirements.txt`, and checks them with pysentry against the
PyPA, PyPI, and OSV advisories. Without uv, pysentry audits only the direct dependencies
at their lower bounds, so `uv` stays in the `lint` environment. The `key-shaped-string`
pre-commit hook fails lint on any committed text holding a token shaped like a USGS key:
40 letters and digits mixing upper case, lower case, and digits. It misses an encoded
key, a key of another shape, and a key glued to `/`, `-`, or `_`: that boundary keeps
base64 images in notebook outputs from matching. Left to review: env-var key handling,
the redaction invariant (D-04), and redirect behavior (D-12). Every error raised in
`Transport.get` keeps a traceback frame whose `headers` local holds the caller's key, so
an error reporter that captures frame locals needs its own scrubbing. Recorded fixtures
carry the same credential risk as `QueryResult`, so scrub auth at record time.

### Q-09: orjson for decoding large responses

**Status:** answered by D-61.

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
