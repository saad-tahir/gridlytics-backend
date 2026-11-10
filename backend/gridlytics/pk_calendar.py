"""Pakistan's public holidays, Eid and Ramadan for the load forecaster's day types (plan Part 5.2, FOR-06: "working, half-day, off day, public holiday
from the Pakistan calendar, Ramadan working day, Eid").

A dated table, entered 2026-11-09, instead of the `holidays` package (not a dependency of the API; its Pakistan lunar dates are estimates too):

* Fixed-date public holidays, from the federal government's annual list (Cabinet Division, Government of Pakistan): Kashmir Solidarity Day (5 February),
  Pakistan Day (23 March), Labour Day (1 May), Youm-e-Takbeer (28 May, from 2024), Independence Day (14 August), Iqbal Day (9 November, restored from
  2022) and Quaid-e-Azam Day (25 December, also Christmas).
* Lunar holidays as observed in Pakistan (the dates the federal government announced after the Ruet-e-Hilal Committee's moon sighting), in OBSERVED:
  Ramadan, Eid ul-Fitr, Eid ul-Adha, Ashura (9 and 10 Muharram) and Eid Milad-un-Nabi (12 Rabi ul-Awwal), 2024 and 2025.
* Every other year's lunar dates are estimated from the arithmetic (tabular) Islamic calendar: Ramadan from 1 Ramadan to the day before Eid, Eid ul-Fitr
  1 to 3 Shawwal, Eid ul-Adha 10 to 12 Dhul-Hijjah, Ashura 9 and 10 Muharram, Eid Milad-un-Nabi 12 Rabi ul-Awwal. Against the observed years the estimate
  falls on the announced day or one day early (1 Ramadan and 12 Rabi ul-Awwal were a day later in Pakistan in 2024 and 2025); `estimated` marks them, and
  the table is extended as a year's dates are announced.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from functools import lru_cache

#: (month, day, name, first year it is a federal holiday)
FIXED = ((2, 5, "Kashmir Solidarity Day", 1990), (3, 23, "Pakistan Day", 1956), (5, 1, "Labour Day", 1972), (5, 28, "Youm-e-Takbeer", 2024),
         (8, 14, "Independence Day", 1947), (11, 9, "Iqbal Day", 2022), (12, 25, "Quaid-e-Azam Day", 1947))

#: the lunar dates announced in Pakistan, by Gregorian year: kind ("ramadan", "eid" or "holiday"), first and last day (inclusive), name
OBSERVED = {
    2024: (("ramadan", "2024-03-12", "2024-04-09", "Ramadan"), ("eid", "2024-04-10", "2024-04-13", "Eid ul-Fitr"),
           ("eid", "2024-06-17", "2024-06-19", "Eid ul-Adha"), ("holiday", "2024-07-16", "2024-07-17", "Ashura"),
           ("holiday", "2024-09-17", "2024-09-17", "Eid Milad-un-Nabi")),
    2025: (("ramadan", "2025-03-02", "2025-03-30", "Ramadan"), ("eid", "2025-03-31", "2025-04-02", "Eid ul-Fitr"),
           ("eid", "2025-06-06", "2025-06-09", "Eid ul-Adha"), ("holiday", "2025-07-05", "2025-07-06", "Ashura")),
}
#: the lunar holidays of OBSERVED's years that are not in it (estimated there too): Eid Milad-un-Nabi 2025
ESTIMATED_IN_OBSERVED = {2025: ("Eid Milad-un-Nabi",)}


def _hijri(y: int, m: int, d: int) -> date:
    """The Gregorian date of an arithmetic (tabular, civil) Islamic calendar date."""
    jd = d + math.ceil(29.5 * (m - 1)) + (y - 1) * 354 + (3 + 11 * y) // 30 + 1948439.5 - 1
    return date.fromordinal(int(jd - 1721424.5))


def _span(a: date, b: date):
    while a <= b:
        yield a
        a += timedelta(days=1)


def _estimated(year: int) -> list[tuple[str, date, date, str]]:
    """The lunar holidays of the arithmetic calendar that touch a Gregorian year."""
    out = []
    ah0 = int((year - 622) * 33 / 32)  # the Islamic year running at the start of the Gregorian year, give or take one
    for ah in range(ah0 - 1, ah0 + 3):
        fitr = _hijri(ah, 10, 1)
        out += [("ramadan", _hijri(ah, 9, 1), fitr - timedelta(days=1), "Ramadan"), ("eid", fitr, fitr + timedelta(days=2), "Eid ul-Fitr"),
                ("eid", _hijri(ah, 12, 10), _hijri(ah, 12, 12), "Eid ul-Adha"), ("holiday", _hijri(ah, 1, 9), _hijri(ah, 1, 10), "Ashura"),
                ("holiday", _hijri(ah, 3, 12), _hijri(ah, 3, 12), "Eid Milad-un-Nabi")]
    return [x for x in out if x[1].year <= year <= x[2].year]


#: the precedence of a day's kinds when two fall on it (Eid over a public holiday over Ramadan)
_RANK = {"eid": 3, "holiday": 2, "ramadan": 1}


@lru_cache(maxsize=64)
def year_days(year: int) -> dict[date, tuple[str, str, bool]]:
    """The year's special days: {date: (kind, name, estimated)}, kind "eid", "holiday" (a public holiday) or "ramadan" (a day of Ramadan)."""
    out: dict[date, tuple[str, str, bool]] = {}

    def put(d: date, kind: str, name: str, est: bool) -> None:
        if d.year == year and (d not in out or _RANK[kind] > _RANK[out[d][0]]):
            out[d] = (kind, name, est)

    for m, d, name, since in FIXED:
        if year >= since:
            put(date(year, m, d), "holiday", name, False)
    if year in OBSERVED:
        lunar = [(k, date.fromisoformat(a), date.fromisoformat(b), n, False) for k, a, b, n in OBSERVED[year]]
        keep = set(ESTIMATED_IN_OBSERVED.get(year, ()))
        lunar += [(k, a, b, n, True) for k, a, b, n in _estimated(year) if n in keep]
    else:
        lunar = [(k, a, b, n, True) for k, a, b, n in _estimated(year)]
    for kind, a, b, name, est in lunar:
        for d in _span(a, b):
            put(d, kind, name, est)
    return out


def day(d: date) -> tuple[str, str, bool] | None:
    """(kind, name, estimated) of a special day, or None for an ordinary one."""
    return year_days(d.year).get(d)
