"""Deterministic verification of the agent's findings.

The model proposes; code checks. Every finding is re-computed from the database and the
extracted contract terms. The result says whether the cited records really show the issue
and what the dollar impact actually is, so a hallucinated ID or a wrong amount never
reaches the buyer unflagged.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import config
from .db import fetch_all

ISSUE_TYPES = [
    "price_above_contract",
    "off_contract",
    "expired_contract",
    "invoice_po_mismatch",
    "duplicate_invoice",
    "missed_rebate",
]

QUARTERS = {
    "Q1": ("01-01", "03-31"),
    "Q2": ("04-01", "06-30"),
    "Q3": ("07-01", "09-30"),
    "Q4": ("10-01", "12-31"),
}


def load_terms() -> dict[str, dict] | None:
    if not config.TERMS_PATH.exists():
        return None
    return {t["contract_id"]: t for t in json.loads(config.TERMS_PATH.read_text())}


def _result(status: str, computed: float | None, notes: list[str]) -> dict:
    return {"status": status, "computed_amount_usd": None if computed is None else round(computed, 2),
            "notes": notes}


def _pos(ids):
    rows = fetch_all(f"SELECT * FROM purchase_orders WHERE po_id IN ({','.join('?' * len(ids))})", tuple(ids))
    return {r["po_id"]: r for r in rows}


def _invoices(ids):
    rows = fetch_all(f"SELECT * FROM invoices WHERE invoice_id IN ({','.join('?' * len(ids))})", tuple(ids))
    return {r["invoice_id"]: r for r in rows}


def verify_finding(finding: dict, terms: dict[str, dict] | None = None) -> dict:
    """Return {status, computed_amount_usd, notes}.

    status is one of:
      verified         the records show the issue and the claimed amount matches (within 1% or $1)
      amount_mismatch  the issue is real but the claimed amount is wrong (use computed_amount_usd)
      not_supported    the records don't show this issue, or cited IDs don't exist
      unverifiable     the check needs data that isn't available (e.g. terms not extracted yet)
    """
    terms = terms if terms is not None else load_terms()
    issue_type = finding.get("issue_type")
    ids = [str(i).strip() for i in finding.get("record_ids", []) if str(i).strip()]
    if issue_type not in ISSUE_TYPES:
        return _result("not_supported", None, [f"Unknown issue type {issue_type!r}."])
    if not ids:
        return _result("not_supported", None, ["No record IDs cited."])

    checker = {
        "price_above_contract": _check_price,
        "off_contract": _check_off_contract,
        "expired_contract": _check_expired,
        "invoice_po_mismatch": _check_invoice_mismatch,
        "duplicate_invoice": _check_duplicate,
        "missed_rebate": _check_rebate,
    }[issue_type]
    status, computed, notes = checker(ids, terms)
    if status == "verified":
        claimed = float(finding.get("amount_at_risk_usd") or 0)
        if abs(claimed - computed) > max(1.0, 0.01 * abs(computed)):
            status = "amount_mismatch"
            notes.append(f"Claimed ${claimed:,.2f} but records show ${computed:,.2f}.")
    return _result(status, computed, notes)


# ---------------------------------------------------------------- per-type checks
# Each returns (status, computed_amount, notes). They return "verified" when the issue holds;
# verify_finding() then compares amounts.

def _missing(ids, found):
    return [i for i in ids if i not in found]


def _check_price(ids, terms):
    if terms is None:
        return "unverifiable", None, ["Contract terms not extracted yet."]
    pos = _pos(ids)
    if missing := _missing(ids, pos):
        return "not_supported", None, [f"Unknown PO IDs: {', '.join(missing)}."]
    total, notes = 0.0, []
    for po_id, p in pos.items():
        t = terms.get(p["contract_id"] or "")
        if not t:
            return "not_supported", None, [f"{po_id} does not reference a contract with known terms."]
        price = {i["item_code"]: i["unit_price"] for i in t["items"]}.get(p["item_code"])
        if price is None:
            return "not_supported", None, [f"{po_id}: item {p['item_code']} is not priced in {p['contract_id']}."]
        if p["unit_price"] <= price + 0.005:
            return "not_supported", None, [f"{po_id} unit price ${p['unit_price']} is not above contract ${price}."]
        over = (p["unit_price"] - price) * p["quantity"]
        total += over
        notes.append(f"{po_id}: ${p['unit_price']:.2f} vs ${price:.2f} x {p['quantity']} = ${over:,.2f} over.")
    return "verified", total, notes


def _check_off_contract(ids, terms):
    pos = _pos(ids)
    if missing := _missing(ids, pos):
        return "not_supported", None, [f"Unknown PO IDs: {', '.join(missing)}."]
    total, notes = 0.0, []
    for po_id, p in pos.items():
        if p["contract_id"]:
            return "not_supported", None, [f"{po_id} references contract {p['contract_id']}."]
        own = fetch_all("SELECT contract_id FROM contracts WHERE supplier_id = ? AND start_date <= ? "
                        "AND end_date >= ?", (p["supplier_id"], p["po_date"], p["po_date"]))
        if own:
            return "not_supported", None, [f"{po_id}: supplier {p['supplier_id']} has an active contract."]
        covering = fetch_all("SELECT contract_id FROM contracts WHERE category = ? AND start_date <= ? "
                             "AND end_date >= ?", (p["category"], p["po_date"], p["po_date"]))
        if not covering:
            return "not_supported", None, [f"{po_id}: no active contract covers {p['category']} on {p['po_date']}."]
        total += p["amount"]
        notes.append(f"{po_id}: ${p['amount']:,.2f} with non-contracted {p['supplier_id']}; "
                     f"covered by {', '.join(c['contract_id'] for c in covering)}.")
    return "verified", total, notes


def _check_expired(ids, terms):
    pos = _pos(ids)
    if missing := _missing(ids, pos):
        return "not_supported", None, [f"Unknown PO IDs: {', '.join(missing)}."]
    total, notes = 0.0, []
    for po_id, p in pos.items():
        c = fetch_all("SELECT * FROM contracts WHERE contract_id = ?", (p["contract_id"] or "",))
        if not c:
            return "not_supported", None, [f"{po_id} does not reference a contract."]
        c = c[0]
        if c["start_date"] <= p["po_date"] <= c["end_date"]:
            return "not_supported", None, [f"{po_id} ({p['po_date']}) is inside {c['contract_id']}'s term."]
        total += p["amount"]
        notes.append(f"{po_id} dated {p['po_date']}; {c['contract_id']} ran {c['start_date']} to {c['end_date']}.")
    return "verified", total, notes


def _check_invoice_mismatch(ids, terms):
    inv_ids = [i for i in ids if i.startswith("INV-")]
    if not inv_ids:
        return "not_supported", None, ["Cite the invoice ID(s) for an invoice-PO mismatch."]
    invs = _invoices(inv_ids)
    if missing := _missing(inv_ids, invs):
        return "not_supported", None, [f"Unknown invoice IDs: {', '.join(missing)}."]
    total, notes = 0.0, []
    for inv_id, inv in invs.items():
        po = _pos([inv["po_id"]]).get(inv["po_id"])
        if not po:
            return "not_supported", None, [f"{inv_id} references unknown PO {inv['po_id']}."]
        if inv["quantity"] == po["quantity"] and abs(inv["unit_price"] - po["unit_price"]) < 0.005:
            return "not_supported", None, [f"{inv_id} matches {po['po_id']}."]
        diff = inv["amount"] - po["amount"]
        total += diff
        notes.append(f"{inv_id}: {inv['quantity']} x ${inv['unit_price']:.2f} vs {po['po_id']}: "
                     f"{po['quantity']} x ${po['unit_price']:.2f} (difference ${diff:,.2f}).")
    return "verified", total, notes


def _check_duplicate(ids, terms):
    invs = _invoices(ids)
    if missing := _missing(ids, invs):
        return "not_supported", None, [f"Unknown invoice IDs: {', '.join(missing)}."]
    if len(invs) < 2:
        # Accept a single cited duplicate if another invoice for the same PO and amount exists.
        inv = next(iter(invs.values()))
        others = fetch_all("SELECT * FROM invoices WHERE po_id = ? AND amount = ? AND invoice_id <> ?",
                           (inv["po_id"], inv["amount"], inv["invoice_id"]))
        if not others:
            return "not_supported", None, [f"No other invoice matches {inv['invoice_id']}."]
        return "verified", inv["amount"], [f"{inv['invoice_id']} duplicates {others[0]['invoice_id']}."]
    keys = {(i["po_id"], i["amount"], i["supplier_id"]) for i in invs.values()}
    if len(keys) != 1:
        return "not_supported", None, ["Cited invoices differ in PO, amount or supplier."]
    amount = next(iter(invs.values()))["amount"]
    return "verified", amount * (len(invs) - 1), [f"{len(invs)} invoices for the same PO and amount ${amount:,.2f}."]


def _check_rebate(ids, terms):
    if terms is None:
        return "unverifiable", None, ["Contract terms not extracted yet."]
    total, notes = 0.0, []
    as_of = date.fromisoformat(config.AS_OF_DATE)
    for key in ids:
        m = re.fullmatch(r"(C\d{2}):(\d{4})(Q[1-4])", key)
        if not m:
            return "not_supported", None, [f"Rebate record IDs must look like 'C02:2026Q1', got {key!r}."]
        cid, year, q = m.groups()
        t = terms.get(cid)
        if not t or not t.get("rebate"):
            return "not_supported", None, [f"{cid} has no volume rebate."]
        start, end = f"{year}-{QUARTERS[q][0]}", f"{year}-{QUARTERS[q][1]}"
        if date.fromisoformat(end) + timedelta(days=30) > as_of:
            return "not_supported", None, [f"{key}: rebate not due until 30 days after {end}."]
        spend = fetch_all("SELECT COALESCE(SUM(amount), 0) AS s FROM purchase_orders WHERE contract_id = ? "
                          "AND po_date BETWEEN ? AND ? AND po_date BETWEEN ? AND ?",
                          (cid, start, end, t["start_date"], t["end_date"]))[0]["s"]
        threshold = t["rebate"]["quarterly_threshold_usd"]
        if spend <= threshold:
            return "not_supported", None, [f"{key}: spend ${spend:,.2f} did not exceed ${threshold:,.0f}."]
        rebate = spend * t["rebate"]["percent"] / 100
        credited = fetch_all("SELECT COALESCE(SUM(amount), 0) AS s FROM credits WHERE contract_id = ? "
                             "AND reason LIKE ?", (cid, f"%rebate%{year}{q}%"))[0]["s"]
        if credited >= rebate - 1:
            return "not_supported", None, [f"{key}: rebate of ${rebate:,.2f} was credited."]
        total += rebate - credited
        notes.append(f"{key}: spend ${spend:,.2f} > ${threshold:,.0f}; owed ${rebate:,.2f}, credited ${credited:,.2f}.")
    return "verified", total, notes
