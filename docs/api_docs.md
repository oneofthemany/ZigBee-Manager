# API Documentation Explorer

Interactive route explorer for ZMM, served at **`/api-docs`**.

Lists every HTTP route on the FastAPI app, grouped by tag or URL prefix,
with a "try it" form for each one so you can exercise endpoints without
leaving the browser.

---

## The three endpoints

| Endpoint | Returns | Use it for |
|---|---|---|
| `/api-docs` | Interactive HTML page | Day-to-day route browsing and testing |
| `/routes` | Plain HTML, dark-themed | Quick eyeballing of the route table |
| `/api/routes` | JSON: `{ groupName: [{method, path}, ...] }` | Programmatic consumers |

---

## Using the explorer

Open `http://<host>:8000/api-docs` in a browser. The page has two panes:

**Left pane — sidebar**

- Groups are collapsed by default; click a header to expand
- **Expand all** / **Collapse all** buttons toggle every group at once
- Search box filters routes by path or HTTP method
- Each group header shows its route count, e.g. `CONFIG (12)`

**Right pane — endpoint detail**

Click any route in the sidebar to see:

- Method badge and full path
- Description (if available)
- Parameters table (if available)
- A **Try it out** form
- Response viewer with status code, timing, and pretty-printed JSON

For `GET` routes, the form accepts a query string (`?foo=bar`).
For `POST` / `PUT` / `PATCH` / `DELETE`, it accepts a JSON body.
The page sends the request with `Content-Type: application/json` automatically.

---

## How routes are grouped

ZMM uses two route registration patterns. Grouping priority:

1. **Tags** — modules using `APIRouter(tags=[...])` get grouped by their first
   tag. Example: `tags=["setup"]` → group `setup`.
2. **URL prefix** — bare `@app.get("/api/<group>/...")` routes get grouped
   by the segment after `/api/`. Example: `/api/config/structured` → group `config`.
3. **Catch-all** — root pages like `/` go into `general`.

Hidden from the docs (still callable, just not listed):

- Anything under `/static/` or `/ws`
- FastAPI's own `/openapi.json`, `/docs`, `/redoc`
- HTTP `HEAD` and `OPTIONS` methods

---

## Adding new routes

Nothing to do — the explorer reads the live FastAPI route table, so any new
endpoint shows up automatically after a restart.

If you want a route to land in a specific group, set `tags=` on the
`APIRouter`:

```python
router = APIRouter(prefix="/api/myfeature", tags=["myfeature"])
```

Otherwise it groups by URL prefix automatically.

---

## Hand-curated descriptions (optional)

The `routeMetadata` object at the top of `static/js/api-docs.js` is a lookup
table for richer route descriptions. Format:

```javascript
'/api/setup/status': {
    description: 'Check if the dongle setup wizard should be shown.',
    params: [
        { name: 'foo', type: 'string', required: true, description: '...' }
    ],
    returns: '{ needs_setup: bool, reason: string }',
    example: null   // or { foo: 'bar' } for POST bodies, or '?foo=bar' for GET
}
```

Routes without an entry still appear in the sidebar and still get a working
try-it form — the metadata table only enriches the description pane.

---
