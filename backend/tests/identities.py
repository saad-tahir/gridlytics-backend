"""Signed-in people for the tests: an account of local sign-in, a membership of a company and a Gridlytics role there, and a TestClient that sends the
person's access token and the company (the owner's decisions of 24 October 2026, sections 1, 3 and 5; design G; stage B4a of the sign-in and split).

`token(email, role, company, company_role)` does what the founder console would (an account with a made-up password, a membership of the company) and what
the company's owner would (the person's Gridlytics role), and gives an access token signed as `POST /api/local/session` signs one. `client(...)` is a
TestClient that sends it with the company in X-Gridlytics-Company.

The test company is registered in the test's own registry: platform.db sits beside `store.DB_PATH` (addendum item 1), so the `db` fixture of test_auth gives
each test its own. Its database is the test's own file, `store.DB_PATH` (addendum item 3): what a test made before anyone signed in, in open mode, is the
signed-in people's workspace too, as it was with the old sign-in. A company asked for with `own_file` has `companies/<id>.db` in the data folder, as a
hosted company has: the tests of isolation between companies use those.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from gridlytics import api, companies, identity, store, store_members
from gridlytics import platform as platformdb

#: The test company, registered (with Gridlytics on) the first time a test asks for it
COMPANY = "Test company"
#: The made-up password every test account is made with: a local account needs one. The tests sign in by token, never with it, but the sign-in's own tests
PASSWORD = "a long test password 1"
#: Seats enough for any test (a company's seats are the founder's: decisions-v2 section 5)
SEATS = 1000


def company(name: str | None = None, *, own_file: bool = False, enabled: bool = True, seats: int = SEATS) -> dict:
    """The registry's company of this name, made the first time it is asked for, its database a file of its own in the data folder's `companies/` with
    `own_file`, else the test's own file. With no name, the test company of the test's database as it is now (`store.DB_PATH`): a test that moves to
    another database in the same folder (another workspace) has another company there."""
    rows = platformdb.list_companies()
    if name is None:
        here = next((c for c in rows if c["db_file"] == str(store.DB_PATH)), None)
        if here is not None:
            return here
        name = COMPANY if not any(c["name"] == COMPANY for c in rows) else f"{COMPANY} {len(rows) + 1}"
    else:
        found = next((c for c in rows if c["name"] == name), None)
        if found is not None:
            return found
    return platformdb.create_company(name, enabled=enabled, seats=seats, db_file=None if own_file else store.DB_PATH)


def _company(which) -> dict:
    if which is None:
        return company()
    if isinstance(which, dict):
        return which
    return platformdb.company(which) or company(which)


def account(email: str, name: str | None = None) -> dict:
    """The account of this address, made with the made-up password the first time it is asked for."""
    return platformdb.account_by_email(email) or platformdb.create_account(email, name or email.split("@")[0].title(), PASSWORD)


def set_role(access_token: str, which, role: str) -> dict:
    """The person's Gridlytics role in the company, set as its owner would set it (source `set`); their member row is made by resolving their token, as their
    first request would. The company's owner is always an administrator (decisions-v2 section 3). Returns the person as `auth.current` holds them."""
    who = companies.resolve(access_token, _company(which)["id"])
    if who.user["role"] == role:
        return who.user
    if who.user["company_role"] == "owner":
        raise ValueError("The company's owner is an administrator in Gridlytics, pinned")
    with store.use_company(who.db_path), store.use_org(store.DEFAULT_ORG):
        return store_members.as_user(store_members.update_member(who.user["id"], role=role, role_source="set", by="test"))


def token(email: str, role: str | None = None, company: dict | str | None = None, company_role: str = "member", *, name: str | None = None,
          minutes: int = 60) -> str:
    """An access token of the person with this address: an account (made if need be), an active membership of `company` (the test company by default; a
    company's row, id or name) with `company_role`, and, when `role` is given, that Gridlytics role in it."""
    which = _company(company)
    person = account(email, name)
    held = platformdb.membership(person["id"], which["id"])
    if held is None:
        platformdb.add_membership(person["id"], which["id"], company_role)
    elif (held["role"], held["status"]) != (company_role, "active"):
        platformdb.set_membership(person["id"], which["id"], role=company_role, status="active")
    access_token = identity.issue_local(person["id"], person["email"], person["name"], minutes)
    if role is not None:
        set_role(access_token, which, role)
    return access_token


def headers(access_token: str, which=None) -> dict:
    """What a signed-in page sends: the token, and the company it works in."""
    out = {"Authorization": f"Bearer {access_token}"}
    if which is not None:
        out["X-Gridlytics-Company"] = _company(which)["id"]
    return out


def client(email: str, role: str | None = None, company: dict | str | None = None, company_role: str = "member", *, name: str | None = None,
           raise_server_exceptions: bool = True) -> TestClient:
    """A TestClient signed in as the person (see `token`), in the company."""
    which = _company(company)
    access_token = token(email, role, which, company_role, name=name)
    return TestClient(api.app, headers=headers(access_token, which), raise_server_exceptions=raise_server_exceptions)
