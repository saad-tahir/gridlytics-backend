"""The shipped tariff data (plan Part 6.1, TAR-12), from the primary-source research note R9 (6 November 2026).

Every object carries a `source` with `verified`: primary (read in a NEPRA decision, an SRO, a DISCO or K-Electric page), secondary (press), or unverified.
Nothing here is hard-coded into the engine: tariffs.py seeds these as versions into each company's file and resolves them by date; a new notification is a
new version, never an edit (plan Part 6.2). Rates are GoP-applicable Rs/kWh before adjustments and taxes; fixed charges in Rs per kW per month on the higher of
the floor share of sanctioned load or the month's MDI, or Rs per consumer per month where `basis` says so.
"""
from __future__ import annotations

SRO_279 = {"kind": "nepra_sro", "ref": "SRO 279(I)/2026 dated 12-02-2026 (NEPRA decision TRF-100/2935-57 of 11-02-2026, Annex A-1 and Annex-C)",
           "url": "https://www.iesco.com.pk/tariff-guide", "retrieved_on": "2026-11-06", "verified": "primary"}
SRO_41 = {"kind": "press", "ref": "SRO 41-52(I)/2026 of 13-01-2026 (B-2 rates as reported by Dawn, 17-02-2026; other categories not read)",
          "url": "https://www.dawn.com/news/1973828", "retrieved_on": "2026-11-06", "verified": "secondary"}
TC_2023 = {"kind": "nepra_decision", "ref": "NEPRA Terms and Conditions of Tariff, clause 10 (LESCO determination 14-07-2023, Annex-V)",
           "url": "https://nepra.org.pk", "retrieved_on": "2026-11-06", "verified": "primary"}
KE_PAGE = {"kind": "ke_page", "ref": "K-Electric tariff structure and FAQs", "url": "https://ke.com.pk/faqs/", "retrieved_on": "2026-11-06", "verified": "primary"}
SRO_1501 = {"kind": "nepra_sro", "ref": "SRO 1501(I)/2026 of 07-09-2026 (QTA Q2 CY2026; NAEPP accounting para 23)", "url": "https://nepra.org.pk",
            "retrieved_on": "2026-11-06", "verified": "primary"}
SRO_2409 = {"kind": "nepra_sro", "ref": "SRO 2409(I)/2025 of 10-12-2025 and NEPRA decision of 09-12-2025 (Incremental Consumption Package)",
            "url": "https://nepra.org.pk", "retrieved_on": "2026-11-06", "verified": "primary"}
SRO_251 = {"kind": "nepra_sro", "ref": "NEPRA (Prosumer) Regulations 2026, SRO 251(I)/2026 of 09-02-2026, regulation 14; SRO 547(I)/2026",
           "url": "https://nepra.org.pk", "retrieved_on": "2026-11-06", "verified": "primary"}
POWER_DIV = {"kind": "manual", "ref": "Power Division 'Electricity Billing' page; KE page; FBR s.235 cards (rates as R9 section 7)",
             "url": "https://power.gov.pk", "retrieved_on": "2026-11-06", "verified": "secondary"}
OGRA = {"kind": "ogra", "ref": "OGRA daily HSD notifications as reported by APP, Dawn and Business Recorder", "url": "https://www.dawn.com/news/2042367",
        "retrieved_on": "2026-11-06", "verified": "secondary"}


def _card(peak=None, off=None, flat=None, fixed=0.0, basis="per_kw_month", floor=0.25, tou=True):
    return {"peak": peak, "off_peak": off, "flat": flat, "fixed": fixed, "fixed_basis": basis, "floor_share": floor, "tou": tou}


#: The CY2026 GoP-applicable schedule, SRO 279(I)/2026 (R9 section 1.2); A-3 and B-1(b) fixed charges are per consumer (R9 correction)
CARDS_279 = {
    "A-1(b)": _card(46.85, 34.53, fixed=675.0, floor=0.50),
    "A-2(a)": _card(flat=37.44, fixed=1000.0, basis="per_consumer_month", tou=False),
    "A-2(b)": _card(flat=39.76, fixed=1250.0, tou=False),
    "A-2(c)": _card(43.82, 35.15, fixed=1250.0),
    "A-3": _card(flat=42.48, fixed=1000.0, basis="per_consumer_month", tou=False),
    "B-1": _card(flat=26.23, fixed=1000.0, basis="per_consumer_month", tou=False),
    "B-1(b)": _card(35.74, 25.48, fixed=1000.0, basis="per_consumer_month"),
    "B-2(a)": _card(flat=26.16, fixed=1250.0, tou=False),
    "B-2(b)": _card(35.68, 22.83, fixed=1250.0),
    "B-3": _card(35.68, 23.67, fixed=1250.0),
    "B-4": _card(35.68, 23.38, fixed=1250.0),
    "B-5": _card(35.68, 23.13, fixed=1250.0),
    "C-1(b)": _card(flat=40.63, fixed=1250.0, tou=False),
    "C-1(c)": _card(46.31, 37.54, fixed=1250.0),
    "C-2": _card(46.31, 36.03, fixed=1250.0),
    "D-2(a)": _card(flat=28.90, fixed=400.0, basis="sanctioned_kw_month", tou=False),
    "D-2(b)": _card(29.54, 28.69, fixed=400.0, basis="sanctioned_kw_month"),
}
#: The January 2026 version (SRO 41-52): B-2(b) 36.68/27.41 reported by Dawn; a 50 percent floor before February (R9 1.2, 3). Other categories are not read:
#: they repeat SRO 279's and are marked unverified per card
CARDS_41 = {k: {**v, "floor_share": 0.50 if v["fixed_basis"] == "per_kw_month" else v["floor_share"], "unverified": True} for k, v in CARDS_279.items()}
CARDS_41["B-2(b)"] = {**CARDS_279["B-2(b)"], "peak": 36.68, "off_peak": 27.41, "floor_share": 0.50}
#: The FY2025-26 version (from 1 July 2025 until the CY2026 rebasing), the third version a pilot's twelve bills span (plan Part 6.1, TAR-12). R9 did not
#: read its rates: every card repeats the January 2026 card and is marked unverified, with the 50 percent floor of the 2023 Terms and Conditions. It lets
#: a 2025 bill or day be priced (and flagged) instead of failing; the owner replaces it with the notified rates from a 2025 bill (task O13)
CARDS_FY2526 = {k: {**v, "unverified": True} for k, v in CARDS_41.items()}
SRC_FY2526 = {"kind": "nepra_decision", "ref": "Uniform schedule FY2025-26 from 01-07-2025: rates not read (R9); placeholder repeating SRO 41-52(I)/2026's cards",
              "url": "https://nepra.org.pk", "retrieved_on": "2026-11-06", "verified": "unverified"}

SHIPPED = [
    {"kind": "schedule", "key": "national", "effective_from": "2025-07-01", "known_from": "2025-07-01", "status": "active",
     "body": {"name": "Uniform schedule FY2025-26 (rates not read: repeats January 2026's cards, unverified)", "cards": CARDS_FY2526}, "source": SRC_FY2526},
    {"kind": "schedule", "key": "national", "effective_from": "2026-01-01", "known_from": "2026-01-13", "status": "active",
     "body": {"name": "Uniform schedule, January 2026 (SRO 41-52(I)/2026)", "cards": CARDS_41}, "source": SRO_41},
    {"kind": "schedule", "key": "national", "effective_from": "2026-02-12", "known_from": "2026-02-12", "status": "active",
     "body": {"name": "Uniform schedule CY2026 (SRO 279(I)/2026)", "cards": CARDS_279}, "source": SRO_279},
    {"kind": "schedule", "key": "national", "effective_from": "2027-01-01", "known_from": "2026-11-06", "status": "awaiting_notification",
     "body": {"name": "Uniform schedule CY2027: awaiting NEPRA's notification (petitions heard 13-15 Oct 2026)", "cards": {}},
     "source": {**SRO_279, "ref": "CY2027 rebasing pending", "verified": "unverified"}},
    {"kind": "tou", "key": "xwdisco", "effective_from": "2023-07-14", "known_from": "2023-07-14", "status": "active",
     "body": {"seasons": [{"months": [12, 1, 2], "peak": ["17:00", "21:00"]}, {"months": [3, 4, 5], "peak": ["18:00", "22:00"]},
                          {"months": [6, 7, 8], "peak": ["19:00", "23:00"]}, {"months": [9, 10, 11], "peak": ["18:00", "22:00"]}], "dst_adjust": False},
     "source": TC_2023},
    {"kind": "tou", "key": "ke", "effective_from": "2019-01-01", "known_from": "2019-01-01", "status": "active",
     "body": {"seasons": [{"months": [4, 5, 6, 7, 8, 9, 10], "peak": ["18:30", "22:30"]}, {"months": [11, 12, 1, 2, 3], "peak": ["18:00", "22:00"]}],
              "dst_adjust": False}, "source": KE_PAGE},
    # Monthly adders by billing month (R9 section 6.3): flat Rs/kWh on every unit; exemptions per adder
    {"kind": "adder", "key": "qta", "effective_from": "2026-06-01", "known_from": "2026-05-19", "status": "active",
     "body": {"name": "QTA Q1 CY2026", "rate": -1.9857, "billing_months": ["2026-06", "2026-07", "2026-08"], "exempt": ["icp_units", "lifeline", "prepaid"]},
     "source": {**SRO_1501, "ref": "QTA Q1 CY2026 (Business Recorder, hearing 19-05-2026)", "verified": "secondary"}},
    {"kind": "adder", "key": "qta", "effective_from": "2026-09-01", "known_from": "2026-09-07", "status": "active",
     "body": {"name": "QTA Q2 CY2026", "rate": 0.5194, "billing_months": ["2026-09", "2026-10", "2026-11"], "exempt": ["icp_units", "lifeline", "prepaid"]},
     "source": SRO_1501},
    {"kind": "adder", "key": "fca", "effective_from": "2026-09-01", "known_from": "2026-09-05", "status": "active",
     "body": {"name": "FCA July 2026", "rate": 2.0581, "billing_months": ["2026-09"], "exempt": ["lifeline", "prepaid", "evcs"], "negative_exempt_icp": True},
     "source": {**SRO_1501, "ref": "FCA July 2026 (Dawn, 05-09-2026)", "verified": "secondary"}},
    {"kind": "adder", "key": "fca", "effective_from": "2026-10-01", "known_from": "2026-10-08", "status": "active",
     "body": {"name": "FCA August 2026", "rate": 1.11, "billing_months": ["2026-10"], "exempt": ["lifeline", "prepaid", "evcs"], "negative_exempt_icp": True},
     "source": {**SRO_1501, "ref": "FCA August 2026 (Tribune, 08-10-2026)", "verified": "secondary"}},
    {"kind": "adder", "key": "fca", "effective_from": "2026-11-01", "known_from": "2026-11-06", "status": "pending",
     "body": {"name": "FCA September 2026 (requested, decision reserved)", "rate": 2.5624, "billing_months": ["2026-11"], "exempt": ["lifeline", "prepaid", "evcs"],
              "negative_exempt_icp": True},
     "source": {**SRO_1501, "ref": "FCA September 2026 requested (Tribune, 30-10-2026)", "verified": "unverified"}},
    {"kind": "adder", "key": "dss", "effective_from": "2023-04-01", "known_from": "2023-04-01", "status": "active",
     "body": {"name": "Debt service surcharge (printed as FC or PHL surcharge)", "rate": 3.23, "billing_months": "all", "exempt": ["icp_units", "lifeline"],
              "category_rates": {"agricultural": 0.43}},
     "source": {**POWER_DIV, "ref": "Rs 3.23/kWh (Profit 15-01-2026; ICP decision names it DSS); 43 paisa for agricultural tube wells, 2023 structure "
                                    "(R9 section 7, secondary: confirm on a D-2 bill)"}},
    {"kind": "tax", "key": "national", "effective_from": "2025-07-01", "known_from": "2025-07-01", "status": "active",
     "body": {"gst": 0.18, "further_tax_no_strn": 0.03,
              "electricity_duty": {"sindh": {"industrial": 0.015, "commercial": 0.02, "bulk": 0.02, "agricultural": 0.01, "domestic": 0.015},
                                   "punjab": {"industrial": 0.015, "commercial": 0.015, "bulk": 0.015, "agricultural": 0.015, "domestic": 0.015},
                                   "kp": {"industrial": 0.015, "commercial": 0.015, "bulk": 0.015, "agricultural": 0.015, "domestic": 0.015},
                                   "balochistan": {"industrial": 0.015, "commercial": 0.015, "bulk": 0.015, "agricultural": 0.015, "domestic": 0.015},
                                   "ict": {"industrial": 0.015, "commercial": 0.015, "bulk": 0.015, "agricultural": 0.015, "domestic": 0.015}},
              "electricity_duty_verified": {"sindh": "primary", "punjab": "unverified", "kp": "unverified", "balochistan": "unverified", "ict": "unverified"},
              "s235": {"industrial": {"nil_upto": 500, "mid_rate": 0.10, "mid_upto": 20000, "base_above": 1950, "rate_above": 0.05},
                       "commercial": {"nil_upto": 500, "mid_rate": 0.10, "mid_upto": 20000, "base_above": 1950, "rate_above": 0.12}},
              "ptv": {"domestic": 35, "commercial": 60, "industrial": 0}, "muct_ke": 750, "punjab_selfgen_per_kwh": 0.04},
     "source": POWER_DIV},
    {"kind": "icp", "key": "national", "effective_from": "2025-12-01", "known_from": "2025-12-10", "status": "active",
     "body": {"rate": 22.98, "reference_months": "2023-12..2024-11", "escalation": 0.028, "first_month": "2025-12", "last_month": "2028-11",
              "categories": ["B-1", "B-1(b)", "B-2(a)", "B-2(b)", "B-3", "B-4", "B-5", "D-2(a)", "D-2(b)"],
              "carries": ["positive_fca"], "not_carries": ["qta", "dss", "negative_fca"],
              "load_factors": {"B-1": 0.40, "B-2": 0.50, "B-3": 0.60, "B-4": 0.50}, "review_pending": "5 Oct 2026 hearing, no notified outcome"},
     "source": SRO_2409},
    {"kind": "prosumer", "key": "net_billing", "effective_from": "2026-02-09", "known_from": "2026-09-07", "status": "active",
     "body": {"naepp": 8.13, "quarter": "2026-04..2026-06", "settlement": "billing_cycle_carry_or_quarterly", "tou_export_schedule": None},
     "source": SRO_251},
    {"kind": "prosumer", "key": "net_metering_legacy", "effective_from": "2015-09-01", "known_from": "2026-09-07", "status": "active",
     "body": {"netting": "by_period", "surplus_rate": "off_peak", "accounting_rate": 25.32, "netting_verified": "unverified"},
     "source": {**SRO_251, "ref": "SRO 547(I)/2026 keeps 2015 agreements to expiry; netting by period is DISCO practice (secondary, unverified)"}},
]

#: Daily HSD pump prices (R9 section 9.1), Rs per litre
FUEL_PRICES = [("hsd", "2026-10-30", 400.67), ("hsd", "2026-10-31", 402.96), ("hsd", "2026-11-03", 395.20), ("hsd", "2026-11-04", 391.52),
               ("hsd", "2026-11-05", 387.81), ("hsd", "2026-11-06", 382.25), ("gas_captive", "2026-07-01", 4200.0), ("lng_captive", "2026-07-01", 4800.0)]
FUEL_SOURCE = {"hsd": OGRA, "gas_captive": {**OGRA, "ref": "Captive gas: placeholder Rs per MMBtu, enter the site's contract price", "verified": "unverified"},
               "lng_captive": {**OGRA, "ref": "Captive RLNG: placeholder Rs per MMBtu, enter the site's contract price", "verified": "unverified"}}

#: The category's kind for electricity duty and s.235
def category_class(category: str) -> str:
    c = category.upper()
    if c.startswith("A-1"):
        return "domestic"
    if c.startswith("A-"):
        return "commercial"
    if c.startswith("B-"):
        return "industrial"
    if c.startswith("C-"):
        return "bulk"
    if c.startswith("D-"):
        return "agricultural"
    return "commercial"
