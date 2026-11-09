"""How the plan reads scale with a site's plan history (no pruning): a synthetic workspace file in scratch holding N realistic plan rows for one site
(the template is a real demo plan from the copy), then the queries plans.py and reports.py run, timed at 2,500 / 5,000 / 10,000 rows.
python -I plans_growth.py <copy company db> <out dir>"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

src, outdir = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
s.row_factory = sqlite3.Row
tpl = dict(s.execute("SELECT * FROM plans WHERE site_id='6d4db49bf87e' AND steps IS NOT NULL ORDER BY generated_at DESC LIMIT 1").fetchone())
ddl = s.execute("SELECT sql FROM sqlite_master WHERE name='plans'").fetchone()[0]
db = f"{outdir}/plans_growth.db"
import os
if os.path.exists(db):
    os.remove(db)
c = sqlite3.connect(db)
c.execute("PRAGMA journal_mode=WAL")
c.execute(ddl)
c.execute("CREATE INDEX plans_site ON plans (site_id, generated_at)")
t_end = datetime(2026, 11, 9, tzinfo=timezone.utc)
SITE = "s1"


def iso(t):
    return t.isoformat(timespec="seconds")


def fill(n_total, have):
    rows = []
    for i in range(have, n_total):
        t = t_end - timedelta(minutes=15 * (n_total - i))
        rows.append((f"p{i:06d}", SITE, iso(t), iso(t), iso(t), iso(t + timedelta(hours=48)), "a", "superseded", "", "highs", 0.02, 1.0, "h",
                     tpl["summary"], tpl["steps"], tpl["slots"], 0))
    c.executemany("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, solver, solve_seconds, "
                  "objective_rs, config_hash, summary, steps, slots, applied) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.execute("UPDATE plans SET status='active' WHERE id = (SELECT id FROM plans ORDER BY generated_at DESC LIMIT 1)")
    c.commit()


def T(label, f, n=3):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        f()
        ts.append(time.perf_counter() - t)
    return round(min(ts) * 1000, 1)


have = 0
res = []
for n in (2500, 5000, 10000):
    fill(n, have)
    have = n
    c2 = sqlite3.connect(db)
    c2.row_factory = sqlite3.Row
    mid = iso(t_end - timedelta(days=2))

    def active_at():
        rows = c2.execute("SELECT id, status, reason, summary FROM plans WHERE site_id = ? AND belief_time <= ? "
                          "ORDER BY belief_time DESC, generated_at DESC, rowid DESC LIMIT 200", (SITE, mid)).fetchall()
        r = c2.execute("SELECT * FROM plans WHERE id = ?", (rows[0]["id"],)).fetchone()
        json.loads(r["steps"])

    def current():
        r = c2.execute("SELECT * FROM plans WHERE site_id = ? AND status = 'active' ORDER BY generated_at DESC LIMIT 1", (SITE,)).fetchone()
        json.loads(r["steps"])

    def history50():
        c2.execute("SELECT * FROM plans WHERE site_id = ? ORDER BY generated_at DESC LIMIT 50", (SITE,)).fetchall()

    def report_month():
        for d in range(31):
            day = (t_end - timedelta(days=31 - d)).date().isoformat()
            c2.execute("SELECT id FROM plans WHERE site_id = ? AND substr(belief_time,1,10) = ? AND status != 'failed'", (SITE, day)).fetchall()

    def founder_counts():
        c2.execute("SELECT COUNT(*) FROM plans").fetchone()
        c2.execute("SELECT COUNT(*) FROM plans WHERE generated_at >= ?", (iso(t_end - timedelta(days=30)),)).fetchone()

    row = {"plans": n, "days_of_history": round(n / 96, 1), "file_mb": round(os.path.getsize(db) / 1e6),
           "active_at_ms": T("active_at", active_at), "current_ms": T("current", current), "history50_ms": T("history50", history50),
           "report_month_plan_ids_ms": T("report", report_month, 1), "founder_counts_ms": T("founder", founder_counts)}
    res.append(row)
    print(json.dumps(row), flush=True)
os.remove(db)
for ext in ("-wal", "-shm"):
    if os.path.exists(db + ext):
        os.remove(db + ext)
