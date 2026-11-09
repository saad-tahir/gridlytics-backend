"""HTTP API of Gridlytics: the guard, the refusal shapes, health, the sign-in state, the users routes and the mounting of every feature's routes.

The guard is cut down from solarlytics-backend at c853130 (api.py lines 97-560, 705-720, 1183-1185, 4869-4942, 5007-5076, 6786-6835; plan Part 10.2): hosts,
CORS, security headers, body limits, JSON-only changes, the rate limits, the sign-in token or a site-bound API key, the company the person works in and its
workspace file. Every feature lives in a `routes_<feature>.py` module mounted at the end; this file never grows by accretion.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, apikeys, auth, backups, companies, identity, money, store, store_members, version
from . import platform as platformdb
from .schemas import Refusal

log = logging.getLogger("gridlytics")
if not log.handlers and os.environ.get("GRIDLYTICS_LOGS", "1") == "1":
    try:
        _logs = store.DB_PATH.parent / "logs"
        _logs.mkdir(parents=True, exist_ok=True)
        _handler = RotatingFileHandler(_logs / "gridlytics.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(_handler)
        log.setLevel(logging.INFO)
    except OSError:
        pass


@asynccontextmanager
async def _lifespan(app_):
    """Start the daily backups and the scheduler (only with GRIDLYTICS_SCHEDULER=1, which the launcher sets and the tests never do)."""
    if os.environ.get("GRIDLYTICS_DAILY_BACKUP") == "1":
        backups.start_daily()
    from . import scheduler
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


_DOCS = os.environ.get("GRIDLYTICS_DOCS") == "1"
app = FastAPI(title="Gridlytics API", version=__version__, docs_url="/docs" if _DOCS else None, redoc_url=None,
              openapi_url="/openapi.json" if _DOCS else None, lifespan=_lifespan)


def _host_names(extra: str) -> set[str]:
    return {h.strip().lower().rstrip(".") for h in ("127.0.0.1", "localhost", "::1", "[::1]", *extra.split(",")) if h.strip()}


ALLOWED_HOSTS = _host_names(os.environ.get("GRIDLYTICS_HOSTS", ""))
identity.check_startup(ALLOWED_HOSTS)
_ORIGIN = re.compile(r"https?://([a-z0-9.-]+|\[[0-9a-f:.]+\])(:\d{1,5})?")


def _cors_origins(raw: str) -> list[str]:
    raw = raw.strip()
    names = json.loads(raw) if raw.startswith("[") else (raw.split(",") if raw else [])
    out = []
    for name in (str(n).strip().rstrip("/").lower() for n in names):
        if name and not _ORIGIN.fullmatch(name):
            raise RuntimeError(f"GRIDLYTICS_CORS_ORIGINS names {name[:80]!r}, which is not an origin (https://host or https://host:port)")
        if name:
            out.append(name)
    return out


CORS_ORIGINS = _cors_origins(os.environ.get("GRIDLYTICS_CORS_ORIGINS", ""))
_regex = os.environ.get("GRIDLYTICS_CORS_ORIGIN_REGEX", "").strip()
CORS_ORIGIN_REGEX = _regex or None
# Idempotency-Key: a client may send a first save's key as a header as well as in the body (the web app sends it in the body)
CORS_ALLOW_HEADERS = ["Authorization", "Content-Type", "X-Gridlytics-Company", "X-Request-ID", "x-founder-session", "Idempotency-Key"]
CORS_EXPOSE_HEADERS = ["X-Request-ID", "X-Total-Count", "X-Gridlytics-Engine", "X-Gridlytics-Role", "X-API-Version", "Content-Disposition", "Retry-After"]
_HOST_HEADER = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(:\d{1,5})?")

ERROR_CODES = {400: "bad_request", 401: "sign_in_required", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 409: "conflict", 413: "too_large",
               410: "gone", 415: "must_be_json", 422: "invalid", 429: "too_many_requests", 500: "server_error", 502: "upstream_failed", 503: "unavailable"}
SIGN_IN_CODES = {"token_invalid": 401, "token_expired": 401, "session_ended": 401, "invalid_credentials": 401, "not_a_member": 403, "no_company": 403,
                 "company_disabled": 403, "member_disabled": 403, "no_seat": 403, "company_required": 409, "session_renewed": 409, "identity_unavailable": 503}
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; script-src 'self'"}
MAX_BODY = 25_000_000
_BODY_METHODS = ("POST", "PUT", "DELETE", "PATCH")
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def _error_body(status: int, detail, request_id: str, code: str | None = None) -> dict:
    return {"detail": detail, "code": code or ERROR_CODES.get(status, "error"), "request_id": request_id}


def _refusal(request_id: str, status: int, detail, code: str | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(_error_body(status, detail, request_id, code), status_code=status, headers={"X-Request-ID": request_id, **(headers or {})})


def _too_many(rid: str, wait: int, who: str) -> JSONResponse:
    return _refusal(rid, 429, f"Too many requests {who}; try again in {wait} second{'s' if wait != 1 else ''}", "too_many_requests", {"Retry-After": str(wait)})


def _fail(message: str, code: str | None = None):
    """A 422 with the message a person reads (the lifted route modules raise through this)."""
    raise Refusal(422, message, code)


PUBLIC_PATHS = {"/api/health", "/api/auth/state"}
PUBLIC_POSTS: set[str] = set()
LOCAL_POSTS = {"/api/local/session", "/api/local/refresh", "/api/local/logout"}
if identity.MODE == "local":
    PUBLIC_POSTS |= LOCAL_POSTS
IDENTITY_PATHS = {"/api/auth/state", "/api/local/context", "/api/local/password", "/api/local/logout", "/api/local/refresh", "/api/local/sessions",
                  "/api/local/sessions/end", "/api/local/sessions/end-others"}
FOUNDER_PREFIX = "/api/founder/"
#: A machine key reaches only its own site: the path's site id, when the path names one, must be the key's (plan Part 10.5)
_SITE_IN_PATH = re.compile(r"^/api/(?:sites|edge)/([^/]+)")
#: The ingest paths, whose rate budget is per key (DAT-03), not per address
_INGEST = re.compile(r"^/api/edge/")


@app.middleware("http")
async def guard(request, call_next):
    started = time.perf_counter()
    sent = request.headers.get("x-request-id") or ""
    rid = sent if _REQUEST_ID.fullmatch(sent) else uuid.uuid4().hex[:16]
    request.state.request_id = rid
    request.state.user_id = ""
    request.state.company_id = ""
    request.state.identity = None
    request.state.company_refusal = None
    response = await _guarded(request, call_next, rid)
    response.headers["X-Request-ID"] = rid
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("X-API-Version", "1")
        response.headers.setdefault("X-Gridlytics-Engine", version.stamp())
    if request.method != "GET" or response.status_code >= 400:
        log.info("request %s %s %s -> %s in %.0f ms%s", rid, request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000.0,
                 f" user {request.state.user_id}" if request.state.user_id else "")
    return response


def _origin_allowed(origin: str) -> bool:
    from urllib.parse import urlparse
    try:
        host = urlparse(origin).hostname or ""
    except ValueError:
        return False
    if host.rstrip(".") in {h.strip("[]") for h in ALLOWED_HOSTS}:
        return True
    return origin in CORS_ORIGINS or (CORS_ORIGIN_REGEX is not None and re.fullmatch(CORS_ORIGIN_REGEX, origin) is not None)


def _enter(user: dict | None, org: str | None, company: dict | None, db_file) -> tuple:
    return (auth.current.set(user), auth.company.set(company), store.set_company(db_file) if db_file is not None else None,
            store.set_org(org or store.DEFAULT_ORG), store.set_actor(user["id"], user.get("email") or "") if user else None)


def _leave(markers: tuple) -> None:
    current, company, db_file, org, actor = markers
    if actor is not None:
        store.reset_actor(actor)
    store.reset_org(org)
    if db_file is not None:
        store.reset_company(db_file)
    auth.company.reset(company)
    auth.current.reset(current)


#: A 60-second cache of an API key's company (plan Part 10.6): a batch a minute per site never re-reads platform.db. Only keys that were found are kept (a
#: made-up key would otherwise grow it by one entry per request), and it is pruned of entries past their minute when it holds _KEY_CACHE_MAX
_KEY_CACHE: dict[str, tuple[float, dict]] = {}
_KEY_CACHE_LOCK = threading.Lock()
_KEY_CACHE_TTL = 60
_KEY_CACHE_MAX = 10_000


def _key_company(digest: str):
    now = time.monotonic()
    with _KEY_CACHE_LOCK:
        hit = _KEY_CACHE.get(digest)
        if hit and now - hit[0] < _KEY_CACHE_TTL:
            return hit[1]
    held = platformdb.index_find("api_key_index", digest)
    row = (platformdb.company(held) or False) if held is not None else None
    with _KEY_CACHE_LOCK:
        if row:
            if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
                for old in [d for d, (at, _) in _KEY_CACHE.items() if now - at >= _KEY_CACHE_TTL]:
                    del _KEY_CACHE[old]
                if len(_KEY_CACHE) >= _KEY_CACHE_MAX:
                    _KEY_CACHE.clear()
            _KEY_CACHE[digest] = (now, row)
        else:
            _KEY_CACHE.pop(digest, None)
    return row


async def _guarded(request, call_next, rid: str):
    raw = request.headers.get("host") or ""
    if not _HOST_HEADER.fullmatch(raw):
        return _refusal(rid, 400, "Unknown host", "unknown_host")
    host = raw.rsplit(":", 1)[0] if not raw.startswith("[") else raw.split("]")[0] + "]"
    if host.lower().rstrip(".") not in ALLOWED_HOSTS:
        return _refusal(rid, 400, "Unknown host", "unknown_host")
    origin = request.headers.get("origin")
    if origin and not _origin_allowed(origin):
        return _refusal(rid, 403, "Cross-site request refused", "cross_site")
    path = request.url.path
    founder = path.startswith(FOUNDER_PREFIX)
    if request.method in _BODY_METHODS and path.startswith("/api/"):
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        bodiless = request.method in ("DELETE", "POST") and not ctype and int(request.headers.get("content-length") or 0) == 0 \
            and "transfer-encoding" not in request.headers
        multipart = ctype in ("multipart/form-data", "text/csv", "application/octet-stream") and path.endswith("/upload")
        if ctype != "application/json" and not ctype.endswith("+json") and not bodiless and not multipart:
            return _refusal(rid, 415, "Requests that change anything must be JSON", "must_be_json")
        if request.scope.get("body_too_large") or int(request.headers.get("content-length") or 0) > MAX_BODY:
            return _refusal(rid, 413, "The request is too large")
    address = request.client.host if request.client else ""
    api_call = path.startswith("/api/") and path != "/api/health"
    bearer = request.headers.get("authorization") or ""
    has_bearer = path.startswith("/api/") and not founder and bearer[:7].lower() == "bearer "
    token = bearer[7:].strip() if has_bearer else ""
    keyed = has_bearer and token.startswith(apikeys.PREFIX)
    ingest = bool(_INGEST.match(path))
    # the ingest paths' budget is per key (DAT-03): a key skips the address limiter there only once it is found (below), never on its shape alone
    if api_call and not (keyed and ingest) and (wait := auth.rate_wait("address", address)):
        return _too_many(rid, wait, "from this address")
    if ingest and not keyed:
        # the edge paths take a site API key, never a person's session (plan Part 7.5 and 10.5): no role may write telemetry, events or capabilities
        return _refusal(rid, 403 if has_bearer else 401, "The edge paths take the site's API key", "key_required",
                        None if has_bearer else {"WWW-Authenticate": "Bearer"})
    public = path in PUBLIC_PATHS or (request.method == "POST" and path in PUBLIC_POSTS) or founder
    user = org = company = ident = db_file = None
    if keyed:
        digest = apikeys.token_hash(token)
        held = _key_company(digest) if apikeys.TOKEN.fullmatch(token) else False
        key = None
        if held:
            if not held["enabled"]:
                return _refusal(rid, 403, f"Gridlytics is not switched on for {held['name']}", "company_disabled")
            db_file, company = platformdb.company_file(held), {"id": held["id"], "name": held["name"]}
            with store.use_company(db_file):
                key = apikeys.by_token(token)
        if key is None:
            # a key that is not found counts against its address like any other request, so made-up keys cannot skip the limiter
            if ingest and (wait := auth.rate_wait("address", address)):
                return _too_many(rid, wait, "from this address")
            return _refusal(rid, 401, "This API key is not known, or it was revoked", "invalid_key", {"WWW-Authenticate": "Bearer"})
        needed = apikeys.scope_needed(request.method, path)
        if needed is None or needed not in key["scopes"]:
            return _refusal(rid, 403, f"This API key may not make this request: it holds {', '.join(key['scopes'])}", "out_of_scope")
        named = _SITE_IN_PATH.match(path)
        if named is None:
            # a key is bound to one site (plan Part 10.5): it reaches only paths that name a site, never a collection of the company's sites
            return _refusal(rid, 403, "This API key reaches only its own site's paths", "out_of_scope")
        if named.group(1) != key.get("site_id"):
            return _refusal(rid, 403, "This API key belongs to another site", "wrong_site")
        if (wait := auth.rate_wait("user", f"key:{key['id']}")):
            return _too_many(rid, wait, "for this key")
        user, org = apikeys.acting_user(key), key["org_id"]
    elif has_bearer:
        wanted = request.headers.get("x-gridlytics-company")
        try:
            if path in IDENTITY_PATHS:
                ident = await run_in_threadpool(identity.verify, token)
                if path == "/api/auth/state":
                    try:
                        who = await run_in_threadpool(companies.resolve, token, wanted)
                    except identity.Refused as refused:
                        if refused.status in (401, 503):
                            raise
                        request.state.company_refusal = refused
                        who = None
                    if who is not None:
                        user, org, db_file = who.user, store.DEFAULT_ORG, who.db_path
                        company = {"id": who.company["id"], "name": who.company["name"]}
            else:
                who = await run_in_threadpool(companies.resolve, token, wanted)
                ident, user, org, db_file = who.identity, who.user, store.DEFAULT_ORG, who.db_path
                company = {"id": who.company["id"], "name": who.company["name"]}
        except identity.Refused as refused:
            if not public or path == "/api/auth/state":
                return _refusal(rid, refused.status, refused.detail, refused.code, {"WWW-Authenticate": "Bearer"} if refused.status == 401 else None)
            ident = user = org = company = db_file = None
    request.state.user_id = user["id"] if user else ident.user_id if ident else ""
    request.state.identity = ident
    acting = user["id"] if user else ident.user_id if ident else None
    if api_call and not keyed and (wait := (auth.rate_wait("user", acting) if acting else auth.rate_wait("public", address) if public else 0)):
        return _too_many(rid, wait, "from this account" if acting else "from this address")
    request.state.company_id = company["id"] if company else ""
    markers = _enter(user, org, company, db_file)
    if user is None and ident is None and path.startswith("/api/") and not public:
        _leave(markers)
        return _refusal(rid, 401, "Sign in first")
    if user is None and not public and path.startswith("/api/") and path not in IDENTITY_PATHS:
        _leave(markers)
        return _refusal(rid, 403, "Choose a company first", "no_company")
    try:
        response = await call_next(request)
        if user is not None and money.filtered_reader():
            response = await _without_money(response)
    except Exception as exc:
        record = {"event": "unanswered_request", "request_id": rid, "method": request.method, "path": path, "user": request.state.user_id,
                  "error": type(exc).__name__, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        log.exception("unanswered request %s", json.dumps(record))
        response = _refusal(rid, 500, "The server could not answer this request")
    finally:
        _leave(markers)
    if user is not None and not user.get("api_key") and path.startswith("/api/"):
        response.headers["X-Gridlytics-Role"] = user["role"]
    return response


async def _without_money(response):
    """The money filter, for every route (plan Part 10.5; audit security-roles-02): a JSON answer to a reader who may not see every money field (a viewer,
    an operator for the fee, a machine key) is sent as `money.filter_json` leaves it, so a route that forgets `out()` sends no rupee either. Other answers
    (a CSV, a PDF, a calendar file) pass as the route made them."""
    if not (response.headers.get("content-type") or "").startswith("application/json"):
        return response
    raw = b"".join([part async for part in response.body_iterator])
    clean = money.filter_json(raw)
    body = raw if clean is None else clean
    sent = Response(content=body, status_code=response.status_code)
    sent.raw_headers = [(k, v) for k, v in response.raw_headers if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode("latin-1"))]
    return sent


class _VersionedPath:
    """/api/v1/x is answered exactly as /api/x."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and (scope["path"] == "/api/v1" or scope["path"].startswith("/api/v1/")):
            scope = {**scope, "path": "/api" + scope["path"][len("/api/v1"):], "api_version": "v1"}
        await self.inner(scope, receive, send)


class _BodyLimit:
    """A body over MAX_BODY is refused with 413 however it is sent (a chunked body is counted as it arrives)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS or not scope["path"].startswith("/api/"):
            return await self.inner(scope, receive, send)
        names = {k.lower() for k, _ in scope["headers"]}
        if b"transfer-encoding" not in names and b"content-length" in names:
            return await self.inner(scope, receive, send)
        held, seen, over = [], 0, False
        while True:
            message = await receive()
            if message["type"] != "http.request":
                held.append(message)
                break
            seen += len(message.get("body", b""))
            if seen > MAX_BODY:
                over, held = True, [{"type": "http.request", "body": b"", "more_body": False}]
                scope = {**scope, "body_too_large": True}
                break
            held.append(message)
            if not message.get("more_body"):
                break

        async def replay():
            if held:
                return held.pop(0)
            return {"type": "http.disconnect"} if over else await receive()

        await self.inner(scope, replay, send)


app.add_middleware(_VersionedPath)
app.add_middleware(_BodyLimit)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_origin_regex=CORS_ORIGIN_REGEX, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"], allow_headers=CORS_ALLOW_HEADERS, expose_headers=CORS_EXPOSE_HEADERS,
                   max_age=600)


@app.exception_handler(StarletteHTTPException)
async def _http_refusal(request, exc):
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:16]
    return JSONResponse({**_error_body(exc.status_code, exc.detail, rid, getattr(exc, "code", None)), **(getattr(exc, "extra", None) or {})},
                        status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _body_refusal(request, exc):
    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:16]
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = [str(p) for p in first.get("loc") or ()]
    text = f"{'.'.join(loc) or 'The request'}: {first.get('msg', 'not valid')}"
    body = _error_body(422, text, rid, "invalid_body")
    body["problems"] = jsonable_encoder([{k: v for k, v in e.items() if k not in ("input", "url", "ctx")} for e in errors])
    return JSONResponse(body, status_code=422)


# ------------------------------------------------------------------------------------------------ health, sign-in state, users, audit

@app.get("/api/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/api/auth/state")
def auth_state(request: Request):
    """Who is signed in, the company the request works in, the person's role there and what each permission allows."""
    user, company = auth.current.get(), auth.company.get()
    refused = request.state.company_refusal
    return {"open": False, "mode": identity.MODE, "user": user, "company": {"id": company["id"], "name": company["name"]} if company else None,
            "company_role": (user or {}).get("company_role") or None, "roles": auth.ROLES, "can": {p: auth.can(p) for p in auth.PERMISSIONS},
            "refusal": {"status": refused.status, "code": refused.code, "detail": refused.detail} if refused is not None else None}


def _member_out(m: dict) -> dict:
    seen = m["last_seen"] is not None
    return {"id": m["user_id"], "email": m["email"], "name": m["name"], "role": m["role"], "role_source": m["role_source"], "company_role": m["company_role"],
            "disabled": m["disabled"], "disabled_reason": m["disabled_reason"], "first_seen": m["first_seen"] if seen else None, "last_seen": m["last_seen"],
            "seen": seen, "grants": m["grants"]}


def _not_seen() -> list[dict]:
    company_id = auth.company.get()["id"]
    people = [{"id": m["user_id"], "email": m["email"], "name": m["name"], "company_role": m["role"]}
              for m in platformdb.company_members(company_id) if m["status"] == "active" and not m["disabled"]]
    known = {m["user_id"] for m in store_members.members()}
    out = []
    for p in people:
        if p["id"] in known:
            continue
        owner = p["company_role"] == "owner"
        preset = None if owner else store_members.preset(p["email"])
        role, source = ("admin", "owner") if owner else (preset["role"], "set") if preset else (companies.MEMBER_ROLE, "default")
        out.append({"id": p["id"], "email": p["email"], "name": p["name"], "role": role, "role_source": source, "company_role": p["company_role"],
                    "disabled": False, "disabled_reason": "", "first_seen": None, "last_seen": None, "seen": False, "grants": []})
    return out


@app.get("/api/users")
def users_list():
    auth.need("manage_users")
    if auth.company.get() is None:
        return []
    return [*(_member_out(m) for m in store_members.members()), *_not_seen()]


def _account(inp) -> dict:
    if not isinstance(inp, dict):
        _fail("The user must be a set of named values")
    out = {}
    if "role" in inp:
        if not isinstance(inp.get("role"), str) or inp["role"] not in auth.ROLES:
            _fail("Unknown role")
        out["role"] = inp["role"]
    if "disabled" in inp:
        if not isinstance(inp["disabled"], bool):
            _fail("Disabled must be yes or no")
        out["disabled"] = 1 if inp["disabled"] else 0
    if "grants" in inp:
        # what an administrator grants a named member beyond the role (plan Part 10.5; audit security-roles-16): the costs, to a viewer
        grants = inp["grants"]
        if not isinstance(grants, list) or any(not isinstance(g, str) or g not in auth.GRANTS for g in grants):
            _fail(f"A member can be granted only the right to {' and '.join(auth.GRANT_WORDS.values())}: grants is a list holding "
                  f"{' or '.join(repr(g) for g in auth.GRANTS)}, or nothing")
        out["grants"] = sorted(set(grants))
    extra = sorted(set(inp) - {"role", "disabled", "grants"})
    if extra:
        _fail(f"Unknown field '{extra[0]}': a member's role and access can be changed here")
    return out


@app.put("/api/users/{user_id}")
def users_edit(user_id: str, request: Request = None, inp: dict = Body(...)):
    """A member's role and access, also ahead of their first sign-in (lifted from Solarlytics' _member_edit; the licence and seal fields are not Gridlytics')."""
    auth.need("manage_users")
    if auth.company.get() is None:
        raise HTTPException(status_code=404, detail="No such user")
    if isinstance(inp, dict) and {"name", "email", "password"} & set(inp):
        # accounts are preassigned (OnyxLeaf makes them): a member's name and address are OnyxLeaf's to change, and a forgotten password is Sign-in help's
        # one-time password (plan Part 9.3; audit parity-supabase-auth-02)
        _fail("OnyxLeaf changes a member's name or email address: ask us. For a forgotten password, use Sign-in help on the member's row")
    fields = _account(inp)
    person = None
    if store_members.member(user_id) is None:
        person = next((p for p in _not_seen() if p["id"] == user_id), None)
        if person is None:
            raise HTTPException(status_code=404, detail="No such user")
    by = auth.actor()
    with companies._seat_lock:
        found = store_members.member(user_id)
        before = _member_out(found) if found is not None else person
        role, disabled = fields.get("role", before["role"]), bool(fields.get("disabled", before["disabled"]))
        refused = [g for g in fields.get("grants", ()) if auth.GRANTS[g] != role]
        if refused:
            g = refused[0]
            holders = [f"{'an' if n[0] in 'aeiou' else 'a'} {n}" for n in (auth.ROLES[r].lower() for r in auth.ROLES if r in auth.PERMISSIONS[g])]
            _fail(f"A grant to {auth.GRANT_WORDS[g]} is for a {auth.ROLES[auth.GRANTS[g]].lower()}: {', '.join(holders[:-1])} and {holders[-1]} "
                  f"{auth.GRANT_WORDS[g]} already")
        owner = before["role_source"] == "owner" or before["company_role"] == "owner"
        if owner and (role != "admin" or disabled):
            _fail("The company's owner is its administrator in Gridlytics, and stays one")
        admins = [m for m in store_members.members() if m["role"] == "admin" and not m["disabled"]]
        if found is not None and before["role"] == "admin" and not before["disabled"] and len(admins) == 1 and (role != "admin" or disabled):
            _fail("The last administrator cannot be removed or demoted")
        held = platformdb.company(auth.company.get()["id"])
        no_seat = (not disabled and (found is None or before["disabled"]) and not owner and held is not None
                   and store_members.count_members() >= held["seats"])
        if no_seat and (found is not None or "disabled" in fields):
            _fail(f"{held['name']} has {held['seats']} seat{'' if held['seats'] == 1 else 's'} in Gridlytics and every one is taken: switch someone off "
                  "first, or ask us for more seats")
        if found is None:
            made = store_members.add_member(user_id, email=person["email"], name=person["name"], role=role,
                                            role_source="set" if "role" in fields and not owner else person["role_source"],
                                            company_role=person["company_role"], disabled=disabled or no_seat,
                                            disabled_reason=companies.NO_SEAT if no_seat else "", by=by, grants=fields.get("grants", ()))
            store_members.use_preset(person["email"])
        else:
            changes = {k: fields[k] for k in ("role", "grants") if k in fields}
            if "role" in fields and not owner:
                changes["role_source"] = "set"
            if "disabled" in fields:
                changes.update(disabled=disabled, disabled_reason="")
            made = store_members.update_member(user_id, **changes, by=by) if changes else found
    store.audit(by, "changed a user", before["email"], {**fields, **({"role_before": before["role"]} if "role" in fields else {}),
                                                        **({"grants_before": before["grants"]} if "grants" in fields else {})})
    return _member_out(made)


# ------------------------------------------------------------------------------------------------ an administrator helps a member sign in (plan Part 9.3)

#: The letters of a one-time password: none reads as another (0 and o, 1 and l and i), so it can be read out on the phone. Four groups of four: 19 characters
#: with the dashes (a password needs 10), about 79 bits
_ONE_TIME_LETTERS = "abcdefghjkmnpqrstuvwxyz23456789"


def _one_time_password() -> str:
    return "-".join("".join(secrets.choice(_ONE_TIME_LETTERS) for _ in range(4)) for _ in range(4))


def _helped(user_id: str, doing: str, inp) -> dict:
    """The account an administrator may help sign in until PLT-11 brings email (plan Part 9.3; audit pages-A-02): in local mode, an active member of this
    company and of no other (an administrator here never takes over a person's access to another company: OnyxLeaf does that); never the company's owner,
    whose password OnyxLeaf resets; never oneself (My account changes one's own). The request carries no body: the server makes the password."""
    auth.need("manage_users", doing)
    if inp:
        _fail("Send nothing: Gridlytics makes the one-time password and shows it once")
    company = auth.company.get()
    held = platformdb.membership(user_id, company["id"]) if company is not None and isinstance(user_id, str) and len(user_id) <= 64 else None
    found = platformdb.account(user_id) if held is not None and held["status"] == "active" else None
    if found is None:
        raise HTTPException(status_code=404, detail="No such user")
    if identity.MODE != "local":
        raise Refusal(409, "With this sign-in service a forgotten password is reset by email: ask OnyxLeaf", "not_local_mode")
    if user_id == (auth.current.get() or {}).get("id"):
        _fail("Change your own password in My account")
    if held["role"] == "owner":
        raise Refusal(409, "The company's owner asks OnyxLeaf for a new password or to clear a lockout", "owner")
    if any(m["company_id"] != company["id"] for m in platformdb.memberships(user_id)):
        raise Refusal(409, f"{found['name']} is also in another company: OnyxLeaf gives them a new password or clears their lockout", "other_company")
    return found


@app.post("/api/users/{user_id}/password")
def users_password(user_id: str, inp: dict | None = Body(None)):
    """A one-time password for a member (plan Part 9.3; audit pages-A-02): every session of the account ends, its sign-in lockout is cleared, and the
    password is in this answer only (kept as its hash, never in an audit line). Until the person chooses their own, only the sign-in's own paths answer
    (platform.signed_in)."""
    found = _helped(user_id, "Giving a member a one-time password", inp)
    password = _one_time_password()
    platformdb.set_password(found["id"], password, one_time=True)
    platformdb.clear_lock(found["email"], found["id"], auth.actor())
    store.audit(auth.actor(), "issued a one-time password", found["email"])
    return {"password": password, "user_id": found["id"]}


@app.post("/api/users/{user_id}/unlock")
def users_unlock(user_id: str, inp: dict | None = Body(None)):
    """Clear a member's sign-in lockout (plan Part 9.3): the refused tries recorded before now stop counting, from every address."""
    found = _helped(user_id, "Clearing a member's sign-in lockout", inp)
    platformdb.clear_lock(found["email"], found["id"], auth.actor())
    store.audit(auth.actor(), "cleared a sign-in lockout", found["email"])
    return {"cleared": True}


@app.get("/api/audit")
def audit_list(offset: int = 0):
    auth.need("manage_users")
    return store.list_audit(offset=max(0, offset))


# ------------------------------------------------------------------------------------------------ the feature routes

from . import routes_founder, routes_local, routes_platform  # noqa: E402
from . import (routes_sites, routes_tariffs, routes_telemetry, routes_edge, routes_forecasts, routes_plans, routes_savings, routes_reports,  # noqa: E402
               routes_alerts, routes_demo)

FEATURE_ROUTES = (routes_platform, routes_sites, routes_tariffs, routes_telemetry, routes_edge, routes_forecasts, routes_plans, routes_savings, routes_reports,
                  routes_alerts, routes_demo, routes_founder) + ((routes_local,) if identity.MODE == "local" else ())
for _routes in FEATURE_ROUTES:
    app.include_router(_routes.router)


# The built web app, only where GRIDLYTICS_WEB_DIST names its folder (the release build); last, so it never answers an /api path
_WEB_DIST = os.environ.get("GRIDLYTICS_WEB_DIST", "").strip()
if _WEB_DIST and Path(_WEB_DIST).exists():
    _DIST = Path(_WEB_DIST).resolve()
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        target = (_DIST / path).resolve() if path and "\x00" not in path else _DIST / "index.html"
        inside = _DIST in target.parents and target.is_file()
        return FileResponse(target if inside else _DIST / "index.html")
