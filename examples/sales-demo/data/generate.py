"""Generate the sales-demo CSV files.

Deterministic (fixed seed), standard library only. Run from anywhere:

    uv run python examples/sales-demo/data/generate.py
"""

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent
START, END = date(2025, 1, 1), date(2026, 10, 31)

REGIONS = [  # id, name, demand weight
    ("emea", "EMEA", 1.0),
    ("na", "North America", 1.3),
    ("apac", "APAC", 0.7),
    ("latam", "Latin America", 0.35),
]
COUNTRIES = {
    "emea": ["Germany", "France", "United Kingdom", "Poland", "Spain"],
    "na": ["United States", "Canada"],
    "apac": ["Japan", "Australia", "Singapore", "India"],
    "latam": ["Brazil", "Mexico", "Chile"],
}
SEGMENTS = [("Enterprise", 0.15, 6.0), ("Mid-market", 0.35, 2.0), ("SMB", 0.5, 1.0)]  # share, avg qty
CHANNELS = [("Online", 0.5), ("Retail", 0.3), ("Partner", 0.2)]
CATALOG = {  # category -> subcategory -> price range
    "Hardware": {"Laptops": (900, 1800), "Monitors": (180, 600), "Accessories": (15, 120)},
    "Software": {"Licenses": (200, 1200), "Subscriptions": (20, 150)},
    "Services": {"Support": (300, 900), "Training": (400, 1500)},
}
MARGIN = {"Hardware": 0.25, "Software": 0.75, "Services": 0.45}
RETURN_RATE = {"Hardware": 0.07, "Software": 0.015, "Services": 0.015}
BRANDS = ["Northwind", "Contoso", "Fabrikam", "Tailspin"]


def write(name, header, rows):
    with open(OUT / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    print(f"{name}.csv: {len(rows)} rows")


def poisson(rng, lam):
    limit, k, p = math.exp(-lam), 0, 1.0  # Knuth; fine for small lambda
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def pick(rng, options, weight_index):
    return rng.choices(options, weights=[o[weight_index] for o in options])[0]


def main():
    rng = random.Random(20261105)
    days = [START + timedelta(n) for n in range((END - START).days + 1)]

    write("regions", ["region_id", "region_name"], [(r, n) for r, n, _ in REGIONS])
    write("calendar", ["date"], [(d.isoformat(),) for d in days])

    products = []
    for category, subs in CATALOG.items():
        for sub, (lo, hi) in subs.items():
            for i in range(1, 5):
                brand = rng.choice(BRANDS)
                price = round(rng.uniform(lo, hi), -1 if hi > 200 else 0)
                cost = round(price * (1 - MARGIN[category]) * rng.uniform(0.85, 1.15), 2)
                products.append((f"P{len(products) + 1:03d}", f"{brand} {sub} {i}", category,
                                 sub, brand, price, cost))
    write("products", ["product_id", "product_name", "category", "subcategory", "brand",
                       "list_price", "unit_cost"], products)

    customers = []
    for n in range(1, 401):
        region = pick(rng, REGIONS, 2)[0]
        segment = pick(rng, SEGMENTS, 1)[0]
        signup = START + timedelta(days=rng.randint(-730, 600))
        customers.append((f"C{n:04d}", f"Customer {n:04d}", segment,
                          rng.choice(COUNTRIES[region]), region, signup))
    write("customers", ["customer_id", "customer_name", "segment", "country", "region_id",
                        "signup_date"], [c[:5] + (c[5].isoformat(),) for c in customers])

    avg_qty = {s: q for s, _, q in SEGMENTS}
    region_of = {c[0]: c[4] for c in customers}
    lines, order_no = [], 0
    for d in days:
        growth = 1 + 0.25 * (d - START).days / 365
        season = 1 + 0.3 * math.sin(d.timetuple().tm_yday / 365 * 2 * math.pi - 1.2)
        weekday = 0.6 if d.weekday() >= 5 else 1.0
        active = [c for c in customers if c[5] <= d]
        for _ in range(poisson(rng, 9 * growth * season * weekday)):
            cust = rng.choice(active)
            channel = pick(rng, CHANNELS, 1)[0]
            order_no += 1
            for line_no in range(1, rng.randint(1, 4) + 1):
                p = rng.choice(products)
                qty = max(1, round(rng.expovariate(1 / avg_qty[cust[2]])))
                discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2 if channel == "Partner" else 0.1])
                returned = rng.random() < RETURN_RATE[p[2]]
                lines.append((f"O{order_no:06d}", line_no, d.isoformat(), cust[0], p[0], channel,
                              qty, p[5], discount, p[6], str(returned).lower()))
    write("order_lines", ["order_id", "line_no", "order_date", "customer_id", "product_id",
                          "channel", "quantity", "unit_price", "discount", "unit_cost",
                          "returned"], lines)

    # Monthly revenue targets per region: same month last year +12 %, else this month +5 %.
    actual = {}
    for _, _, d, cust, _, _, qty, price, discount, _, _ in lines:
        key = (d[:7], region_of[cust])
        actual[key] = actual.get(key, 0) + qty * price * (1 - discount)
    targets = []
    for d in days:
        if d.day != 1:
            continue
        for region, _, _ in REGIONS:
            prior = actual.get((f"{d.year - 1}-{d.month:02d}", region))
            base = prior * 1.12 if prior else actual.get((d.isoformat()[:7], region), 0) * 1.05
            targets.append((d.isoformat(), region, int(round(base, -3))))
    write("targets", ["month", "region_id", "revenue_target"], targets)


if __name__ == "__main__":
    main()
