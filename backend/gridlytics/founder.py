"""The founder console's own sign-in (the owner's decisions of 24 October 2026, section 4; design E as the decisions keep it; stage B6 of the sign-in and split).

The founder is no account of a company: a username, a password and a six-digit code of an authenticator app, set on the server only:

* GRIDLYTICS_FOUNDER_USERNAME;
* GRIDLYTICS_FOUNDER_PASSWORD_HASH: auth.py's PBKDF2 form, made by `python -m gridlytics.founder hash`. Boxlytics' plain sha256 (64 hex digits) is taken
  too, for parity, with a warning in the log;
* GRIDLYTICS_FOUNDER_TOTP_SECRET: Base32 (`python -m gridlytics.founder totp-secret` makes one, with the otpauth:// link for the app);
* GRIDLYTICS_FOUNDER_SESSION_SECRET: the key of the session tokens.

Each is read when it is needed, never kept. Until all four are set, and while the code secret is not Base32 or the hash is in neither form, the
console answers 503.

The session is an 8-hour token in the exact format of Boxlytics' `founder-auth.ts` (`boxlytics@c429bef`, supabase/functions/_shared/founder-auth.ts,
ported): base64url of the JSON {"sub":"founder","iat":..,"exp":..} (JSON.stringify's order, no spaces), a dot, and base64url of HMAC-SHA256 over that first
part with the session secret (trimmed), all without padding. It is checked as `verifySessionToken` checks it: the signature compared in constant time, `sub`
"founder", `iat` and `exp` finite numbers, and `exp` after now. Nothing is kept of a session on the server, so signing out is forgetting the token.

The sign-in is two steps, and the password is sent once (addendum item 13):

1. the username and the password: compared in constant time, and the password's hash is checked even when the username is wrong, so that neither the
   answer nor its time says which was wrong. A good pair gives a challenge (auth.new_link: only its hash is kept), good for CHALLENGE_MINUTES and
   CHALLENGE_TRIES codes;
2. the challenge and the code: mfa.verify, with the time step of the last code taken kept in platform.db, so that a code is never taken twice (Boxlytics
   took one again within its 90 seconds).

Boxlytics' two gaps are closed: wrong passwords and wrong codes are locked out as the app's own sign-in is (platform.lock_wait, R-235's rules), and wrong
codes from every address together stop the code step for the founder (R-271; the lock is read before the challenge, so nobody is told "sign in again" and
then refused). Every sign-in, good or refused, is written to the founder's audit; the record the lockout counts is platform.db's `account_events`, under
WHO, which is no email address and so never an account's.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sys
import time

from . import auth, mfa
from . import platform as platformdb

log = logging.getLogger("gridlytics")

#: The four settings of the console (decisions-v2 section 4)
SETTINGS = ("GRIDLYTICS_FOUNDER_USERNAME", "GRIDLYTICS_FOUNDER_PASSWORD_HASH", "GRIDLYTICS_FOUNDER_TOTP_SECRET", "GRIDLYTICS_FOUNDER_SESSION_SECRET")
#: A session's life (founder-auth.ts: SESSION_DURATION_SECONDS)
SESSION_SECONDS = 8 * 3600
#: The challenge between the two steps: how long it lasts and how many codes it takes (auth_mfa's, carried over)
CHALLENGE_MINUTES = auth.CHALLENGE_MINUTES
CHALLENGE_TRIES = auth.MAX_TRIES
#: Wrong codes from every address together in auth.LOCK_MINUTES before the code step is stopped for the founder (R-271; the accounts' second factor's number)
CODE_TRIES = auth.MFA_ACCOUNT_TRIES
#: The founder's line in the record of sign-ins: not an email address, so never an account's
WHO = "founder"
#: The record's words: a password taken (its detail the challenge's hash), a code refused, and a sign-in (its detail the step of the code taken)
PASSWORD_TAKEN = "founder password taken"
CODE_REFUSED = "founder code refused"
SIGNED_IN = platformdb.SIGNED_IN
REFUSED = platformdb.REFUSED
#: A session token is far shorter than this; anything longer is not one
MAX_TOKEN = 2048
_WRONG = "The username or the password is wrong"
#: The forms GRIDLYTICS_FOUNDER_PASSWORD_HASH takes: auth.hash_password's PBKDF2, and Boxlytics' sha256 in hex (either case)
_HASH_FORMS = re.compile(r"pbkdf2_sha256\$[1-9]\d{0,7}\$(?:[0-9a-f]{2})+\$[0-9a-f]{64}|[0-9a-fA-F]{64}")
_clock = time.time
#: Boxlytics' sha256 hash is said once a process, not at every sign-in
_warned = False


class Refused(Exception):
    """A refusal of the founder's sign-in or session, the routes' to answer: the status, the code, the sentence, and the seconds to wait for a 429."""

    def __init__(self, status: int, code: str, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.retry_after = status, code, detail, retry_after


# ------------------------------------------------------------------------------------------------ the settings

def _setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def missing() -> list[str]:
    """The console's settings that are not set; empty when it is set up."""
    return [name for name in SETTINGS if not _setting(name)]


def malformed() -> list[str]:
    """The settings that are set but cannot work: a code secret that is not Base32, or a password hash in neither form (auth.py's PBKDF2, or 64 hex digits).
    Either would refuse every sign-in, and lock the founder out, without saying why."""
    out = []
    try:
        mfa.secret_bytes(_setting("GRIDLYTICS_FOUNDER_TOTP_SECRET"))
    except ValueError:
        out.append("GRIDLYTICS_FOUNDER_TOTP_SECRET")
    if not _HASH_FORMS.fullmatch(_setting("GRIDLYTICS_FOUNDER_PASSWORD_HASH")):
        out.append("GRIDLYTICS_FOUNDER_PASSWORD_HASH")
    return out


def need_set_up() -> None:
    """Refused 503 until the four settings are set, and while one cannot work (its value is never said)."""
    gone = missing()
    if gone:
        raise Refused(503, "unavailable", f"The founder console is not set up on this server: set {', '.join(gone)}")
    bad = malformed()
    if bad:
        raise Refused(503, "unavailable", f"The founder console is not set up on this server: {' and '.join(bad)} {'is' if len(bad) == 1 else 'are'} "
                                          "not in the form `python -m gridlytics.founder` makes")


# ------------------------------------------------------------------------------------------------ the password's hash

def hash_password(password: str) -> str:
    """The PBKDF2 hash GRIDLYTICS_FOUNDER_PASSWORD_HASH holds (auth.py's form, `pbkdf2_sha256$<rounds>$<salt>$<digest>`)."""
    return auth.hash_password(password)


def check_password(password: str, stored: str) -> bool:
    """Whether the password is the one of the hash: auth.py's PBKDF2, or Boxlytics' plain sha256 in hex (either case), which is taken with a warning."""
    global _warned
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{64}", stored):
        if not _warned:
            _warned = True
            log.warning("GRIDLYTICS_FOUNDER_PASSWORD_HASH is a plain sha256, as Boxlytics keeps it: make a PBKDF2 one with "
                        "`python -m gridlytics.founder hash` and set it in its place")
        return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), stored.lower())
    return auth.check_password(password, stored)


# ------------------------------------------------------------------------------------------------ the session token (founder-auth.ts, ported)

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)


def _sign(payload_b64: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest())


def new_token() -> tuple[str, int]:
    """A session token of the founder and the moment it ends (unix seconds): createSessionToken's."""
    need_set_up()
    now = math.floor(_clock())
    exp = now + SESSION_SECONDS
    payload_b64 = _b64(json.dumps({"sub": "founder", "iat": now, "exp": exp}, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, _setting('GRIDLYTICS_FOUNDER_SESSION_SECRET'))}", exp


def _finite(value) -> bool:
    """Number.isFinite's answer for a value JSON.parse gives: an integer too large for a float is Infinity there, and was an OverflowError (a 500) here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def verify(token) -> int:
    """The moment a founder's session token ends (unix seconds), as verifySessionToken checks it; Refused 401 when there is none, when it is not good
    (the format, the signature, the payload) or when it has ended. A token is the two parts of createSessionToken and nothing more (Boxlytics' split
    would let a third part through unread)."""
    need_set_up()
    if not isinstance(token, str) or not token.strip():
        raise Refused(401, "founder_session_required", "Sign in to the founder console first")
    invalid = Refused(401, "founder_session_invalid", "The founder session is not valid; sign in again")
    parts = token.strip().split(".")
    if len(token) > MAX_TOKEN or len(parts) != 2 or not all(parts):
        raise invalid
    payload_b64, signature = parts
    try:
        expected = _sign(payload_b64, _setting("GRIDLYTICS_FOUNDER_SESSION_SECRET"))
    except UnicodeEncodeError:
        raise invalid from None
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
        raise invalid
    try:
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise invalid from None
    if not isinstance(payload, dict) or payload.get("sub") != "founder" or not _finite(payload.get("iat")) or not _finite(payload.get("exp")):
        raise invalid
    if not payload["exp"] > math.floor(_clock()):
        raise Refused(401, "founder_session_expired", "The founder session has ended; sign in again")
    return int(payload["exp"])


# ------------------------------------------------------------------------------------------------ the sign-in: the lockout, the password, the code

def _wait_words(wait: int) -> str:
    minutes = math.ceil(wait / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _locked(address: str) -> None:
    """The founder's locks, read before anything else at both steps: wrong passwords from this address and wrong codes from this address (auth.MAX_TRIES
    each in auth.LOCK_MINUTES), and wrong codes from every address together (CODE_TRIES). The password step is told of a code lock too, so that the
    founder is not let on to a code step that will be refused (R-271)."""
    wait = platformdb.lock_wait(WHO, address)
    if wait:
        raise Refused(429, "too_many_requests", f"Too many attempts from here; wait {_wait_words(wait)}", wait)
    wait = max(platformdb.lock_wait(WHO, address, refused=CODE_REFUSED), platformdb.lock_wait(WHO, None, refused=CODE_REFUSED, tries=CODE_TRIES))
    if wait:
        raise Refused(429, "too_many_requests", f"Too many wrong codes; wait {_wait_words(wait)}, then sign in again with your password", wait)


def login(username, password, address: str) -> dict:
    """Step 1: the username and the password. A good pair gives the challenge the code step takes; anything else one sentence for both, and a refusal the
    lockout counts. The pair is checked in full whichever part is wrong."""
    need_set_up()
    _locked(address)
    if not isinstance(username, str) or not isinstance(password, str):
        raise Refused(422, "invalid", "Give the founder's username and password")
    # Both parts are checked, the hash even for a wrong username, and the username in constant time: the answer's time says nothing of which was wrong
    user_ok = hmac.compare_digest(username.encode("utf-8"), _setting("GRIDLYTICS_FOUNDER_USERNAME").encode("utf-8"))
    password_ok = check_password(password, _setting("GRIDLYTICS_FOUNDER_PASSWORD_HASH"))
    if not (user_ok and password_ok):
        platformdb.record(REFUSED, WHO, address)
        platformdb.founder_audit("sign-in refused", "", {"address": address})
        raise Refused(401, "invalid_credentials", _WRONG)
    challenge, digest = auth.new_link()
    platformdb.record(PASSWORD_TAKEN, WHO, address, detail={"challenge": digest})
    return {"challenge": challenge, "expires_in": CHALLENGE_MINUTES * 60, "tries": CHALLENGE_TRIES}


def code(challenge, code, address: str) -> dict:
    """Step 2: the challenge of step 1 and a code of the authenticator app. A good code gives the session {token, expires_at}. A wrong one is counted,
    and the answer says the tries the challenge has left; a wrong one that stops the code step says so at once (R-271)."""
    from . import store_founder
    need_set_up()
    _locked(address)
    ended = Refused(401, "challenge_expired", "This sign-in has ended; sign in again with your password")
    if not isinstance(challenge, str) or not re.fullmatch(auth.LINK_TOKEN, challenge):
        raise ended
    if not isinstance(code, str) or not 1 <= len(code) <= 20:
        raise Refused(422, "invalid", "Give the six-digit code of the authenticator app")
    secret = _setting("GRIDLYTICS_FOUNDER_TOTP_SECRET")
    taken = store_founder.take_code(auth.token_hash(challenge), address, lambda last: mfa.verify(secret, code, last),
                                    tries=CHALLENGE_TRIES, seconds=CHALLENGE_MINUTES * 60)
    if taken is None:
        raise ended
    if taken["step"] is None:
        platformdb.founder_audit("code refused", "", {"address": address})
        if platformdb.lock_wait(WHO, None, refused=CODE_REFUSED, tries=CODE_TRIES):
            if store_founder.code_refusals(auth.LOCK_MINUTES * 60) == CODE_TRIES:           # once, when the code step is stopped
                platformdb.founder_audit("code step stopped", "", {"address": address, "minutes": auth.LOCK_MINUTES})
        _locked(address)
        left = taken["tries_left"]
        raise Refused(401, "invalid_code", f"The code is wrong or was used already; {left} tr{'y' if left == 1 else 'ies'} left" if left
                      else "The code is wrong or was used already; sign in again with your password")
    token, exp = new_token()
    platformdb.founder_audit("signed in", "", {"address": address})
    return {"token": token, "expires_at": exp}


# ------------------------------------------------------------------------------------------------ the command line

def main(argv: list[str] | None = None, stdin=None, stdout=None, stderr=None) -> int:
    """`python -m gridlytics.founder hash | totp-secret | code`: the password's hash for GRIDLYTICS_FOUNDER_PASSWORD_HASH (the password asked twice, or
    read from the standard input when it is not a terminal), a new code secret for GRIDLYTICS_FOUNDER_TOTP_SECRET with its otpauth:// link, and the
    current code of GRIDLYTICS_FOUNDER_TOTP_SECRET. Nothing is printed but what was asked for; no secret is taken as an argument, which a shell's
    history would keep."""
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m gridlytics.founder", description="The founder console's settings")
    parser.add_argument("what", choices=("hash", "totp-secret", "code"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    if args.what == "hash":
        if stdin.isatty():
            password = getpass.getpass("The founder's password: ")
            if getpass.getpass("Again: ") != password:
                print("The two passwords differ", file=stderr)
                return 1
        else:
            password = stdin.readline().rstrip("\r\n")
        problem = platformdb.password_problem(password)
        if problem:
            print(problem, file=stderr)
            return 1
        print(hash_password(password), file=stdout)
        return 0
    if args.what == "totp-secret":
        secret = mfa.new_secret()
        print(secret, file=stdout)
        print(mfa.uri(secret, _setting("GRIDLYTICS_FOUNDER_USERNAME") or WHO, f"{mfa.ISSUER} founder"), file=stdout)
        return 0
    secret = _setting("GRIDLYTICS_FOUNDER_TOTP_SECRET")
    try:
        key = mfa.secret_bytes(secret)
    except ValueError:
        print("Set GRIDLYTICS_FOUNDER_TOTP_SECRET to the founder's code secret (Base32) first", file=stderr)
        return 1
    print(mfa.totp(key), file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
