# Response fixtures

Live responses from the three services, recorded on 2026-10-08 by
`scripts/record_fixtures.py`. Each file is one response:

```json
{
  "recorded": "2026-10-08",
  "request": {
    "url": "<the request as sent>",
    "params": {
      "f": "json"
    }
  },
  "response": {
    "status": 200,
    "headers": {},
    "body": {}
  }
}
```

`params` is what the recorder passed; a page reached by a `next` link has none, since
its query sits in `url`. `body` is the decoded JSON document. `headers` keeps every
response header except `content-encoding`, `content-length`, and `set-cookie`, since the
first two describe the compressed bytes on the wire. No request header is stored.

| Directory     | Host                                 | Key sent | `X-RateLimit-*` headers   |
| ------------- | ------------------------------------ | -------- | ------------------------- |
| `nwis/`       | `api.waterdata.usgs.gov/ogcapi/v1`   | yes      | 4000 metadata, 1000 items |
| `fabricdata/` | `api.water.usgs.gov/fabric/pygeoapi` | no       | 300                       |
| `geoconnex/`  | `reference.geoconnex.us`             | no       | none                      |

A name ending in `-N` is page N of one traversal, each page fetched from the previous
page's `next` link. `items` pages follow the server's default order and `sorted` pages
add `sortby`. NWIS `next` links carry a `cursor` and no response reports
`numberMatched`; FabricData and GeoConnex links carry `offset` and report it.

## Re-recording

Follow `.agents/skills/live-api/SKILL.md`. The script sends about 40 requests with
`max_retries=0`, sends the key only to NWIS, and replaces the key's value in anything it
writes. Then run the skill's key scan and commit the changed files with their new
`recorded` date.

```bash
pixi r -e dev python scripts/record_fixtures.py
```
