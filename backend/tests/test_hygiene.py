"""The request guard every answer goes through (api.py: the host and origin checks, JSON-only changes, the body limit, CORS for the web app on its own
origin, the security headers, the closed API pages), carried over from Solarlytics' `tests/test_hygiene.py` (round 7, audit parity-architecture-10).

The guard is the security boundary of a hosted product, lifted from Solarlytics with the sign-in, and nothing pinned its rules: a refactor of api.py
could drop any of them with the suite still green. Each rule has its test here; the sign-in half of the guard (tokens, companies, API keys) is
test_signin_guard's. The tests' own settings are conftest.py's: the test host `testserver`, the web app's origin `https://app.example.test` given as a
JSON array, and its preview origins' pattern."""
from __future__ import annotations

import inspect
import os
import re

import pytest
from fastapi.testclient import TestClient

from gridlytics import api

client = TestClient(api.app, raise_server_exceptions=False)
#: A route that takes a JSON body and answers anyone in local mode (signing out with no session is a no-op)
POST = "/api/local/logout"
APP = "https://app.example.test"
PREVIEW = "https://preview-b4a-1.example.test"
EVIL = ("https://evil.example", "https://app.example.test.evil.example", "https://preview-x.example.test.evil.example", "http://app.example.test",
        "null")


# ------------------------------------------------------------------------------------------------ the host and the origin

def test_an_unknown_host_or_a_cross_site_page_is_refused():
    """DNS rebinding and cross-site requests: only this machine's names (and the ones GRIDLYTICS_HOSTS adds) are served, and a page on another site
    cannot drive the API."""
    for host in ("evil.example", "evil example", "127.0.0.1.evil.example", "", "testserver:80:80"):
        r = client.get("/api/health", headers={"host": host})
        assert r.status_code == 400 and r.json()["code"] == "unknown_host", (host, r.text)
    for host in ("127.0.0.1:8051", "localhost", "localhost.", "[::1]:8051", "TestServer"):
        assert client.get("/api/health", headers={"host": host}).status_code == 200, host
    for origin in EVIL:
        r = client.post(POST, json={}, headers={"origin": origin})
        assert r.status_code == 403 and r.json()["code"] == "cross_site", (origin, r.text)
    # the page this machine serves (the launcher's 127.0.0.1 ports) and the web app's own origin may
    for origin in ("http://127.0.0.1:5295", "http://localhost:5195", APP, PREVIEW):
        assert client.post(POST, json={}, headers={"origin": origin}).status_code == 200, origin


def test_the_test_host_is_allowed_only_in_the_tests():
    assert os.environ.get("GRIDLYTICS_HOSTS") == "testserver"
    assert "testserver" in api.ALLOWED_HOSTS
    # without the setting, this machine's names only
    assert api._host_names("") == {"127.0.0.1", "localhost", "::1", "[::1]"}
    assert api._host_names(" App.Example.com. , api.example.com") == {"127.0.0.1", "localhost", "::1", "[::1]", "app.example.com", "api.example.com"}


# ------------------------------------------------------------------------------------------------ what a change may be

def test_a_change_must_be_json():
    """A plain HTML form can post without the browser asking first; it cannot send JSON."""
    for ctype in ("application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain"):
        r = client.post(POST, content="change=1", headers={"content-type": ctype})
        assert r.status_code == 415 and r.json()["code"] == "must_be_json", (ctype, r.text)
    for method in ("PUT", "PATCH", "DELETE"):
        r = client.request(method, "/api/sites/abc", content="a=1", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415, method
    assert client.post(POST, json={}).status_code == 200
    assert client.post(POST, content="{}", headers={"content-type": "application/merge-patch+json"}).status_code == 200
    # a POST or DELETE with no body at all is not a form: it goes on to the route (here, to the sign-in it needs)
    assert client.post("/api/sites/abc/plan/resolve").status_code != 415
    assert client.delete("/api/sites/abc").status_code != 415


def test_a_file_is_taken_only_by_an_upload_path():
    # a CSV is taken where a file is uploaded, and goes on to the sign-in; anywhere else it is refused as a form would be
    r = client.post("/api/sites/abc/telemetry/upload", content="ts,load_power_w\n", headers={"content-type": "text/csv"})
    assert r.status_code == 401
    r = client.post("/api/sites/abc/outages/schedule", content="ts,load_power_w\n", headers={"content-type": "text/csv"})
    assert r.status_code == 415 and r.json()["code"] == "must_be_json"


def test_a_body_over_the_limit_is_refused_however_it_is_sent(monkeypatch):
    monkeypatch.setattr(api, "MAX_BODY", 1000)
    big = "{" + " " * 1200 + "}"
    r = client.post(POST, content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large", r.text
    # a chunked body, which says no length: counted as it arrives
    r = client.post(POST, content=iter([b"{" + b" " * 600, b" " * 600 + b"}"]), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large", r.text
    assert client.post(POST, content="{" + " " * 900 + "}", headers={"content-type": "application/json"}).status_code == 200
    assert client.post(POST, content=iter([b"{", b" " * 500, b"}"]), headers={"content-type": "application/json"}).status_code == 200


# ------------------------------------------------------------------------------------------------ CORS: the web app on its own origin

ASKED = {"origin": APP, "access-control-request-method": "PUT",
         "access-control-request-headers": "authorization, content-type, x-gridlytics-company, x-request-id, x-founder-session, idempotency-key"}


def test_the_web_app_on_its_own_origin_may_call_the_api_and_read_the_headers_it_needs():
    """The hosted web app (GRIDLYTICS_CORS_ORIGINS, and the preview origins' pattern) may send the token, the company, a request id, the founder's
    session and an idempotency key, and read every header the pages read, the role's included; no other site may, and nothing is sent with the
    browser's credentials."""
    pre = client.options("/api/sites", headers=ASKED)
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == APP
    assert {h.strip().lower() for h in pre.headers["access-control-allow-headers"].split(",")} >= {
        "authorization", "content-type", "x-gridlytics-company", "x-request-id", "x-founder-session", "idempotency-key"}
    assert {"GET", "POST", "PUT", "DELETE", "PATCH"} <= {m.strip() for m in pre.headers["access-control-allow-methods"].split(",")}
    assert pre.headers["access-control-max-age"] == "600" and "access-control-allow-credentials" not in pre.headers
    for origin in (APP, PREVIEW):
        r = client.get("/api/health", headers={"origin": origin})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin, origin
        assert {h.strip() for h in r.headers["access-control-expose-headers"].split(",")} == {
            "X-Request-ID", "X-Total-Count", "X-Gridlytics-Engine", "X-Gridlytics-Role", "X-API-Version", "Content-Disposition", "Retry-After"}
        assert "access-control-allow-credentials" not in r.headers
    # another site: no answer to its preflight, and its request refused by the guard, with no CORS header to let a page read the refusal
    for origin in EVIL:
        assert client.options("/api/sites", headers={**ASKED, "origin": origin}).status_code == 400, origin
        r = client.get("/api/health", headers={"origin": origin})
        assert r.status_code == 403 and r.json()["code"] == "cross_site" and "access-control-allow-origin" not in r.headers, origin
    # a header the web app does not send is not allowed
    r = client.options("/api/sites", headers={**ASKED, "access-control-request-headers": "x-made-up"})
    assert r.status_code == 400


def test_the_web_apps_origins_are_read_as_a_list_or_a_json_array_and_nothing_else_is_taken():
    assert api._cors_origins("") == []
    assert api._cors_origins(" https://App.Example.com/ , https://preview.example.com:8443 ") == ["https://app.example.com", "https://preview.example.com:8443"]
    assert api._cors_origins('["https://app.example.com", "http://127.0.0.1:5192"]') == ["https://app.example.com", "http://127.0.0.1:5192"]
    assert api.CORS_ORIGINS == [APP]                                                 # conftest gives it as a JSON array
    for bad in ("*", '["*"]', "https://app.example.com/path", "app.example.com", "ftp://app.example.com", '["https://a.example.com"', '{"a": 1}', "[1]",
                '[{"origin": "https://a.example.com"}]'):
        with pytest.raises(RuntimeError, match="GRIDLYTICS_CORS_ORIGINS"):
            api._cors_origins(bad)
    # the preview origins' pattern: a broken one stops the start with its setting named, never the first request with a server error
    with pytest.raises(RuntimeError, match="GRIDLYTICS_CORS_ORIGIN_REGEX"):
        api._cors_regex("https://(preview")
    assert api._cors_regex("") is None and api._cors_regex(r" https://p-\d+\.example\.com ") == r"https://p-\d+\.example\.com"
    assert api.CORS_ORIGIN_REGEX == r"https://preview-[a-z0-9-]+\.example\.test"


# ------------------------------------------------------------------------------------------------ what every answer carries

def _secure(r) -> None:
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY", r.request.url
    assert r.headers["referrer-policy"] == "no-referrer"
    policy = r.headers["content-security-policy"]
    # framed by no page; the web app the API serves runs only its own scripts: an injected inline script or one from elsewhere does not run
    for rule in ("frame-ancestors 'none'", "script-src 'self'", "base-uri 'self'", "form-action 'self'"):
        assert rule in policy, (r.request.url, rule)
    assert re.fullmatch(r"[A-Za-z0-9._-]{8,64}", r.headers["x-request-id"])


def test_every_answer_carries_the_security_headers_and_the_api_pages_are_closed():
    answers = [client.get("/api/health"),                                             # an answer
               client.get("/api/sites"),                                              # a refusal of the sign-in
               client.get("/api/health", headers={"host": "evil.example"}),           # the guard's own refusals
               client.post(POST, content="a=1", headers={"content-type": "application/x-www-form-urlencoded"}),
               client.get("/api/health", headers={"origin": "https://evil.example"}),
               client.get("/nothing-here")]
    for r in answers:
        _secure(r)
    assert answers[0].headers["x-api-version"] == "1" and answers[0].headers["x-gridlytics-engine"]
    # the API's own schema and pages are not served (GRIDLYTICS_DOCS=1 opens them on a developer's machine only)
    assert api._DOCS is False
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = client.get(path)
        assert r.status_code == 404 or "openapi" not in r.text.lower(), path
        _secure(r)


def test_a_refusal_has_one_shape_and_names_the_request():
    """Every refusal: a sentence, a code and the request's id, which is the one the page sent when it is well-formed (so a support request names it)."""
    r = client.get("/api/sites", headers={"x-request-id": "page-made-id-0001"})
    assert r.status_code == 401 and set(r.json()) == {"detail", "code", "request_id"}
    assert r.json()["request_id"] == r.headers["x-request-id"] == "page-made-id-0001"
    for sent in ("short", "has spaces in it", "x" * 65, "<script>alert(1)</script>"):
        r = client.get("/api/sites", headers={"x-request-id": sent})
        assert r.headers["x-request-id"] != sent and re.fullmatch(r"[0-9a-f]{16}", r.headers["x-request-id"]), sent
        assert r.json()["request_id"] == r.headers["x-request-id"]


def test_the_versioned_path_is_the_same_api():
    assert client.get("/api/v1/health").json() == client.get("/api/health").json()
    r = client.get("/api/v1/health", headers={"host": "evil.example"})
    assert r.status_code == 400                                                       # through the same guard


# ------------------------------------------------------------------------------------------------ the lifted tests

def test_no_lifted_test_is_skipped_by_name_any_more():
    """The lifted tests that asserted Solarlytics' own data were skipped by name until round 7; they are re-aimed now, and the only skip conftest.py
    adds is the opt-in benchmarks' marker."""
    import conftest
    assert not hasattr(conftest, "_SOLARLYTICS_ONLY")
    source = inspect.getsource(conftest.pytest_collection_modifyitems)
    assert source.count("mark.skip(") == 1 and "SLOW" in source
