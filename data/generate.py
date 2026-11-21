"""Generate the synthetic procurement dataset and plant known compliance issues.

Outputs (all under data/):
  suppliers.csv, contracts.csv, purchase_orders.csv, invoices.csv, credits.csv
  contracts/<contract_id>.md        contract documents the agent reads
  procurement.db                    SQLite database built from the CSVs
  answers/ground_truth.csv          every planted issue (answer key, never shown to the agent)
  answers/contract_terms_truth.json the true terms behind each contract (extraction answer key)

Run: python data/generate.py   (deterministic: same seed, same data)
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

SEED = 42
DATA = Path(__file__).resolve().parent
PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 9, 30)
QUARTERS = {
    "2026Q1": (date(2026, 1, 1), date(2026, 3, 31)),
    "2026Q2": (date(2026, 4, 1), date(2026, 6, 30)),
    "2026Q3": (date(2026, 7, 1), date(2026, 9, 30)),
}

rng = random.Random(SEED)


# ---------------------------------------------------------------- master data

@dataclass
class Item:
    code: str
    description: str
    unit: str
    price: float
    qty: tuple[int, int]  # typical order quantity range


@dataclass
class Contract:
    contract_id: str
    supplier_id: str
    category: str
    start: date
    end: date
    payment_terms_days: int
    items: list[Item]
    rebate: dict | None = None  # {"pct": 2.0, "quarterly_threshold": 150000}
    style: str = "table"        # how the pricing schedule is written: table | prose
    orders_per_month: tuple[int, int] = (2, 4)
    extra_clauses: list[str] = field(default_factory=list)


SUPPLIERS = [
    # supplier_id, name, category, country, contracted
    ("S01", "Apex Industrial Metals", "Direct Materials", "US", True),
    ("S02", "Northline Polymers", "Direct Materials", "US", True),
    ("S03", "Keystone MRO Supply", "MRO", "US", True),
    ("S04", "Brightway Facility Services", "Facilities", "US", True),
    ("S05", "Swift Freight Partners", "Logistics & Freight", "US", True),
    ("S06", "Harbor Logistics Group", "Logistics & Freight", "US", True),
    ("S07", "CloudPeak Software", "IT & Software", "US", True),
    ("S08", "Ironclad Safety Products", "MRO", "US", True),
    ("S09", "Meridian IT Hardware", "IT & Software", "US", True),
    ("S10", "Precision Tooling Co", "Direct Materials", "MX", True),
    ("S11", "QuickParts Online", "MRO", "US", False),
    ("S12", "Metro Courier Express", "Logistics & Freight", "US", False),
    ("S13", "Summit Strategy Consulting", "Professional Services", "US", False),
    ("S14", "Ledger & Pine Advisory", "Professional Services", "US", False),
    ("S15", "TechMart Retail", "IT & Software", "US", False),
]
SUPPLIER_NAME = {s[0]: s[1] for s in SUPPLIERS}

CONTRACTS = [
    Contract("C01", "S01", "Direct Materials", date(2025, 7, 1), date(2027, 6, 30), 45, [
        Item("DM-1010", "Cold-rolled steel sheet, 16 gauge", "ton", 1185.00, (8, 20)),
        Item("DM-1022", "Aluminum bar stock 6061, 2 in", "ft", 14.25, (400, 1200)),
        Item("DM-1031", "Stainless tube 304, 1.5 in", "ft", 22.80, (200, 700)),
    ], rebate={"pct": 2.0, "quarterly_threshold": 150000}, style="table", orders_per_month=(4, 5)),
    Contract("C02", "S02", "Direct Materials", date(2025, 10, 1), date(2027, 9, 30), 30, [
        Item("DM-2001", "HDPE resin pellets", "kg", 1.92, (4000, 12000)),
        Item("DM-2014", "Polycarbonate sheet 4x8, 1/4 in", "sheet", 168.00, (20, 80)),
    ], rebate={"pct": 3.0, "quarterly_threshold": 60000}, style="prose", orders_per_month=(2, 3)),
    Contract("C03", "S03", "MRO", date(2025, 1, 1), date(2027, 12, 31), 30, [
        Item("MRO-114", "Nitrile gloves, box of 100", "box", 11.40, (40, 160)),
        Item("MRO-207", "Hydraulic oil ISO 46, 5 gal", "pail", 64.50, (6, 30)),
        Item("MRO-311", "Ball bearing 6205-2RS", "each", 7.85, (50, 300)),
        Item("MRO-415", "V-belt B48", "each", 18.20, (10, 60)),
    ], style="table", orders_per_month=(3, 4)),
    Contract("C04", "S04", "Facilities", date(2024, 7, 1), date(2026, 6, 30), 30, [
        Item("FAC-100", "Janitorial service, Plant A (monthly)", "month", 9400.00, (1, 1)),
        Item("FAC-110", "HVAC preventive maintenance visit", "visit", 1250.00, (1, 3)),
    ], style="prose", orders_per_month=(1, 2),
       extra_clauses=["Renewal is not automatic. Any extension must be signed by both parties before the expiration date."]),
    Contract("C05", "S05", "Logistics & Freight", date(2025, 4, 1), date(2027, 3, 31), 30, [
        Item("LOG-501", "FTL dry van, Chicago to Dallas", "load", 2350.00, (2, 8)),
        Item("LOG-502", "FTL dry van, Chicago to Atlanta", "load", 2180.00, (2, 6)),
        Item("LOG-510", "LTL pallet, regional", "pallet", 145.00, (10, 40)),
    ], style="table", orders_per_month=(3, 4)),
    Contract("C06", "S06", "Logistics & Freight", date(2025, 9, 1), date(2027, 8, 31), 45, [
        Item("LOG-601", "Ocean container 40ft, Shanghai to Long Beach", "container", 3900.00, (1, 3)),
        Item("LOG-610", "Drayage, Long Beach port to DC", "move", 640.00, (1, 4)),
    ], rebate={"pct": 1.5, "quarterly_threshold": 80000}, style="prose", orders_per_month=(1, 2)),
    Contract("C07", "S07", "IT & Software", date(2025, 2, 1), date(2028, 1, 31), 60, [
        Item("IT-701", "ERP user license, monthly", "seat", 85.00, (40, 60)),
        Item("IT-705", "Premium support add-on, monthly", "month", 1800.00, (1, 1)),
    ], style="table", orders_per_month=(1, 1)),
    Contract("C08", "S08", "MRO", date(2025, 6, 1), date(2027, 5, 31), 30, [
        Item("MRO-801", "Safety glasses, anti-fog", "each", 4.60, (100, 400)),
        Item("MRO-812", "Hard hat, Type I Class E", "each", 21.75, (20, 80)),
        Item("MRO-820", "Hi-vis vest, ANSI Class 2", "each", 8.90, (50, 200)),
    ], style="prose", orders_per_month=(2, 3)),
    Contract("C09", "S09", "IT & Software", date(2025, 11, 1), date(2027, 10, 31), 30, [
        Item("IT-901", "Business laptop, 14 in, 32GB", "each", 1340.00, (3, 15)),
        Item("IT-905", "27 in monitor", "each", 289.00, (5, 25)),
        Item("IT-910", "Docking station USB-C", "each", 176.00, (5, 25)),
    ], style="table", orders_per_month=(1, 3)),
    Contract("C10", "S10", "Direct Materials", date(2025, 3, 1), date(2027, 2, 28), 60, [
        Item("DM-3005", "Carbide end mill, 1/2 in, 4 flute", "each", 48.50, (20, 80)),
        Item("DM-3012", "CNC fixture plate, machined", "each", 620.00, (2, 10)),
    ], style="table", orders_per_month=(1, 2)),
]
CONTRACT_BY_ID = {c.contract_id: c for c in CONTRACTS}

# Off-contract items: what a requester buys from a non-contracted supplier instead.
NON_CONTRACT_ITEMS = {
    "S11": [("QP-114", "Nitrile gloves, box of 100 (web order)", "box", 14.99, (20, 60)),
            ("QP-311", "Ball bearing 6205-2RS (web order)", "each", 10.40, (40, 120))],
    "S12": [("MC-200", "Expedited LTL pallet, regional", "pallet", 210.00, (8, 20))],
    "S15": [("TM-901", "Business laptop, 14 in, 32GB (retail)", "each", 1599.00, (2, 6))],
    "S13": [("PS-100", "Strategy workshop, daily rate", "day", 4200.00, (1, 4))],
    "S14": [("PS-200", "Audit advisory, hourly", "hour", 275.00, (10, 60))],
}

REQUESTERS = ["J. Alvarez", "M. Chen", "R. Okafor", "S. Patel", "L. Novak", "T. Brooks", "A. Haddad"]
BUSINESS_UNITS = ["Plant A", "Plant B", "Distribution", "Corporate"]


def rand_date(start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def quarter_of(d: date) -> str:
    for q, (a, b) in QUARTERS.items():
        if a <= d <= b:
            return q
    raise ValueError(d)


# ---------------------------------------------------------------- generation

def generate():
    pos: list[dict] = []
    issues: list[dict] = []
    po_seq = iter(range(10001, 99999))

    def new_po(d, supplier_id, category, code, desc, qty, price, contract_id):
        po = {
            "po_id": f"PO-{next(po_seq)}",
            "po_date": d.isoformat(),
            "supplier_id": supplier_id,
            "category": category,
            "item_code": code,
            "description": desc,
            "quantity": qty,
            "unit_price": round(price, 2),
            "amount": round(qty * round(price, 2), 2),
            "contract_id": contract_id or "",
            "requester": rng.choice(REQUESTERS),
            "business_unit": rng.choice(BUSINESS_UNITS),
        }
        pos.append(po)
        return po

    # 1) Normal contracted purchasing at contract price, month by month.
    for c in CONTRACTS:
        for month in range(1, 10):
            m_start = date(2026, month, 1)
            m_end = (date(2026, month + 1, 1) - timedelta(days=1)) if month < 12 else date(2026, 12, 31)
            if c.end < m_start:
                continue  # contract over: normal buying stops (expired buys are planted below)
            for _ in range(rng.randint(*c.orders_per_month)):
                it = rng.choice(c.items)
                d = rand_date(m_start, min(m_end, c.end))
                new_po(d, c.supplier_id, c.category, it.code, it.description,
                       rng.randint(*it.qty), it.price, c.contract_id)

    # 2) Legitimate non-contract spend (Professional Services has no contract).
    for sid in ("S13", "S14"):
        for _ in range(10):
            code, desc, unit, price, qty = rng.choice(NON_CONTRACT_ITEMS[sid])
            new_po(rand_date(PERIOD_START, PERIOD_END), sid, "Professional Services",
                   code, desc, rng.randint(*qty), price, None)

    pos.sort(key=lambda p: (p["po_date"], p["po_id"]))

    # --- Plant: price above contract
    candidates = [p for p in pos if p["contract_id"] in ("C01", "C03", "C05", "C08", "C09")]
    for p in rng.sample(candidates, 7):
        contract_price = p["unit_price"]
        p["unit_price"] = round(contract_price * (1 + rng.choice([0.04, 0.06, 0.08, 0.10, 0.12])), 2)
        p["amount"] = round(p["quantity"] * p["unit_price"], 2)
        impact = round((p["unit_price"] - contract_price) * p["quantity"], 2)
        issues.append(issue("price_above_contract", p["supplier_id"], p["contract_id"], [p["po_id"]], impact,
                            f"{p['item_code']} billed at ${p['unit_price']:.2f} vs contract ${contract_price:.2f} "
                            f"x {p['quantity']}"))

    # --- Plant: off-contract (maverick) spend
    for sid, cat, covering in (("S11", "MRO", "C03"), ("S11", "MRO", "C03"),
                               ("S12", "Logistics & Freight", "C05"), ("S12", "Logistics & Freight", "C05"),
                               ("S15", "IT & Software", "C09")):
        code, desc, unit, price, qty = rng.choice(NON_CONTRACT_ITEMS[sid])
        p = new_po(rand_date(PERIOD_START, PERIOD_END), sid, cat, code, desc, rng.randint(*qty), price, None)
        issues.append(issue("off_contract", sid, covering, [p["po_id"]], p["amount"],
                            f"{desc} bought from non-contracted {SUPPLIER_NAME[sid]}; "
                            f"category covered by {covering}"))

    # --- Plant: purchases against an expired contract (C04 ended 2026-06-30)
    c04 = CONTRACT_BY_ID["C04"]
    for d in (date(2026, 7, 6), date(2026, 8, 3), date(2026, 8, 19), date(2026, 9, 2)):
        it = c04.items[0] if d.day < 10 else c04.items[1]
        p = new_po(d, "S04", "Facilities", it.code, it.description, rng.randint(*it.qty), it.price, "C04")
        issues.append(issue("expired_contract", "S04", "C04", [p["po_id"]], p["amount"],
                            f"PO dated {d.isoformat()} references C04, which expired 2026-06-30"))

    # Renumber POs in date order so planted issues don't stand out by ID.
    pos.sort(key=lambda p: (p["po_date"], p["po_id"]))
    renumber = {p["po_id"]: f"PO-{10001 + n}" for n, p in enumerate(pos)}
    for p in pos:
        p["po_id"] = renumber[p["po_id"]]
    for i in issues:
        i["record_ids"] = ";".join(renumber[r] for r in i["record_ids"].split(";"))
    planted_po_ids = {rid for i in issues for rid in i["record_ids"].split(";")}

    # --- Invoices: one per PO, 10-35 days after the PO (if inside the period)
    invoices: list[dict] = []
    inv_seq = iter(range(500001, 999999))
    for p in pos:
        inv_date = date.fromisoformat(p["po_date"]) + timedelta(days=rng.randint(10, 35))
        if inv_date > PERIOD_END:
            continue
        invoices.append({
            "invoice_id": f"INV-{next(inv_seq)}",
            "po_id": p["po_id"],
            "supplier_id": p["supplier_id"],
            "invoice_date": inv_date.isoformat(),
            "quantity": p["quantity"],
            "unit_price": p["unit_price"],
            "amount": p["amount"],
        })

    clean_invoices = [i for i in invoices if i["po_id"] not in planted_po_ids]

    # --- Plant: invoice does not match PO (price or quantity overbilled)
    mismatch = rng.sample(clean_invoices, 5)
    for k, inv in enumerate(mismatch):
        po_amount = inv["amount"]
        if k % 2 == 0:
            inv["unit_price"] = round(inv["unit_price"] * 1.05, 2)
            what = "unit price 5% above PO"
        else:
            inv["quantity"] = inv["quantity"] + max(1, round(inv["quantity"] * 0.1))
            what = "quantity above PO"
        inv["amount"] = round(inv["quantity"] * inv["unit_price"], 2)
        issues.append(issue("invoice_po_mismatch", inv["supplier_id"], "", [inv["invoice_id"]],
                            round(inv["amount"] - po_amount, 2), f"{inv['invoice_id']} vs {inv['po_id']}: {what}"))

    # --- Plant: duplicate invoices (same PO and amount, new number, a few days later)
    remaining = [i for i in clean_invoices if i not in mismatch]
    for orig in rng.sample(remaining, 3):
        dup = dict(orig)
        dup["invoice_id"] = f"INV-{next(inv_seq)}"
        dup["invoice_date"] = (date.fromisoformat(orig["invoice_date"]) + timedelta(days=rng.randint(1, 4))).isoformat()
        invoices.append(dup)
        issues.append(issue("duplicate_invoice", orig["supplier_id"], "", [dup["invoice_id"], orig["invoice_id"]],
                            dup["amount"], f"{dup['invoice_id']} duplicates {orig['invoice_id']} for {orig['po_id']}"))

    invoices.sort(key=lambda i: (i["invoice_date"], i["invoice_id"]))

    # --- Rebates: credit every earned quarterly rebate, except the planted misses
    credits: list[dict] = []
    cr_seq = iter(range(1, 999))
    earned = []
    for c in CONTRACTS:
        if not c.rebate:
            continue
        for q, (a, b) in QUARTERS.items():
            spend = round(sum(p["amount"] for p in pos if p["contract_id"] == c.contract_id
                              and a <= date.fromisoformat(p["po_date"]) <= min(b, c.end)), 2)
            if spend > c.rebate["quarterly_threshold"]:
                earned.append((c, q, spend))
    # The audit is as of PERIOD_END (Sep 30). Credits are due 30 days after quarter end, so Q3
    # rebates are not due yet: they get no credit and are NOT issues (a trap for the agent).
    # Planted misses: every overdue (Q1/Q2) earned quarter for C02. C01's overdue rebates are credited.
    misses = {("C02", q) for c, q, _ in earned if c.contract_id == "C02" and q != "2026Q3"}
    for c, q, spend in earned:
        rebate = round(spend * c.rebate["pct"] / 100, 2)
        if q == "2026Q3":
            continue
        if (c.contract_id, q) in misses:
            issues.append(issue("missed_rebate", c.supplier_id, c.contract_id, [f"{c.contract_id}:{q}"], rebate,
                                f"{q} spend ${spend:,.2f} passed the ${c.rebate['quarterly_threshold']:,} threshold; "
                                f"{c.rebate['pct']}% rebate not credited"))
            continue
        credit_date = QUARTERS[q][1] + timedelta(days=rng.randint(12, 28))
        credits.append({
            "credit_id": f"CR-{next(cr_seq):03d}",
            "supplier_id": c.supplier_id,
            "contract_id": c.contract_id,
            "credit_date": credit_date.isoformat(),
            "amount": rebate,
            "reason": f"Volume rebate {q}",
        })
    # A non-rebate credit as noise
    credits.append({"credit_id": f"CR-{next(cr_seq):03d}", "supplier_id": "S03", "contract_id": "C03",
                    "credit_date": "2026-05-14", "amount": 212.40, "reason": "Damaged goods return"})

    for n, i in enumerate(issues, 1):
        i["issue_id"] = f"GT-{n:03d}"
    return pos, invoices, credits, issues, earned


def issue(issue_type, supplier_id, contract_id, record_ids, impact, description):
    return {"issue_type": issue_type, "supplier_id": supplier_id, "contract_id": contract_id,
            "record_ids": ";".join(record_ids), "impact_usd": round(impact, 2), "description": description}


# ---------------------------------------------------------------- contract documents

def contract_markdown(c: Contract) -> str:
    name = SUPPLIER_NAME[c.supplier_id]
    lines = [
        f"# Supply Agreement {c.contract_id}",
        "",
        f"**Between:** Granite Ridge Manufacturing, Inc. (\"Buyer\") and {name} (\"Supplier\")",
        f"**Category:** {c.category}",
        "",
        "## 1. Term",
        f"This Agreement takes effect on {c.start.strftime('%B %-d, %Y')} and remains in force until "
        f"{c.end.strftime('%B %-d, %Y')} (the \"Expiration Date\"), unless terminated earlier under Section 7.",
    ]
    for clause in c.extra_clauses:
        lines.append(clause)
    lines += ["", "## 2. Pricing"]
    if c.style == "table":
        lines += ["Supplier will invoice the following firm prices for the full term. Prices exclude sales tax.", "",
                  "| Item code | Description | Unit | Unit price (USD) |", "|---|---|---|---|"]
        for it in c.items:
            lines.append(f"| {it.code} | {it.description} | {it.unit} | {it.price:,.2f} |")
    else:
        lines.append("Prices are fixed for the full term and exclude sales tax. Supplier agrees to the following:")
        for it in c.items:
            lines.append(f"- Item {it.code} ({it.description.lower()}) will be supplied at "
                         f"${it.price:,.2f} per {it.unit}.")
    lines += ["", "Any price change requires a written amendment signed by Buyer's Procurement Director. "
                  "Invoices above the contracted unit price will be short-paid to the contracted price.",
              "", "## 3. Payment Terms",
              f"Payment is due net {c.payment_terms_days} days from receipt of a correct invoice. "
              "Each invoice must reference a valid Buyer purchase order number and must not exceed the "
              "PO quantity or price."]
    lines += ["", "## 4. Volume Rebate"]
    if c.rebate:
        lines.append(
            f"For each calendar quarter in which Buyer's purchases under this Agreement exceed "
            f"${c.rebate['quarterly_threshold']:,.0f}, Supplier shall credit Buyer a rebate equal to "
            f"{c.rebate['pct']:g}% of that quarter's total purchases. Supplier shall issue the credit "
            f"within 30 days after the end of the quarter.")
    else:
        lines.append("No volume rebate applies to this Agreement.")
    lines += ["", "## 5. Delivery and Acceptance",
              "Goods are delivered FOB destination. Buyer may reject non-conforming goods within 10 business days.",
              "", "## 6. Exclusivity",
              f"Buyer will source its {c.category} requirements for the items listed in Section 2 from Supplier "
              "for the term, except in an emergency approved in writing by Procurement.",
              "", "## 7. Termination",
              "Either party may terminate for material breach on 30 days' written notice if the breach is not cured.",
              ""]
    return "\n".join(lines)


def contract_truth(c: Contract) -> dict:
    return {
        "contract_id": c.contract_id,
        "supplier_name": SUPPLIER_NAME[c.supplier_id],
        "start_date": c.start.isoformat(),
        "end_date": c.end.isoformat(),
        "payment_terms_days": c.payment_terms_days,
        "items": [{"item_code": i.code, "unit": i.unit, "unit_price": i.price} for i in c.items],
        "rebate": ({"percent": c.rebate["pct"], "quarterly_threshold_usd": c.rebate["quarterly_threshold"]}
                   if c.rebate else None),
    }


# ---------------------------------------------------------------- write out

def write_csv(path: Path, rows: list[dict]):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


SCHEMA = """
CREATE TABLE suppliers (supplier_id TEXT PRIMARY KEY, name TEXT, category TEXT, country TEXT);
CREATE TABLE contracts (contract_id TEXT PRIMARY KEY, supplier_id TEXT, category TEXT,
                        start_date TEXT, end_date TEXT);
CREATE TABLE purchase_orders (po_id TEXT PRIMARY KEY, po_date TEXT, supplier_id TEXT, category TEXT,
                              item_code TEXT, description TEXT, quantity INTEGER, unit_price REAL,
                              amount REAL, contract_id TEXT, requester TEXT, business_unit TEXT);
CREATE TABLE invoices (invoice_id TEXT PRIMARY KEY, po_id TEXT, supplier_id TEXT, invoice_date TEXT,
                       quantity INTEGER, unit_price REAL, amount REAL);
CREATE TABLE credits (credit_id TEXT PRIMARY KEY, supplier_id TEXT, contract_id TEXT, credit_date TEXT,
                      amount REAL, reason TEXT);
"""


def build_db(tables: dict[str, list[dict]]):
    db_path = DATA / "procurement.db"
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    for name, rows in tables.items():
        cols = list(rows[0].keys())
        con.executemany(f"INSERT INTO {name} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [[None if r[c] == "" else r[c] for c in cols] for r in rows])
    con.commit()
    con.close()


def main():
    pos, invoices, credits, issues, earned = generate()
    suppliers = [{"supplier_id": s[0], "name": s[1], "category": s[2], "country": s[3]} for s in SUPPLIERS]
    contracts = [{"contract_id": c.contract_id, "supplier_id": c.supplier_id, "category": c.category,
                  "start_date": c.start.isoformat(), "end_date": c.end.isoformat()} for c in CONTRACTS]

    tables = {"suppliers": suppliers, "contracts": contracts, "purchase_orders": pos,
              "invoices": invoices, "credits": credits}
    for name, rows in tables.items():
        write_csv(DATA / f"{name}.csv", rows)
    build_db(tables)

    (DATA / "contracts").mkdir(exist_ok=True)
    for c in CONTRACTS:
        (DATA / "contracts" / f"{c.contract_id}.md").write_text(contract_markdown(c))

    (DATA / "answers").mkdir(exist_ok=True)
    issues_out = [{k: i[k] for k in ("issue_id", "issue_type", "supplier_id", "contract_id",
                                     "record_ids", "impact_usd", "description")} for i in issues]
    write_csv(DATA / "answers" / "ground_truth.csv", issues_out)
    (DATA / "answers" / "contract_terms_truth.json").write_text(
        json.dumps([contract_truth(c) for c in CONTRACTS], indent=2))

    print(f"POs: {len(pos)}  invoices: {len(invoices)}  credits: {len(credits)}  planted issues: {len(issues)}")
    by_type: dict[str, list] = {}
    for i in issues:
        by_type.setdefault(i["issue_type"], []).append(i["impact_usd"])
    for t, v in by_type.items():
        print(f"  {t:22s} {len(v):2d} issues  ${sum(v):>12,.2f}")
    print(f"  {'TOTAL':22s} {len(issues):2d} issues  ${sum(i['impact_usd'] for i in issues):>12,.2f}")
    print("Earned rebate quarters:", [(c.contract_id, q, s) for c, q, s in earned])


if __name__ == "__main__":
    main()
