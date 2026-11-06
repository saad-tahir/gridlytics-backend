"""Who a request comes from: the access token it carries, checked (the owner's decisions of 24 October 2026, sections 1 and 2; design B.1 and B.2).

Two modes, GRIDLYTICS_AUTH_MODE:

* `local` (the default: this machine, demos and the tests). The backend is the sign-in service and mints the token itself (`issue_local`): HS256 with
  GRIDLYTICS_LOCAL_JWT_SECRET, issuer "gridlytics-local", in the claim shape of a Supabase Auth access token, so that the rest of the app reads one shape.
  A copy that answers to any host other than this machine must set that secret (32 characters or more). On this machine, without it, a random one is made
  once and kept in the data folder as `platform.secret` (git-ignored), so that every worker and every restart signs with the same one.
* `supabase` (hosted, later). Gridlytics' own Supabase project, never Boxlytics': a token is ES256 (or RS256) under a key of the project's JWKS
  (GRIDLYTICS_SUPABASE_URL + /auth/v1/.well-known/jwks.json, kept for GRIDLYTICS_JWKS_TTL seconds), or HS256 with the project's legacy secret
  (GRIDLYTICS_SUPABASE_JWT_SECRET) where the project still signs with it.

The mode and the configuration choose the algorithm and the key; the token's header only picks among them, so `alg: none`, or an HS256 token where only the
JWKS is configured, is refused before any signature is looked at (RFC 8725, section 3.1 [recalled]). Only a person who signed in with an email and a
password gets in (decisions, section 1): the token's `amr` must name `password` and its `app_metadata.provider` must be `email`, so a Google sign-in, a
magic link or an anonymous sign-in is refused though the project issued it. Which company the person works in, and with what role, is not read here.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import jwt

log = logging.getLogger("gridlytics")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


MODE = os.environ.get("GRIDLYTICS_AUTH_MODE", "local").strip().lower()
#: The issuer of the tokens local mode mints
LOCAL_ISSUER = "gridlytics-local"
LOCAL_JWT_SECRET = os.environ.get("GRIDLYTICS_LOCAL_JWT_SECRET", "").strip()
SUPABASE_URL = os.environ.get("GRIDLYTICS_SUPABASE_URL", "").strip().rstrip("/")
#: The project's anon (publishable) key, which is public: sent with the JWKS request, as the project's gateway asks of every caller
SUPABASE_ANON_KEY = os.environ.get("GRIDLYTICS_SUPABASE_ANON_KEY", "").strip()
SUPABASE_JWT_SECRET = os.environ.get("GRIDLYTICS_SUPABASE_JWT_SECRET", "").strip()
AUDIENCE = os.environ.get("GRIDLYTICS_JWT_AUDIENCE", "").strip() or "authenticated"
#: How long the project's keys are kept before they are fetched again; and how long past that they are still used while the project cannot be reached (a
#: key the project withdrew is then still taken for that long: the program's choice, against locking everyone out for a short outage)
JWKS_TTL = _env_int("GRIDLYTICS_JWKS_TTL", 600)
JWKS_GRACE = 3600
#: An unknown `kid` (the project rotated its key) fetches the keys again at most this often
REFETCH_SECONDS = 30
#: Clock difference allowed on exp, nbf and iat
LEEWAY = 30
#: The least length of a signing secret: 256 bits as text, the size of HS256's hash (RFC 7518, section 3.2 [recalled])
SECRET_CHARS = 32
#: The host names that are this machine, as the guard compares them (api._host_names: without case and without a trailing dot); "testserver" is the test
#: client's, which no network address can have
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "testserver"})
#: The addresses of this machine itself (as urlsplit gives a URL's host: lower case, no brackets): the only ones the project's keys may come from over plain
#: http. Not "testserver", which a resolver on the network could answer for
THIS_MACHINE = frozenset({"127.0.0.1", "localhost", "::1"})
#: A token far longer than any the project issues (about a kilobyte) is not parsed
MAX_TOKEN = 8192
FETCH_TIMEOUT = 5
MAX_JWKS_BYTES = 256 * 1024
ASYMMETRIC = ("ES256", "RS256")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: How long a reader waits for another worker to finish writing the secret file it has just made
_SECRET_WAIT = 2.0
_clock = time.monotonic

INVALID = "This sign-in is not valid; sign in again"
ENDED = "Your session has ended; sign in again"
PASSWORD_ONLY = "Only a sign-in with an email and a password is accepted; sign in with yours"
ANONYMOUS = "An anonymous sign-in is not accepted; sign in with your email and password"
UNAVAILABLE = "The sign-in service cannot be reached just now; try again in a minute"


class Refused(Exception):
    """A token that does not let its bearer in: the status (401, or 503 while the project's keys cannot be fetched), a machine-readable code and the sentence
    a person reads, as the API's refusals carry them (master PS-08)."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _invalid(detail: str = INVALID) -> Refused:
    return Refused(401, "token_invalid", detail)


@dataclass(frozen=True)
class Identity:
    """The person a verified token names. `user_id` is the token's `sub` (a uuid), `email` is lower-case, `claims` the whole verified token."""
    user_id: str
    email: str
    name: str
    issuer: str
    session_id: str
    aal: str
    claims: dict


# ================================================================================================================ start-up

def check_startup(hosts) -> None:
    """Refuses (RuntimeError) a configuration that would let the wrong people in, before the app answers anyone. `hosts` are the host names the API answers
    to (api.ALLOWED_HOSTS)."""
    names = {str(h).strip().lower().rstrip(".") for h in hosts if str(h).strip()}
    if MODE == "local":
        if LOCAL_JWT_SECRET and len(LOCAL_JWT_SECRET) < SECRET_CHARS:
            raise RuntimeError(f"GRIDLYTICS_LOCAL_JWT_SECRET is shorter than {SECRET_CHARS} characters")
        if names - LOOPBACK and not LOCAL_JWT_SECRET:
            raise RuntimeError(f"Local sign-in on a host other than this machine ({', '.join(sorted(names - LOOPBACK))}) needs GRIDLYTICS_LOCAL_JWT_SECRET, "
                               f"{SECRET_CHARS} characters or more, the same for every worker")
    elif MODE == "supabase":
        if not SUPABASE_URL or not SUPABASE_ANON_KEY:
            raise RuntimeError("Supabase sign-in needs GRIDLYTICS_SUPABASE_URL and GRIDLYTICS_SUPABASE_ANON_KEY (Gridlytics' own project)")
        parts = urlsplit(SUPABASE_URL)
        # the keys that sign every token come from this address: over https, unless it is the Supabase CLI's stack on this machine
        if not parts.hostname or not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in THIS_MACHINE)):
            raise RuntimeError("GRIDLYTICS_SUPABASE_URL must be the project's https address")
        if SUPABASE_JWT_SECRET and len(SUPABASE_JWT_SECRET) < SECRET_CHARS:
            raise RuntimeError(f"GRIDLYTICS_SUPABASE_JWT_SECRET is shorter than {SECRET_CHARS} characters: it is not the project's JWT secret")
    else:
        raise RuntimeError(f"GRIDLYTICS_AUTH_MODE is 'local' or 'supabase', not {MODE!r}")


# ================================================================================================================ local mode

def _secret_file() -> Path:
    """Where the kept secret lives: the platform registry's `secret_file()` (addendum, item 1), read at each call, so that a test's own database has its own
    secret. Imported here, not at the top: the platform's sign-in service imports this module."""
    from . import platform as platformdb
    return platformdb.secret_file()


_secrets: dict[str, str] = {}
_secrets_lock = threading.Lock()


def local_secret() -> str:
    """The secret local tokens are signed with: GRIDLYTICS_LOCAL_JWT_SECRET, else the data folder's `platform.secret`, made the first time."""
    if LOCAL_JWT_SECRET:
        return LOCAL_JWT_SECRET
    path = _secret_file()
    with _secrets_lock:
        key = os.path.abspath(path)
        if key not in _secrets:
            _secrets[key] = _kept_secret(path)
        return _secrets[key]


def _kept_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # made only if it is not there: two workers starting together must not each write their own
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(secrets.token_urlsafe(48) + "\n")
    deadline = time.monotonic() + _SECRET_WAIT
    while True:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) >= SECRET_CHARS:
            return text
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{path} holds no secret of {SECRET_CHARS} characters or more: delete it to have a new one made, "
                               "or set GRIDLYTICS_LOCAL_JWT_SECRET")
        time.sleep(0.05)          # another worker made it a moment ago and is writing it


def issue_local(user_id: str, email: str, name: str, minutes: int = 60) -> str:
    """A local-mode access token for an account (decisions, section 1), in the claim shape of a Supabase Auth access token: `sub` the account's uuid, `role`
    and `aud` "authenticated", `iss` "gridlytics-local", `amr` a password sign-in, `app_metadata.provider` "email", ending after `minutes`."""
    if MODE != "local":
        raise RuntimeError("Local tokens are minted only in local mode")
    if not isinstance(user_id, str) or not _UUID.fullmatch(user_id):
        raise ValueError("An account's id is a lower-case uuid")
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("An account has an email")
    if not isinstance(minutes, int) or minutes < 1:
        raise ValueError("A token lasts at least a minute")
    name = " ".join(str(name or "").split())[:80]
    now = int(time.time())
    claims = {"iss": LOCAL_ISSUER, "aud": AUDIENCE, "sub": user_id, "email": email, "phone": "", "role": "authenticated", "iat": now, "exp": now + 60 * minutes,
              "aal": "aal1", "amr": [{"method": "password", "timestamp": now}], "session_id": str(uuid.uuid4()), "is_anonymous": False,
              "app_metadata": {"provider": "email", "providers": ["email"]}, "user_metadata": {"full_name": name} if name else {}}
    return jwt.encode(claims, local_secret(), algorithm="HS256")


# ================================================================================================================ supabase mode: the project's keys

_jwks_lock = threading.Lock()
#: kid -> (algorithm, public key); when the keys were last fetched (`at`), last tried (`tried`), last tried for an unknown kid (`missed`), by _clock
_jwks: dict = {"keys": {}, "at": None, "tried": None, "missed": None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch_json(url: str) -> dict:
    """One GET of the project's JWKS. Not redirected: the keys come from the project's own address or not at all."""
    req = urllib.request.Request(url, headers={"apikey": SUPABASE_ANON_KEY, "Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=FETCH_TIMEOUT) as res:
        body = res.read(MAX_JWKS_BYTES + 1)
    if len(body) > MAX_JWKS_BYTES:
        raise ValueError("the answer is far larger than a key set")
    return json.loads(body.decode("utf-8"))


def _refresh(now: float) -> None:
    """Fetches the project's keys (with _jwks_lock held). A failure keeps the keys there were; a key the app cannot use (a symmetric one, one for
    encryption, an unknown type) is left out."""
    _jwks["tried"] = now
    url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    try:
        listed = _fetch_json(url)
        listed = listed.get("keys") if isinstance(listed, dict) else None
        if not isinstance(listed, list):
            raise ValueError("the answer has no list of keys")
    # URLError, HTTPError and timeouts are OSErrors; bad JSON a ValueError; a broken answer (IncompleteRead, BadStatusLine) an HTTPException, which is
    # neither and would otherwise fail the request as a 500 rather than a 503
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.warning("the sign-in keys could not be fetched from %s: %s", url, exc)
        return
    keys = {}
    for jwk in listed:
        if not isinstance(jwk, dict) or not isinstance(jwk.get("kid"), str) or jwk.get("kty") not in ("EC", "RSA") or jwk.get("use", "sig") != "sig":
            continue
        try:
            found = jwt.PyJWK(jwk)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
        if found.algorithm_name in ASYMMETRIC:
            keys[jwk["kid"]] = (found.algorithm_name, found.key)
    if not keys:
        log.warning("the sign-in keys at %s hold no key the app can check a token with", url)
        return
    _jwks.update(keys=keys, at=now)


def _jwks_key(kid: str, alg: str):
    with _jwks_lock:
        now = _clock()
        at = _jwks["at"]
        if not (at is not None and now - at < JWKS_TTL):
            if _jwks["tried"] is None or now - _jwks["tried"] >= REFETCH_SECONDS:
                _refresh(now)
        elif kid not in _jwks["keys"] and (_jwks["missed"] is None or now - _jwks["missed"] >= REFETCH_SECONDS):
            _jwks["missed"] = now
            _refresh(now)
        at = _jwks["at"]
        usable = at is not None and now - at < JWKS_TTL + JWKS_GRACE
        found = _jwks["keys"].get(kid) if usable else None
    if not usable:
        raise Refused(503, "identity_unavailable", UNAVAILABLE)
    if found is None or found[0] != alg:
        raise _invalid()
    return found[1]


def clear_cache() -> None:
    """Forgets the project's keys and the local secrets read (the tests)."""
    with _jwks_lock:
        _jwks.update(keys={}, at=None, tried=None, missed=None)
    with _secrets_lock:
        _secrets.clear()


# ================================================================================================================ the check

def _text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _password_sign_in(claims: dict) -> bool:
    """A sign-in with an email and a password: `amr` (Supabase's list of {method, timestamp}) names `password`, and the account's provider is `email`. A
    second factor adds a method beside it."""
    amr, app = claims.get("amr"), claims.get("app_metadata")
    return (isinstance(amr, list) and any(isinstance(m, dict) and m.get("method") == "password" for m in amr)
            and isinstance(app, dict) and app.get("provider") == "email")


def verify(token) -> Identity:
    """The person a bearer token names, or Refused: 401 `token_expired` for a token past its time, 401 `token_invalid` for anything else wrong with it, 503
    `identity_unavailable` while the project's keys cannot be fetched."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN:
        raise _invalid()
    try:
        head = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _invalid() from None
    alg, kid = head.get("alg"), head.get("kid")
    if MODE == "local":
        if alg != "HS256":
            raise _invalid()
        key, issuer = local_secret(), LOCAL_ISSUER
    elif MODE == "supabase":
        issuer = f"{SUPABASE_URL}/auth/v1"
        if alg in ASYMMETRIC and isinstance(kid, str) and kid:
            key = _jwks_key(kid, alg)
        elif alg == "HS256" and SUPABASE_JWT_SECRET:
            key = SUPABASE_JWT_SECRET
        else:
            raise _invalid()
    else:
        raise RuntimeError(f"GRIDLYTICS_AUTH_MODE is 'local' or 'supabase', not {MODE!r}")
    try:
        claims = jwt.decode(token, key, algorithms=[alg], audience=AUDIENCE, issuer=issuer, leeway=LEEWAY,
                            options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except jwt.ExpiredSignatureError:
        raise Refused(401, "token_expired", ENDED) from None
    except jwt.PyJWTError:
        raise _invalid() from None
    if claims.get("role") != "authenticated":                              # "anon" (the project's public key), "service_role", or none
        raise _invalid()
    if claims.get("is_anonymous"):
        raise _invalid(ANONYMOUS)
    sub, email = claims.get("sub"), claims.get("email")
    if not isinstance(sub, str) or not _UUID.fullmatch(sub) or not isinstance(email, str) or not email.strip():
        raise _invalid()
    if not _password_sign_in(claims):
        raise _invalid(PASSWORD_ONLY)
    email = email.strip().lower()
    meta = claims.get("user_metadata")
    meta = meta if isinstance(meta, dict) else {}
    name = (_text(meta.get("full_name")) or _text(meta.get("name"))
            or " ".join(filter(None, (_text(meta.get("first_name")), _text(meta.get("last_name"))))))
    return Identity(sub, email, name[:80] or email, issuer, str(claims.get("session_id") or ""), str(claims.get("aal") or ""), claims)
