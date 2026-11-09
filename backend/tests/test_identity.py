"""Stage B1 of the sign-in (the owner's decisions of 24 October 2026, sections 1 and 2; design B.1 and B.2): the access token a request carries is checked here,
in both modes. Local mode: an HS256 token the backend minted itself (issuer "gridlytics-local"). Supabase mode: a token of Gridlytics' own Supabase project,
ES256 through the project's JWKS or HS256 with the project's secret. Only sign-ins with an email and a password get in.

Everything here is offline: the JWKS is made in the test from an EC P-256 key, and `identity._fetch_json` is replaced, so no Supabase host is ever called. The
secrets are made up for the tests."""
import base64
import hashlib
import hmac
import http.client
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from gridlytics import identity, store

#: Made up for the tests: never a real secret
SECRET = "test-only-local-secret-0123456789abcdefgh"
PROJECT_SECRET = "test-only-project-secret-0123456789abcdefgh"
#: A name under .invalid, which no resolver answers (RFC 2606): nothing is fetched from it, since _fetch_json is replaced
URL = "https://gridlytics-test.invalid"
ISSUER = f"{URL}/auth/v1"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}
USER = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"

_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER = ec.generate_private_key(ec.SECP256R1())


def _jwk(key, kid: str) -> dict:
    return {**jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "alg": "ES256", "use": "sig"}


def _claims(**over) -> dict:
    """An access token's claims as Supabase Auth writes them for a person who signed in with an email and a password."""
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "authenticated", "sub": USER, "email": "Saad@Example.test", "phone": "", "role": "authenticated", "iat": now,
              "exp": now + 3600, "aal": "aal1", "amr": [{"method": "password", "timestamp": now}], "session_id": str(uuid.uuid4()), "is_anonymous": False,
              "app_metadata": {"provider": "email", "providers": ["email"]}, "user_metadata": {"full_name": "Saad Khan"}}
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not ...}


def _es256(claims: dict, kid: str = "key-1", key=_KEY) -> str:
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


def _refused(token: str, status: int = 401, code: str = "token_invalid") -> identity.Refused:
    with pytest.raises(identity.Refused) as caught:
        identity.verify(token)
    assert (caught.value.status, caught.value.code) == (status, code), caught.value.detail
    assert caught.value.detail
    return caught.value


def _b64(part: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")


def _hs256_by_hand(head: dict, claims: dict, secret: bytes) -> str:
    signing = _b64(head) + "." + _b64(claims)
    return signing + "." + base64.urlsafe_b64encode(hmac.new(secret, signing.encode(), hashlib.sha256).digest()).decode().rstrip("=")


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "local")
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", SECRET)
    monkeypatch.setattr(identity, "AUDIENCE", "authenticated")
    identity.clear_cache()
    yield
    identity.clear_cache()


@pytest.fixture
def supa(monkeypatch):
    """Supabase mode with a JWKS only (no project secret). Yields the list of URLs fetched; `served` holds the keys the JWKS answers with."""
    monkeypatch.setattr(identity, "MODE", "supabase")
    monkeypatch.setattr(identity, "SUPABASE_URL", URL)
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", "anon-key-made-up-for-the-tests")
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", "")
    monkeypatch.setattr(identity, "AUDIENCE", "authenticated")
    fetched, served = [], [_jwk(_KEY, "key-1")]

    def fetch(url):
        fetched.append(url)
        return {"keys": list(served)}

    monkeypatch.setattr(identity, "_fetch_json", fetch)
    identity.clear_cache()
    yield fetched, served
    identity.clear_cache()


# ============================================================================================================ local mode

def test_a_local_token_is_accepted_with_the_right_secret_issuer_and_audience(local):
    who = identity.verify(identity.issue_local(USER, "Saad@Example.test", "Saad Khan"))
    assert (who.user_id, who.email, who.name, who.issuer, who.aal) == (USER, "saad@example.test", "Saad Khan", "gridlytics-local", "aal1")
    assert uuid.UUID(who.session_id) and who.claims["sub"] == USER


def test_issue_local_mints_the_token_in_supabases_claim_shape(local):
    token = identity.issue_local(USER, "saad@example.test", "Saad Khan", minutes=15)
    head, claims = jwt.get_unverified_header(token), jwt.decode(token, options={"verify_signature": False})
    assert head == {"alg": "HS256", "typ": "JWT"}
    assert claims["sub"] == USER and claims["email"] == "saad@example.test"
    assert (claims["role"], claims["aud"], claims["iss"], claims["aal"], claims["is_anonymous"]) == ("authenticated", "authenticated", "gridlytics-local",
                                                                                                    "aal1", False)
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert [m["method"] for m in claims["amr"]] == ["password"] and claims["amr"][0]["timestamp"] == claims["iat"]
    assert claims["app_metadata"] == {"provider": "email", "providers": ["email"]} and claims["user_metadata"] == {"full_name": "Saad Khan"}
    assert str(uuid.UUID(claims["session_id"])) == claims["session_id"]
    # an hour by default (decisions, section 1)
    plain = jwt.decode(identity.issue_local(USER, "saad@example.test", "Saad Khan"), options={"verify_signature": False})
    assert plain["exp"] - plain["iat"] == 3600


def test_issue_local_refuses_what_verify_would_refuse(local, monkeypatch):
    for bad in ("not-a-uuid", USER.upper(), "", "6f1c2a3b4d5e4f608a7b9c0d1e2f3a4b"):
        with pytest.raises(ValueError):
            identity.issue_local(bad, "saad@example.test", "Saad Khan")
    with pytest.raises(ValueError):
        identity.issue_local(USER, "", "Saad Khan")
    with pytest.raises(ValueError):
        identity.issue_local(USER, "saad@example.test", "Saad Khan", minutes=0)
    monkeypatch.setattr(identity, "MODE", "supabase")
    with pytest.raises(RuntimeError):
        identity.issue_local(USER, "saad@example.test", "Saad Khan")


def test_a_local_token_with_the_wrong_secret_issuer_or_audience_is_refused(local):
    now = int(time.time())
    base = _claims(iss="gridlytics-local", iat=now, exp=now + 600)
    _refused(jwt.encode(base, "another-made-up-secret-0123456789abcdef", algorithm="HS256"))
    _refused(jwt.encode({**base, "iss": ISSUER}, SECRET, algorithm="HS256"))
    _refused(jwt.encode({**base, "iss": "gridlytics-dev"}, SECRET, algorithm="HS256"))
    _refused(jwt.encode({**base, "aud": "anon"}, SECRET, algorithm="HS256"))
    assert identity.verify(jwt.encode(base, SECRET, algorithm="HS256")).user_id == USER


def test_without_a_set_secret_one_is_made_once_per_data_folder_and_kept(local, monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", "")
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "one" / "gridlytics.db")
    token = identity.issue_local(USER, "saad@example.test", "Saad Khan")
    kept = (tmp_path / "one" / "platform.secret").read_text(encoding="ascii")
    assert len(kept.strip()) >= 32
    identity.clear_cache()
    assert identity.verify(token).user_id == USER                         # read back from the file, not made again
    assert (tmp_path / "one" / "platform.secret").read_text(encoding="ascii") == kept
    # another data folder (a test's own database) has its own secret, so a token of the first is refused there
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "two" / "gridlytics.db")
    _refused(token)
    assert (tmp_path / "two" / "platform.secret").read_text(encoding="ascii") != kept
    # GRIDLYTICS_DATA_DIR names the data folder whatever the database
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path / "one"))
    assert identity.verify(token).user_id == USER


def test_a_secret_file_that_is_there_is_never_overwritten_and_one_too_short_is_refused(local, monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", "")
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    (tmp_path / "platform.secret").write_text("x" * 40 + "\n", encoding="ascii")
    token = identity.issue_local(USER, "saad@example.test", "Saad Khan")
    assert identity.verify(token).user_id == USER
    jwt.decode(token, "x" * 40, algorithms=["HS256"], audience="authenticated")
    assert (tmp_path / "platform.secret").read_text(encoding="ascii") == "x" * 40 + "\n"
    identity.clear_cache()
    (tmp_path / "platform.secret").write_text("short", encoding="ascii")
    monkeypatch.setattr(identity, "_SECRET_WAIT", 0.05)
    with pytest.raises(RuntimeError, match="platform.secret"):
        identity.issue_local(USER, "saad@example.test", "Saad Khan")


def test_a_supabase_token_is_refused_in_local_mode(local, monkeypatch):
    _refused(_es256(_claims()))
    # even signed with the local secret, its issuer is the project's
    _refused(jwt.encode(_claims(), SECRET, algorithm="HS256"))


# ============================================================================================================ supabase mode

def test_an_es256_token_of_the_project_is_accepted_through_its_jwks(supa):
    fetched, _ = supa
    who = identity.verify(_es256(_claims()))
    assert (who.user_id, who.email, who.name, who.issuer) == (USER, "saad@example.test", "Saad Khan", ISSUER)
    assert fetched == [f"{URL}/auth/v1/.well-known/jwks.json"]
    identity.verify(_es256(_claims()))
    assert len(fetched) == 1                                              # kept for JWKS_TTL


def test_the_name_comes_from_the_metadata_supabase_keeps(supa):
    assert identity.verify(_es256(_claims(user_metadata={"first_name": "Sunia", "last_name": "Ahmed"}))).name == "Sunia Ahmed"
    assert identity.verify(_es256(_claims(user_metadata={"name": "  Maaz \n Ali "}))).name == "Maaz Ali"
    assert identity.verify(_es256(_claims(user_metadata={}))).name == "saad@example.test"
    assert len(identity.verify(_es256(_claims(user_metadata={"full_name": "A" * 300}))).name) == 80


def test_hs256_with_the_project_secret_is_accepted(supa, monkeypatch):
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", PROJECT_SECRET)
    fetched, _ = supa
    assert identity.verify(jwt.encode(_claims(), PROJECT_SECRET, algorithm="HS256")).user_id == USER
    _refused(jwt.encode(_claims(), "another-made-up-secret-0123456789abcdef", algorithm="HS256"))
    assert fetched == []


def test_hs256_is_refused_when_only_the_jwks_is_configured(supa):
    fetched, _ = supa
    _refused(jwt.encode(_claims(), PROJECT_SECRET, algorithm="HS256"))
    _refused(_hs256_by_hand({"alg": "HS256", "typ": "JWT"}, _claims(), b""))      # the empty secret no project secret stands for
    assert fetched == []


@pytest.mark.parametrize("mode", ["local", "supabase"])
def test_an_expired_token_is_refused_as_expired(mode, local, supa, monkeypatch):
    monkeypatch.setattr(identity, "MODE", mode)
    now = int(time.time())
    if mode == "local":
        token = jwt.encode(_claims(iss="gridlytics-local", iat=now - 7200, exp=now - 120), SECRET, algorithm="HS256")
    else:
        token = _es256(_claims(iat=now - 7200, exp=now - 120))
    _refused(token, 401, "token_expired")


def test_a_token_within_the_leeway_of_its_expiry_is_still_accepted(supa):
    now = int(time.time())
    assert identity.verify(_es256(_claims(iat=now - 3600, exp=now - 5))).user_id == USER


def test_a_token_not_valid_yet_is_refused(supa):
    now = int(time.time())
    _refused(_es256(_claims(nbf=now + 600)))
    _refused(_es256(_claims(iat=now + 600, exp=now + 4200)))


def test_the_wrong_audience_or_issuer_is_refused(supa):
    _refused(_es256(_claims(aud="anon")))
    _refused(_es256(_claims(aud=...)))
    _refused(_es256(_claims(iss="https://another-project.invalid/auth/v1")))
    _refused(_es256(_claims(iss="gridlytics-local")))
    _refused(_es256(_claims(iss=...)))


def test_the_claims_a_token_must_carry(supa):
    for missing in ("exp", "iat", "sub"):
        _refused(_es256(_claims(**{missing: ...})))
    _refused(_es256(_claims(sub="not-a-uuid")))
    _refused(_es256(_claims(sub=USER.upper())))
    _refused(_es256(_claims(email="")))
    _refused(_es256(_claims(email=...)))


def test_alg_none_is_refused(supa, local, monkeypatch):
    for mode in ("supabase", "local"):
        monkeypatch.setattr(identity, "MODE", mode)
        _refused(_b64({"alg": "none", "typ": "JWT"}) + "." + _b64(_claims()) + ".")
        _refused(_b64({"alg": "none", "typ": "JWT", "kid": "key-1"}) + "." + _b64(_claims()) + ".")
        _refused(_b64({"alg": "HS512", "typ": "JWT"}) + "." + _b64(_claims()) + ".c2ln")


def test_what_is_not_a_token_is_refused(supa):
    for bad in ("", "abc", "a.b.c", "Bearer x", "x" * 20000, None, 42):
        _refused(bad)


def test_an_unknown_kid_fetches_the_jwks_once_more_then_waits(supa, monkeypatch):
    fetched, served = supa
    clock = [1000.0]
    monkeypatch.setattr(identity, "_clock", lambda: clock[0])
    identity.verify(_es256(_claims()))
    assert len(fetched) == 1
    rotated = _es256(_claims(), kid="key-2", key=_OTHER)
    _refused(rotated)
    assert len(fetched) == 2                                              # one refetch for the unknown kid
    _refused(rotated)
    _refused(_es256(_claims(), kid="key-3", key=_OTHER))
    assert len(fetched) == 2                                              # no more within REFETCH_SECONDS
    served.append(_jwk(_OTHER, "key-2"))                                  # the project rotates its key
    clock[0] += identity.REFETCH_SECONDS
    assert identity.verify(rotated).user_id == USER
    assert len(fetched) == 3
    assert identity.verify(_es256(_claims())).user_id == USER             # the old key still listed, still good
    assert len(fetched) == 3


def test_the_jwks_is_fetched_again_after_its_ttl(supa, monkeypatch):
    fetched, served = supa
    clock = [1000.0]
    monkeypatch.setattr(identity, "_clock", lambda: clock[0])
    identity.verify(_es256(_claims()))
    clock[0] += identity.JWKS_TTL - 1
    identity.verify(_es256(_claims()))
    assert len(fetched) == 1
    served[:] = [_jwk(_OTHER, "key-2")]                                   # key-1 withdrawn
    clock[0] += 1
    _refused(_es256(_claims()))
    assert len(fetched) == 2


def test_a_jwks_that_cannot_be_reached_answers_unavailable_and_a_known_key_outlives_a_short_outage(supa, monkeypatch):
    fetched, _ = supa
    clock = [1000.0]
    monkeypatch.setattr(identity, "_clock", lambda: clock[0])
    real = identity._fetch_json

    def down(url):
        fetched.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(identity, "_fetch_json", down)
    _refused(_es256(_claims()), 503, "identity_unavailable")
    monkeypatch.setattr(identity, "_fetch_json", real)
    clock[0] += identity.REFETCH_SECONDS
    identity.verify(_es256(_claims()))
    monkeypatch.setattr(identity, "_fetch_json", down)
    clock[0] += identity.JWKS_TTL + 1
    assert identity.verify(_es256(_claims())).user_id == USER             # stale but within the grace: the service is down, not the key withdrawn
    clock[0] += identity.JWKS_GRACE
    _refused(_es256(_claims()), 503, "identity_unavailable")


def test_while_the_jwks_cannot_be_reached_it_is_tried_at_most_once_every_30_seconds(supa, monkeypatch):
    fetched, _ = supa
    clock = [1000.0]
    monkeypatch.setattr(identity, "_clock", lambda: clock[0])
    real = identity._fetch_json

    def down(url):
        fetched.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(identity, "_fetch_json", down)
    for kid in ("key-1", "key-2", "key-3"):                               # never fetched yet: one try for them all
        _refused(_es256(_claims(), kid=kid), 503, "identity_unavailable")
    clock[0] += identity.REFETCH_SECONDS - 1
    _refused(_es256(_claims()), 503, "identity_unavailable")
    assert len(fetched) == 1
    clock[0] += 1
    _refused(_es256(_claims()), 503, "identity_unavailable")
    assert len(fetched) == 2
    # keys there were, gone stale while the project is down: still used within the grace, one try every 30 s, and none extra for an unknown kid
    monkeypatch.setattr(identity, "_fetch_json", real)
    clock[0] += identity.REFETCH_SECONDS
    identity.verify(_es256(_claims()))
    monkeypatch.setattr(identity, "_fetch_json", down)
    clock[0] += identity.JWKS_TTL
    tries = len(fetched)
    for _ in range(3):
        assert identity.verify(_es256(_claims())).user_id == USER
    _refused(_es256(_claims(), kid="key-2", key=_OTHER))
    assert len(fetched) == tries + 1


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b'{"keys": ['), http.client.BadStatusLine("garbled"), OSError("refused"),
                                   ValueError("not JSON")], ids=["incomplete", "bad-status", "os", "value"])
def test_a_broken_answer_from_the_jwks_answers_unavailable(supa, monkeypatch, error):
    def broken(url):
        raise error

    monkeypatch.setattr(identity, "_fetch_json", broken)
    _refused(_es256(_claims()), 503, "identity_unavailable")


def test_the_keys_are_fetched_with_the_anon_key_without_redirects_and_within_a_size(monkeypatch):
    """`_fetch_json` itself, with urllib's opener replaced: nothing goes on the network."""
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", "anon-key-made-up-for-the-tests")
    seen, body = {}, [json.dumps({"keys": []}).encode()]

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return body[0] if n < 0 else body[0][:n]

    class Opener:
        def open(self, req, timeout=None):
            seen.update(url=req.full_url, apikey=req.get_header("Apikey"), timeout=timeout)
            return Answer()

    def build(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(identity.urllib.request, "build_opener", build)
    url = f"{ISSUER}/.well-known/jwks.json"
    assert identity._fetch_json(url) == {"keys": []}
    assert seen == {"handlers": (identity._NoRedirect,), "url": url, "apikey": "anon-key-made-up-for-the-tests", "timeout": identity.FETCH_TIMEOUT}
    assert identity._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.invalid/jwks.json") is None
    body[0] = b" " * (identity.MAX_JWKS_BYTES + 1)
    with pytest.raises(ValueError):
        identity._fetch_json(url)


def test_a_jwks_with_nothing_usable_answers_unavailable(supa, monkeypatch):
    monkeypatch.setattr(identity, "_fetch_json", lambda url: {"keys": "not a list"})
    _refused(_es256(_claims()), 503, "identity_unavailable")


def test_a_key_is_used_only_for_its_own_algorithm(supa):
    _, served = supa
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    served.append({**jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True), "kid": "rsa-1", "alg": "RS256", "use": "sig"})
    served.append({"kty": "oct", "k": base64.urlsafe_b64encode(PROJECT_SECRET.encode()).decode().rstrip("="), "kid": "oct-1", "alg": "HS256"})
    served.append({**_jwk(_OTHER, "enc-1"), "use": "enc"})
    p384 = ec.generate_private_key(ec.SECP384R1())
    served.append({**jwt.algorithms.ECAlgorithm.to_jwk(p384.public_key(), as_dict=True), "kid": "p384-1", "use": "sig"})
    assert identity.verify(jwt.encode(_claims(), rsa_key, algorithm="RS256", headers={"kid": "rsa-1"})).user_id == USER
    # only the keys for ES256 and RS256 signatures are kept
    assert {kid: alg for kid, (alg, _) in identity._jwks["keys"].items()} == {"key-1": "ES256", "rsa-1": "RS256"}
    _refused(jwt.encode(_claims(), p384, algorithm="ES384", headers={"kid": "p384-1"}))
    # an EC key's kid under RS256, an RSA key's kid under ES256: refused
    _refused(jwt.encode(_claims(), rsa_key, algorithm="RS256", headers={"kid": "key-1"}))
    _refused(_es256(_claims(), kid="rsa-1"))
    # a symmetric key in the JWKS is never taken, nor a key for encryption
    _refused(jwt.encode(_claims(), PROJECT_SECRET, algorithm="HS256", headers={"kid": "oct-1"}))
    _refused(_es256(_claims(), kid="enc-1", key=_OTHER))


def test_an_es256_token_without_a_kid_is_refused(supa):
    _refused(jwt.encode(_claims(), _KEY, algorithm="ES256"))


def test_a_forged_or_altered_token_under_a_known_kid_is_refused(supa):
    # signed with another key, which the header offers itself (`jwk`): only the project's own key for that kid is used
    _refused(jwt.encode(_claims(), _OTHER, algorithm="ES256", headers={"kid": "key-1", "jwk": _jwk(_OTHER, "key-1")}))
    _refused(_es256(_claims(), key=_OTHER))
    # a real token whose claims were changed after signing
    head, _, sig = _es256(_claims()).split(".")
    _refused(".".join((head, _b64(_claims(sub=str(uuid.uuid4()))), sig)))


@pytest.mark.parametrize("project_secret", ["", PROJECT_SECRET], ids=["jwks-only", "with-project-secret"])
def test_hs256_signed_with_the_projects_public_key_is_refused(supa, monkeypatch, project_secret):
    """Algorithm confusion (RFC 8725, section 2.1 [recalled]): the public key, which anyone can fetch, used as an HMAC secret under the kid it is listed with."""
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", project_secret)
    pem = _KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    for public in (pem, json.dumps(_jwk(_KEY, "key-1")).encode()):
        _refused(_hs256_by_hand({"alg": "HS256", "typ": "JWT", "kid": "key-1"}, _claims(), public))


def test_a_token_far_longer_than_any_the_project_issues_is_not_read(supa):
    fetched, _ = supa
    _refused(_es256(_claims(user_metadata={"full_name": "Saad Khan", "about": "x" * identity.MAX_TOKEN})))
    assert fetched == []                                                  # refused before any key is looked for
    assert identity.verify(_es256(_claims(user_metadata={"full_name": "Saad Khan", "about": "x" * 2000}))).user_id == USER


def test_a_local_token_is_refused_in_supabase_mode(local, supa, monkeypatch):
    monkeypatch.setattr(identity, "MODE", "local")
    token = identity.issue_local(USER, "saad@example.test", "Saad Khan")
    monkeypatch.setattr(identity, "MODE", "supabase")
    _refused(token)                                                        # HS256 with no project secret configured
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", PROJECT_SECRET)
    _refused(token)                                                        # signed with another secret
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", SECRET)
    _refused(token)                                                        # even with the same secret, the issuer is not the project's


# ============================================================================================================ who may sign in

def test_role_anon_and_anonymous_sign_ins_are_refused(supa):
    for role in ("anon", "service_role", ...):
        assert _refused(_es256(_claims(role=role))).detail == identity.INVALID
    assert _refused(_es256(_claims(is_anonymous=True))).detail == identity.ANONYMOUS


@pytest.mark.parametrize("amr", [[{"method": "oauth", "timestamp": 1}], [{"method": "otp"}], [{"method": "magiclink"}], ["password"], [], "password", ...])
def test_a_token_whose_amr_lacks_password_is_refused(supa, amr):
    assert _refused(_es256(_claims(amr=amr))).detail == identity.PASSWORD_ONLY


def test_a_password_sign_in_with_a_second_factor_is_accepted(supa):
    who = identity.verify(_es256(_claims(aal="aal2", amr=[{"method": "totp", "timestamp": 2}, {"method": "password", "timestamp": 1}])))
    assert who.aal == "aal2"


@pytest.mark.parametrize("meta", [{"provider": "google", "providers": ["google", "email"]}, {"providers": ["email"]}, {}, ..., "email"])
def test_a_token_whose_provider_is_not_email_is_refused(supa, meta):
    _refused(_es256(_claims(app_metadata=meta)))


def test_the_password_only_rules_hold_for_local_tokens_too(local):
    now = int(time.time())
    base = _claims(iss="gridlytics-local", iat=now, exp=now + 600)
    assert identity.verify(jwt.encode(base, SECRET, algorithm="HS256")).user_id == USER
    for bad in ({"amr": [{"method": "oauth"}]}, {"app_metadata": {"provider": "google"}}, {"role": "anon"}, {"is_anonymous": True}):
        _refused(jwt.encode({**base, **bad}, SECRET, algorithm="HS256"))


# ============================================================================================================ start-up

def test_local_mode_on_this_machine_needs_no_secret(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "local")
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", "")
    identity.check_startup(LOOPBACK)
    identity.check_startup({"LocalHost.", "testserver"})


@pytest.mark.parametrize("host", ["solar.example.com", "Solar.Example.COM.", "192.168.1.20", "0.0.0.0"])
def test_local_mode_on_another_host_needs_a_secret_of_32_characters(monkeypatch, host):
    monkeypatch.setattr(identity, "MODE", "local")
    for secret in ("", "x" * 31):
        monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", secret)
        with pytest.raises(RuntimeError, match="GRIDLYTICS_LOCAL_JWT_SECRET"):
            identity.check_startup(LOOPBACK | {host})
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", "x" * 32)
    identity.check_startup(LOOPBACK | {host})


def test_a_set_local_secret_is_never_shorter_than_32_characters(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "local")
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", "too short")
    with pytest.raises(RuntimeError, match="GRIDLYTICS_LOCAL_JWT_SECRET"):
        identity.check_startup(LOOPBACK)


def test_supabase_mode_needs_the_url_and_the_anon_key(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "supabase")
    for url, anon in (("", "anon-key-made-up"), (URL, ""), ("", "")):
        monkeypatch.setattr(identity, "SUPABASE_URL", url)
        monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", anon)
        with pytest.raises(RuntimeError, match="GRIDLYTICS_SUPABASE_URL|GRIDLYTICS_SUPABASE_ANON_KEY"):
            identity.check_startup(LOOPBACK | {"solar.example.com"})
    monkeypatch.setattr(identity, "SUPABASE_URL", URL)
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", "anon-key-made-up")
    identity.check_startup(LOOPBACK | {"solar.example.com"})


def test_supabase_mode_fetches_its_keys_over_https_except_from_this_machine(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "supabase")
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", "anon-key-made-up")
    for bad in ("http://gridlytics-test.invalid", "ftp://gridlytics-test.invalid", "gridlytics-test.invalid", "https://",
                "http://testserver", "http://localhost.gridlytics-test.invalid"):    # "testserver" is the test client's name, not this machine's
        monkeypatch.setattr(identity, "SUPABASE_URL", bad)
        with pytest.raises(RuntimeError, match="GRIDLYTICS_SUPABASE_URL"):
            identity.check_startup(LOOPBACK)
    for good in ("http://127.0.0.1:54321", "http://localhost:54321", "http://[::1]:54321"):    # the Supabase CLI's local stack
        monkeypatch.setattr(identity, "SUPABASE_URL", good)
        identity.check_startup(LOOPBACK)


def test_a_set_project_secret_is_never_shorter_than_32_characters(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "supabase")
    monkeypatch.setattr(identity, "SUPABASE_URL", URL)
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", "anon-key-made-up")
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", "x" * 31)
    with pytest.raises(RuntimeError, match="GRIDLYTICS_SUPABASE_JWT_SECRET"):
        identity.check_startup(LOOPBACK)
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", PROJECT_SECRET)
    identity.check_startup(LOOPBACK)


def test_an_unknown_mode_is_refused_at_start_up(monkeypatch):
    monkeypatch.setattr(identity, "MODE", "dev")
    with pytest.raises(RuntimeError, match="GRIDLYTICS_AUTH_MODE"):
        identity.check_startup(LOOPBACK)


def test_the_refusal_carries_status_code_and_sentence():
    refused = identity.Refused(401, "token_expired", "Your session has ended: sign in again")
    assert (refused.status, refused.code, refused.detail, str(refused)) == (401, "token_expired", "Your session has ended: sign in again",
                                                                             "Your session has ended: sign in again")
