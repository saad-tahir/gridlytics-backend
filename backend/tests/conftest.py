import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Tests write run history to a throwaway database, never to the workspace one.
os.environ["GRIDLYTICS_DB"] = str(Path(tempfile.mkdtemp(prefix="gridlytics-test-")) / "test.db")

# The test client calls itself "testserver"; only the tests allow that name.
os.environ["GRIDLYTICS_HOSTS"] = "testserver"

# A reset of the settings copies the database first (backups.before_reset); the tests make that copy only where they test it
os.environ["GRIDLYTICS_RESET_BACKUP"] = "0"

# Local sign-in whatever the machine's own settings (the owner's decisions of 24 October 2026, section 1), with a fixed made-up signing key, so that no test
# writes a platform.secret; the registry and the companies' files follow each test's database (no GRIDLYTICS_DATA_DIR); never a Supabase project
os.environ["GRIDLYTICS_AUTH_MODE"] = "local"
os.environ["GRIDLYTICS_LOCAL_JWT_SECRET"] = "made-up-local-signing-key-of-the-tests-0001"
for _name in ("GRIDLYTICS_DATA_DIR", "GRIDLYTICS_SUPABASE_URL", "GRIDLYTICS_SUPABASE_ANON_KEY", "GRIDLYTICS_SUPABASE_JWT_SECRET",
              "GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "GRIDLYTICS_TRUST_PROXY"):
    os.environ.pop(_name, None)
# The founder console's own settings (decisions, section 4), made up; its password hash and code secret are set by its own tests
os.environ["GRIDLYTICS_FOUNDER_USERNAME"] = "founder"
os.environ["GRIDLYTICS_FOUNDER_SESSION_SECRET"] = "made-up-founder-session-key-of-the-tests-01"
# The web app served from another origin, and its preview origins, for the tests of CORS (test_hygiene); both forms of the list are read
os.environ["GRIDLYTICS_CORS_ORIGINS"] = '["https://app.example.test"]'
os.environ["GRIDLYTICS_CORS_ORIGIN_REGEX"] = r"https://preview-[a-z0-9-]+\.example\.test"


import sqlite3  # noqa: E402
from contextlib import closing  # noqa: E402

import pytest  # noqa: E402

_SHARED_DB = Path(os.environ["GRIDLYTICS_DB"])
#: The shared database's registry of local sign-in (platform.py: beside the database)
_SHARED_PLATFORM = _SHARED_DB.parent / "platform.db"


def _left(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    try:
        with closing(sqlite3.connect(path)) as con:
            return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0                                     # no such table yet: nothing was made


def _clear(path: Path, tables: tuple) -> None:
    with closing(sqlite3.connect(path)) as con, con:
        for table in tables:
            if con.execute("SELECT name FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                con.execute(f"DELETE FROM {table}")


@pytest.fixture(autouse=True)
def _no_users_left_in_the_shared_database():
    """A test that makes a user (the first administrator's setup, an invitation) or a person of local sign-in (an account, a member: identities.py, test_auth's
    `_admin` and `_user`) must do it in a database of its own (the `db` fixture of test_auth), which also gives it its own registry: users or accounts left in
    the shared test database make every later test on the same worker meet the sign-in, as 401s far from their cause (the completion program, P04). This
    names the test that left them."""
    yield
    users, members, accounts = _left(_SHARED_DB, "users"), _left(_SHARED_DB, "members"), _left(_SHARED_PLATFORM, "accounts")
    if users or members:                             # cleared, so that the tests after it run as they would
        _clear(_SHARED_DB, ("users", "sessions", "members", "role_presets"))
    if accounts:
        _clear(_SHARED_PLATFORM, ("accounts", "memberships", "refresh_tokens", "account_events"))
    if users or members or accounts:
        pytest.fail(f"this test left {users} user(s), {members} member(s) and {accounts} account(s) in the shared test database or its registry: give it the "
                    "`db` fixture (test_auth) so that its people are its own", pytrace=False)


@pytest.fixture(autouse=True)
def _fresh_sign_in():
    """The companies' kept contexts (supabase mode) and the signing keys read are forgotten between tests (companies.py, identity.py): a test that changes the
    mode or the key never reaches the next one through them."""
    from gridlytics import companies, identity
    companies.clear_cache()
    identity.clear_cache()
    yield


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """The API's rate limits (package P20) count every request of an address in a 60-second window, in memory, and every test client is the one address
    'testclient': a worker running hundreds of tests a minute would meet a limit no person meets, as 429s far from their cause. Each test starts with the
    window empty; the tests of the limits themselves set their own."""
    from gridlytics import auth
    auth.limiter().reset()
    yield


@pytest.fixture(autouse=True)
def _no_real_resolver(monkeypatch):
    """Solarlytics' netguard is not part of Gridlytics (no outbound fetches in v1); nothing to stub."""
    yield


# The lifted tests that asserted Solarlytics' own data (projects, role presets, the program-routes table) were re-aimed for Gridlytics in round 7 (audit
# parity-architecture-10): none is skipped by name any more, and test_hygiene holds that no test is.


#: The opt-in wall-clock benchmarks (round 7, audit pr-review-01 and tests-and-perf-12): a registered marker, not an ad-hoc switch per test. They run with
#: `-m slow` (only them) or with GRIDLYTICS_SLOW_TESTS=1 (them and the rest, HANDOFF section 9); any other run skips them by name, so the count shows.
#: Their correctness halves are not here: each benchmark keeps a default-suite test of what it asserts, on work no machine's load changes
#: (test_fix_r5_genset: the farm's 192-step plans from two marks; tests/budget.py: the solves' node budget; test_fix_r7_tests_ci: DAT-01's and DAT-08's
#: reads as searches of the minute table's key)
SLOW = "slow"


def pytest_configure(config):
    config.addinivalue_line("markers", f"{SLOW}: an opt-in wall-clock benchmark (solve times, read times); run with -m {SLOW} or "
                                       "GRIDLYTICS_SLOW_TESTS=1 on an idle machine, before changing optimise.py or telemetry.py (HANDOFF section 9)")


def _slow_runs(config) -> bool:
    return os.environ.get("GRIDLYTICS_SLOW_TESTS") == "1" or SLOW in (config.getoption("markexpr", "") or "").split()


def pytest_collection_modifyitems(config, items):
    slow = _slow_runs(config)
    for item in items:
        if item.get_closest_marker(SLOW) and not slow:
            item.add_marker(pytest.mark.skip(reason=f"an opt-in wall-clock benchmark: -m {SLOW} or GRIDLYTICS_SLOW_TESTS=1 runs it (HANDOFF section 9)"))
