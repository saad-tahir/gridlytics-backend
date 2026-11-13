"""What Gridlytics follows from Boxlytics is recorded where a reviewer reads it (audit parity-architecture-08; UI/UX plan Part H, H1.1 and H10).

Boxlytics is another party's product: nothing in it is licensed for reuse, and no file of it is copied. The pieces Solarlytics followed from it at
`boxlytics@c429bef` (the founder's session token, the Supabase SQL), which came into Gridlytics with the lift, were named only in code comments, so a
reader of THIRD_PARTY_NOTICES.md would have concluded that none was used. The notice's section "Constructions from Boxlytics (another party)" now lists
each, and these tests hold it to the code: a file that names `boxlytics@c429bef` must be listed there, and each hash in the section is a full SHA-256.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
HEADING = "## Constructions from Boxlytics (another party), no files copied"
#: Where the backend's own code and SQL live (tests, docs and the notices themselves describe, they do not construct)
SOURCES = (ROOT / "backend" / "gridlytics", ROOT / "supabase")


def _section() -> str:
    text = NOTICES.read_text(encoding="utf-8")
    assert HEADING in text, "THIRD_PARTY_NOTICES.md has no Boxlytics section"
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _citing(marker: str) -> set[str]:
    found = set()
    for base in SOURCES:
        for path in base.rglob("*"):
            if path.suffix in (".py", ".sql") and "__pycache__" not in path.parts and marker in path.read_text(encoding="utf-8"):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_every_file_that_follows_boxlytics_at_c429bef_is_listed_in_the_notice():
    section = _section()
    citing = _citing("boxlytics@c429bef")
    assert "backend/gridlytics/founder.py" in citing, "the founder's token format names its Boxlytics origin"
    missing = sorted(p for p in citing if f"`{p}`" not in section)
    assert missing == [], f"these files follow Boxlytics at c429bef but the notice does not list them: {missing}"


def test_the_section_names_the_commit_the_owners_decision_and_full_hashes():
    section = _section()
    assert "`boxlytics@c429bef`" in section and "24 October 2026" in section
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    assert rows, "the section lists no file"
    hashes = re.findall(r"`([0-9a-f]{16,})`", section)
    assert hashes and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes), "a hash in the section is not a full SHA-256"


def test_the_plain_sha256_founder_hash_is_listed_as_a_compatibility_path_to_remove():
    section = _section()
    row = next(line for line in section.splitlines() if "plain SHA-256" in line)
    assert "backend/gridlytics/founder.py" in row and "to be removed" in row


def test_the_sql_rows_say_which_lines_follow_boxlytics_as_written():
    """The section said every construction was rewritten in Gridlytics' own code, yet grid_001 keeps two places line for line as Boxlytics' migrations
    write them: get_my_context's memberships answer and company_list_members' columns and order (a comparison of six-line runs, as the web app's
    scripts/provenance-check.mjs makes, found 12; UX 1 check). The rows of 003 and 011 name those lines, and the section's opening allows for them."""
    section = _section()
    assert "except the lines a row says follow Boxlytics as written" in section
    rows = {name: next(line for line in section.splitlines() if f"`migrations/{name}`" in line) for name in ("003_company_views.sql", "011_token_economy.sql")}
    assert "follow Boxlytics as written" in rows["003_company_views.sql"] and "lines 348 to 396" in rows["003_company_views.sql"]
    assert "follows Boxlytics as written" in rows["011_token_economy.sql"] and "lines 312 to 330" in rows["011_token_economy.sql"]
    sql = (ROOT / "supabase" / "migrations" / "grid_001_accounts_and_companies.sql").read_text(encoding="utf-8").splitlines()
    assert sql[311].strip() == "'memberships', (" and "company_list_members" in sql[347], "the line numbers in the notice moved: correct them"

