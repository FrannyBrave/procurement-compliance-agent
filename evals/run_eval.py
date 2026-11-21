"""Evaluate the agent against the answer key.

Audit eval: run the full audit N times and match findings to data/answers/ground_truth.csv
            (same issue type and overlapping record IDs). Reports precision, recall and F1 per
            issue type, dollars found vs planted, cost, tool calls and time.
Q&A eval:   questions with exact answers computed from the database, graded automatically by
            checking the numbers and IDs in the agent's answer.

Usage:
    python evals/run_eval.py --audit-runs 3          # live audit runs (about $0.50 each)
    python evals/run_eval.py --qa                    # live Q&A questions (about $0.10 each)
    python evals/run_eval.py --score results/demo_audit.json   # score a saved run, no API calls
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from agent.db import fetch_all  # noqa: E402

ISSUE_TYPES = ["price_above_contract", "off_contract", "expired_contract",
               "invoice_po_mismatch", "duplicate_invoice", "missed_rebate"]


def load_truth() -> list[dict]:
    with (config.ANSWERS_DIR / "ground_truth.csv").open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["ids"] = set(r["record_ids"].split(";"))
        r["impact_usd"] = float(r["impact_usd"])
    return rows


# ---------------------------------------------------------------- audit scoring

def score_audit(run: dict) -> dict:
    truth = load_truth()
    findings = run["findings"]
    matched_truth: set[str] = set()
    tp_findings, fp_findings = [], []
    for f in findings:
        hit = next((t for t in truth if t["issue_type"] == f["issue_type"] and t["ids"] & set(f["record_ids"])
                    and t["issue_id"] not in matched_truth), None)
        if hit:
            matched_truth.add(hit["issue_id"])
            tp_findings.append(f)
        else:
            fp_findings.append(f)
    missed = [t for t in truth if t["issue_id"] not in matched_truth]

    per_type = {}
    for t in ISSUE_TYPES:
        tp = sum(1 for f in tp_findings if f["issue_type"] == t)
        fp = sum(1 for f in fp_findings if f["issue_type"] == t)
        total = sum(1 for g in truth if g["issue_type"] == t)
        per_type[t] = {"planted": total, "found": tp, "false_positives": fp,
                       "precision": tp / (tp + fp) if tp + fp else 1.0, "recall": tp / total if total else 1.0}

    tp, fp, fn = len(tp_findings), len(fp_findings), len(missed)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    verified_usd = sum(f["verification"]["computed_amount_usd"] or 0 for f in tp_findings
                       if f["verification"]["status"] in ("verified", "amount_mismatch"))
    return {
        "run_id": run["run_id"], "model": run["model"], "effort": run["effort"], "status": run["status"],
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0,
        "true_positives": tp, "false_positives": fp, "missed": fn,
        "usd_found": round(verified_usd, 2), "usd_planted": round(sum(t["impact_usd"] for t in truth), 2),
        "claimed_amount_errors": sum(1 for f in findings if f["verification"]["status"] == "amount_mismatch"),
        "cost_usd": run["cost_usd"], "tool_calls": run["tool_calls"], "turns": run["turns"],
        "duration_s": run["duration_s"], "per_type": per_type,
        "missed_issues": [f"{t['issue_id']} {t['issue_type']} {t['record_ids']}" for t in missed],
        "false_positive_findings": [f"{f['issue_type']} {f['record_ids']} ({f['verification']['status']})"
                                    for f in fp_findings],
    }


# ---------------------------------------------------------------- Q&A

def qa_questions() -> list[dict]:
    """Questions whose expected answers are computed from the database, not hardcoded."""
    one = lambda q, p=(): fetch_all(q, p)[0]  # noqa: E731
    top_supplier = one("SELECT s.name, ROUND(SUM(p.amount), 2) AS t FROM purchase_orders p JOIN suppliers s "
                       "USING (supplier_id) GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    q3_c05 = one("SELECT ROUND(SUM(amount), 2) AS t FROM purchase_orders WHERE contract_id = 'C05' "
                 "AND po_date BETWEEN '2026-07-01' AND '2026-09-30'")["t"]
    ps_spend = one("SELECT ROUND(SUM(amount), 2) AS t FROM purchase_orders "
                   "WHERE category = 'Professional Services'")["t"]
    n_inv_s03 = one("SELECT COUNT(*) AS n FROM invoices WHERE supplier_id = 'S03'")["n"]
    biggest_po = one("SELECT po_id, amount FROM purchase_orders ORDER BY amount DESC LIMIT 1")
    c02_q2 = one("SELECT ROUND(SUM(amount), 2) AS t FROM purchase_orders WHERE contract_id = 'C02' "
                 "AND po_date BETWEEN '2026-04-01' AND '2026-06-30'")["t"]
    return [
        {"q": "Which supplier has the highest total PO spend this year, and how much?",
         "expect": [top_supplier["name"], top_supplier["t"]]},
        {"q": "What was total PO spend under contract C05 in Q3 2026?", "expect": [q3_c05]},
        {"q": "How much have we spent on Professional Services, and is any of it under contract?",
         "expect": [ps_spend]},
        {"q": "How many invoices has Keystone MRO Supply sent us?", "expect": [n_inv_s03]},
        {"q": "Which single PO has the largest amount?", "expect": [biggest_po["po_id"], biggest_po["amount"]]},
        {"q": "What is the contracted unit price for item MRO-311?", "expect": [7.85]},
        {"q": "When does the Brightway Facility Services contract expire?", "expect": ["2026-06-30|June 30, 2026"]},
        {"q": "What are the payment terms in contract C07?", "expect": [60]},
        {"q": "What rebate percentage and quarterly threshold does contract C01 have?", "expect": [2, 150000]},
        {"q": "What were Northline Polymers' purchases under C02 in Q2 2026, and did they earn a rebate?",
         "expect": [c02_q2]},
    ]


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*\.?\d*", text)]


def grade(answer: str, expect: list) -> bool:
    nums = _numbers(answer)
    for e in expect:
        if isinstance(e, (int, float)):
            if not any(abs(n - e) <= max(0.01, abs(e) * 0.0005) for n in nums):
                return False
        elif not any(alt.lower() in answer.lower() for alt in str(e).split("|")):
            return False
    return True


def run_qa() -> dict:
    from agent.loop import run_ask
    rows = []
    for item in qa_questions():
        res = run_ask(item["q"])
        ok = grade(res.final_text, item["expect"])
        rows.append({"question": item["q"], "expected": item["expect"], "correct": ok,
                     "cost_usd": res.cost_usd, "tool_calls": res.tool_calls, "duration_s": res.duration_s,
                     "answer": res.final_text})
        print(f"  {'PASS' if ok else 'FAIL'}  ${res.cost_usd:.3f}  {item['q']}", flush=True)
    return {"accuracy": sum(r["correct"] for r in rows) / len(rows), "n": len(rows),
            "avg_cost_usd": round(statistics.mean(r["cost_usd"] for r in rows), 4),
            "avg_duration_s": round(statistics.mean(r["duration_s"] for r in rows), 1), "questions": rows}


# ---------------------------------------------------------------- reporting

def print_audit(s: dict):
    print(f"\n{s['run_id']}  ({s['model']}, effort {s['effort']}, {s['status']})")
    print(f"  precision {s['precision']:.0%}  recall {s['recall']:.0%}  F1 {s['f1']:.2f}   "
          f"${s['usd_found']:,.2f} of ${s['usd_planted']:,.2f} found")
    print(f"  ${s['cost_usd']:.2f}  {s['tool_calls']} tool calls  {s['turns']} turns  {s['duration_s']}s  "
          f"claimed-amount errors caught by verifier: {s['claimed_amount_errors']}")
    for t, v in s["per_type"].items():
        print(f"    {t:22s} {v['found']}/{v['planted']} found, {v['false_positives']} false positives")
    for m in s["missed_issues"]:
        print("    MISSED", m)
    for m in s["false_positive_findings"]:
        print("    FALSE POSITIVE", m)


def summarize(scores: list[dict]) -> dict:
    keys = ["precision", "recall", "f1", "usd_found", "cost_usd", "tool_calls", "duration_s"]
    return {k: {"mean": round(statistics.mean(s[k] for s in scores), 4),
                "min": min(s[k] for s in scores), "max": max(s[k] for s in scores)} for k in keys}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit-runs", type=int, default=0)
    ap.add_argument("--qa", action="store_true")
    ap.add_argument("--score", nargs="*", default=[], help="Saved run JSON files to score (no API calls)")
    args = ap.parse_args()

    report = {"created_at": datetime.now().isoformat(timespec="seconds"), "audit": [], "qa": None}
    for path in args.score:
        s = score_audit(json.loads(Path(path).read_text()))
        report["audit"].append(s)
        print_audit(s)
    if args.audit_runs:
        from agent.loop import run_audit
        for i in range(args.audit_runs):
            print(f"\nAudit run {i + 1}/{args.audit_runs} ...", flush=True)
            s = score_audit(run_audit().to_dict())
            report["audit"].append(s)
            print_audit(s)
    if report["audit"]:
        report["audit_summary"] = summarize(report["audit"])
    if args.qa:
        print("\nQ&A eval")
        report["qa"] = run_qa()
        print(f"  accuracy {report['qa']['accuracy']:.0%} of {report['qa']['n']}, "
              f"avg ${report['qa']['avg_cost_usd']:.3f}, {report['qa']['avg_duration_s']}s")

    if args.audit_runs or args.qa:
        config.RESULTS_DIR.mkdir(exist_ok=True)
        out = config.RESULTS_DIR / f"eval_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nSaved {out.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
