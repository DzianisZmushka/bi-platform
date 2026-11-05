"""Deterministic generator for the erp-sales example.

SAP-style sales data with the properties enterprise sources always have:
composite document keys (VBELN + POSNR), multi-currency amounts with exchange
rates, a fiscal calendar, a document flow (order -> billing), a product
hierarchy and a few technical column names the model must hide.

Standard library only. Same seed, same files, every time.

Tables (SAP-ish names on purpose):
  t001   company codes                 BUKRS, BUTXT, WAERS (local currency)
  tvko   sales organisations           VKORG, VTEXT, BUKRS
  kna1   customers                     KUNNR, NAME1, LAND1, KTOKD (account group), ERDAT
  mara   materials                     MATNR, MAKTX, MATKL (group), PRDHA (hierarchy), MEINS
  t179t  product hierarchy texts       PRODH, VTEXT, STUFE (level)
  tcurr  exchange rates to USD         FCURR, GDATU (valid from), UKURS
  calendar                             DATUM, FISCAL_YEAR, FISCAL_PERIOD, PERIOD_START
  vbak   sales order headers           VBELN, ERDAT, AUDAT, VKORG, KUNNR, WAERK, AUART, VKBUR
  vbap   sales order items             VBELN, POSNR, MATNR, KWMENG, NETWR, WAVWR, ABGRU
  vbrp   billing items                 VBELN (billing doc), POSNR, AUBEL, AUPOS, FKDAT, FKIMG, NETWR
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

SEED = 20261105
OUT = Path(__file__).resolve().parent
START = date(2025, 1, 1)
END = date(2026, 10, 31)

rng = random.Random(SEED)


# ---------------------------------------------------------------- helpers


def write(name: str, header: list[str], rows: list[list]) -> None:
    with (OUT / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"{name:10} {len(rows):>7} rows")


def daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def fiscal(d: date) -> tuple[int, int]:
    """Fiscal year starts 1 April (K4-style variant with a shift)."""
    if d.month >= 4:
        return d.year, d.month - 3
    return d.year - 1, d.month + 9


# ---------------------------------------------------------- master data


company_codes = [
    ("1000", "Nordic Industrial AG", "EUR"),
    ("2000", "Nordic Industrial US Inc", "USD"),
    ("3000", "Nordic Industrial Gulf FZE", "AED"),
]

sales_orgs = [
    ("DE01", "Germany", "1000"),
    ("PL01", "Poland", "1000"),
    ("US01", "USA East", "2000"),
    ("US02", "USA West", "2000"),
    ("AE01", "UAE", "3000"),
]

# sales org -> list of (country, weight)
org_countries = {
    "DE01": [("DE", 6), ("AT", 2), ("CH", 1), ("NL", 1)],
    "PL01": [("PL", 5), ("CZ", 2), ("LT", 1)],
    "US01": [("US", 8), ("CA", 1)],
    "US02": [("US", 7), ("MX", 2)],
    "AE01": [("AE", 5), ("SA", 3), ("QA", 1), ("OM", 1)],
}

account_groups = [("KUNA", "Key account", 2), ("DEBI", "Standard", 6), ("CPD", "One-time", 1)]

# product hierarchy: 3 levels, SAP-style concatenated codes (5+5+8)
hierarchy = {
    "Pumps": {
        "Centrifugal": ["CP-100", "CP-200", "CP-350", "CP-500"],
        "Diaphragm": ["DP-10", "DP-25", "DP-50"],
    },
    "Valves": {
        "Ball": ["BV-15", "BV-25", "BV-50", "BV-80"],
        "Gate": ["GV-50", "GV-100"],
        "Control": ["CV-25A", "CV-50A", "CV-100S"],
    },
    "Services": {
        "Maintenance": ["SVC-MAINT-STD", "SVC-MAINT-PRM"],
        "Installation": ["SVC-INST-01"],
    },
}

material_groups = {"Pumps": "ZPMP", "Valves": "ZVLV", "Services": "ZSRV"}

# base list price in USD and unit cost ratio by level-1
price_band = {"Pumps": (1800, 9500, 0.62), "Valves": (120, 2400, 0.55), "Services": (400, 3000, 0.40)}

order_types = [("OR", "Standard order", 8), ("ZRE", "Returns", 1), ("ZFD", "Free of charge", 0.3)]

reject_reasons = ["", "", "", "", "", "", "", "", "", "01", "02", "Z1"]  # mostly empty

# monthly USD rates: EUR->USD, AED->USD (fixed peg), plus noise
def rates() -> list[list]:
    rows = []
    d = date(2025, 1, 1)
    eur = 1.04
    while d <= END:
        eur = max(0.98, min(1.20, eur + rng.uniform(-0.02, 0.025)))
        rows.append(["EUR", d.isoformat(), round(eur, 5)])
        rows.append(["AED", d.isoformat(), 0.27229])
        rows.append(["USD", d.isoformat(), 1.0])
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    return rows


# ------------------------------------------------------------- generate


def gen_master() -> tuple[list, list, list, list, dict]:
    t001 = [[b, t, w] for b, t, w in company_codes]
    tvko = [[v, t, b] for v, t, b in sales_orgs]

    # product hierarchy texts + materials
    t179t: list[list] = []
    mara: list[list] = []
    mat_meta: dict[str, tuple[str, str, float, float]] = {}  # MATNR -> (L1, PRODH, price, cost)
    for i1, (l1, subs) in enumerate(hierarchy.items(), start=1):
        c1 = f"{i1:05d}"
        t179t.append([c1, l1, 1])
        for i2, (l2, mats) in enumerate(subs.items(), start=1):
            c2 = c1 + f"{i2:05d}"
            t179t.append([c2, l2, 2])
            for i3, m in enumerate(mats, start=1):
                c3 = c2 + f"{i3:08d}"
                t179t.append([c3, m, 3])
                matnr = f"{(i1 * 1000000 + i2 * 10000 + i3):018d}"
                lo, hi, cost_ratio = price_band[l1]
                price = round(rng.uniform(lo, hi), 2)
                cost = round(price * cost_ratio * rng.uniform(0.9, 1.1), 2)
                unit = "H" if l1 == "Services" else "PC"
                mara.append([matnr, f"{m} {l2.lower()} {l1.lower()[:-1]}", material_groups[l1], c3, unit])
                mat_meta[matnr] = (l1, c3, price, cost)

    # customers
    kna1: list[list] = []
    cust_meta: dict[str, tuple[str, str]] = {}  # KUNNR -> (VKORG, country)
    kunnr = 100000
    names = ["Voss", "Brenner", "Kowalski", "Lindqvist", "Okafor", "Haddad", "Mendes", "Novak",
             "Reyes", "Tanaka", "Al Farsi", "Dubois", "Schäfer", "Petrov", "Nair", "Castillo"]
    suffix = ["GmbH", "S.A.", "Ltd", "LLC", "Sp. z o.o.", "AG", "Inc", "FZE", "BV", "s.r.o."]
    for vkorg, _, _ in sales_orgs:
        n = rng.randint(28, 46)
        for _ in range(n):
            kunnr += 1
            country = rng.choices([c for c, _ in org_countries[vkorg]], [w for _, w in org_countries[vkorg]])[0]
            ktokd = rng.choices([g for g, _, _ in account_groups], [w for _, _, w in account_groups])[0]
            erdat = START - timedelta(days=rng.randint(0, 2200)) if rng.random() < 0.7 else \
                START + timedelta(days=rng.randint(0, (END - START).days - 60))
            kna1.append([f"{kunnr:010d}", f"{rng.choice(names)} {rng.choice(suffix)}", country, ktokd, erdat.isoformat()])
            cust_meta[f"{kunnr:010d}"] = (vkorg, country)

    return t001, tvko, t179t, mara, {"mat": mat_meta, "cust": cust_meta, "kna1": kna1}


def gen_transactions(meta: dict, rate_rows: list[list]) -> tuple[list, list, list]:
    mat_meta = meta["mat"]
    cust_by_org: dict[str, list[str]] = {}
    for k, (vkorg, _) in meta["cust"].items():
        cust_by_org.setdefault(vkorg, []).append(k)
    cust_created = {row[0]: date.fromisoformat(row[4]) for row in meta["kna1"]}

    # rate lookup: (currency, month) -> usd
    rate_by_month = {(c, d[:7]): r for c, d, r in rate_rows}
    org_currency = {v: dict((b, w) for b, _, w in company_codes)[b] for v, _, b in sales_orgs}

    vbak: list[list] = []
    vbap: list[list] = []
    vbrp: list[list] = []
    vbeln = 1000000
    billing_no = 9000000

    # seasonality: Q1 slow, Q4 strong; gentle growth
    def daily_volume(d: date) -> float:
        season = {1: 0.8, 2: 0.85, 3: 1.0, 4: 1.0, 5: 1.05, 6: 1.1, 7: 0.9, 8: 0.85,
                  9: 1.1, 10: 1.2, 11: 1.25, 12: 1.0}[d.month]
        growth = 1 + 0.18 * ((d - START).days / (END - START).days)
        weekday = 0.25 if d.weekday() >= 5 else 1.0
        return 11 * season * growth * weekday

    for d in daterange(START, END):
        n_orders = max(0, int(rng.gauss(daily_volume(d), 2.5)))
        for _ in range(n_orders):
            vkorg = rng.choices([v for v, _, _ in sales_orgs], [5, 2, 6, 4, 3])[0]
            # customers only after they exist
            pool = [c for c in cust_by_org[vkorg] if cust_created[c] <= d]
            if not pool:
                continue
            kunnr = rng.choice(pool)
            auart = rng.choices([a for a, _, _ in order_types], [w for _, _, w in order_types])[0]
            waerk = org_currency[vkorg]
            vbeln += 1
            doc = f"{vbeln:010d}"
            audat = d - timedelta(days=rng.randint(0, 3))
            vkbur = f"{vkorg[:2]}{rng.randint(1, 3):02d}"
            vbak.append([doc, d.isoformat(), audat.isoformat(), vkorg, kunnr, waerk, auart, vkbur])

            n_items = rng.choices([1, 2, 3, 4, 6], [5, 4, 3, 2, 1])[0]
            rate = rate_by_month[(waerk, d.strftime("%Y-%m"))]
            for i in range(1, n_items + 1):
                posnr = f"{i * 10:06d}"
                matnr = rng.choice(list(mat_meta))
                l1, _, price_usd, cost_usd = mat_meta[matnr]
                qty = rng.choices([1, 2, 3, 5, 8, 12, 20], [6, 5, 4, 3, 2, 1, 1])[0]
                if l1 == "Services":
                    qty = rng.choice([4, 8, 16, 24, 40])
                discount = rng.choices([0, 0.03, 0.05, 0.08, 0.12, 0.18], [5, 3, 3, 2, 1, 1])[0]
                price_doc = price_usd / rate * (1 - discount)  # in document currency
                cost_doc = cost_usd / rate
                sign = -1 if auart == "ZRE" else (0 if auart == "ZFD" else 1)
                netwr = round(price_doc * qty * sign, 2)
                wavwr = round(cost_doc * qty * (1 if auart != "ZRE" else -1), 2)
                abgru = rng.choice(reject_reasons)
                vbap.append([doc, posnr, matnr, qty, netwr, wavwr, abgru])

                # billing: most non-rejected items are billed 2-25 days later
                if not abgru and auart != "ZFD" and rng.random() < 0.93:
                    fkdat = d + timedelta(days=rng.randint(2, 25))
                    if fkdat <= END:
                        billing_no += 1
                        bill_doc = f"{billing_no:010d}"
                        # partial billing on ~8% of multi-quantity items
                        fkimg = qty
                        if qty > 1 and rng.random() < 0.08:
                            fkimg = rng.randint(1, qty - 1)
                        bill_netwr = round(netwr * fkimg / qty, 2)
                        vbrp.append([bill_doc, "000010", doc, posnr, fkdat.isoformat(), fkimg, bill_netwr])

    return vbak, vbap, vbrp


def gen_calendar() -> list[list]:
    rows = []
    for d in daterange(START - timedelta(days=400), END + timedelta(days=60)):
        fy, fp = fiscal(d)
        period_start = date(fy if fp <= 9 else fy + 1, (fp + 3) if fp <= 9 else fp - 9, 1)
        rows.append([d.isoformat(), fy, fp, period_start.isoformat()])
    return rows


def main() -> None:
    t001, tvko, t179t, mara, meta = gen_master()
    tcurr = rates()
    vbak, vbap, vbrp = gen_transactions(meta, tcurr)

    write("t001", ["BUKRS", "BUTXT", "WAERS"], t001)
    write("tvko", ["VKORG", "VTEXT", "BUKRS"], tvko)
    write("kna1", ["KUNNR", "NAME1", "LAND1", "KTOKD", "ERDAT"], meta["kna1"])
    write("mara", ["MATNR", "MAKTX", "MATKL", "PRDHA", "MEINS"], mara)
    write("t179t", ["PRODH", "VTEXT", "STUFE"], t179t)
    write("tcurr", ["FCURR", "GDATU", "UKURS"], tcurr)
    write("calendar", ["DATUM", "FISCAL_YEAR", "FISCAL_PERIOD", "PERIOD_START"], gen_calendar())
    write("vbak", ["VBELN", "ERDAT", "AUDAT", "VKORG", "KUNNR", "WAERK", "AUART", "VKBUR"], vbak)
    write("vbap", ["VBELN", "POSNR", "MATNR", "KWMENG", "NETWR", "WAVWR", "ABGRU"], vbap)
    write("vbrp", ["VBELN", "POSNR", "AUBEL", "AUPOS", "FKDAT", "FKIMG", "NETWR"], vbrp)


if __name__ == "__main__":
    main()
