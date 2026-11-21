"""Extract structured terms from each contract document with Claude (structured outputs).

Contracts are written in different styles (tables, prose), so this is a real extraction task.
The output, data/contracts/terms.json, feeds the get_contract_terms tool and the verifier.
Running with --eval scores the extraction field by field against the answer key.

Run: python -m agent.extract_terms [--eval-only]
"""

from __future__ import annotations

import argparse
import json
import time

import anthropic
from pydantic import BaseModel, Field

from . import config


class PricedItem(BaseModel):
    item_code: str
    description: str
    unit: str = Field(description="Unit of measure, e.g. ton, box, load, seat")
    unit_price: float = Field(description="Contracted price per unit in USD")


class Rebate(BaseModel):
    percent: float = Field(description="Rebate percentage, e.g. 2.0 for 2%")
    quarterly_threshold_usd: float = Field(description="Quarterly purchase amount that must be exceeded")
    credit_due_days_after_quarter: int


class ContractTerms(BaseModel):
    contract_id: str
    supplier_name: str
    category: str
    start_date: str = Field(description="YYYY-MM-DD")
    end_date: str = Field(description="YYYY-MM-DD")
    auto_renews: bool
    payment_terms_days: int
    items: list[PricedItem]
    rebate: Rebate | None = Field(description="null if the contract has no volume rebate")
    notable_clauses: list[str] = Field(description="Short notes on clauses a compliance auditor should know")


SYSTEM = ("You extract contract terms for a procurement compliance system. Copy numbers exactly as written. "
          "Do not infer terms that the document does not state.")


def extract_one(client: anthropic.Anthropic, text: str) -> tuple[ContractTerms, dict]:
    t0 = time.monotonic()
    response = client.beta.messages.parse(
        model=config.EXTRACTION_MODEL,
        max_tokens=8000,
        system=SYSTEM,
        output_config={"effort": "low"},  # simple, well-specified task
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": f"Extract the terms of this contract.\n\n{text}"}],
        output_format=ContractTerms,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Extraction refused: {response.stop_details}")
    meta = {"model": response.model, "latency_s": round(time.monotonic() - t0, 2),
            "cost_usd": round(config.cost_usd(config.EXTRACTION_MODEL, response.usage), 5)}
    return response.parsed_output, meta


def run_extraction() -> list[dict]:
    client = anthropic.Anthropic()
    out, total_cost = [], 0.0
    for path in sorted(config.CONTRACTS_DIR.glob("C*.md")):
        terms, meta = extract_one(client, path.read_text())
        total_cost += meta["cost_usd"]
        print(f"  {path.stem}: {len(terms.items)} items, rebate={'yes' if terms.rebate else 'no'}  "
              f"({meta['latency_s']}s, ${meta['cost_usd']:.4f})")
        out.append(terms.model_dump())
    config.TERMS_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {config.TERMS_PATH.relative_to(config.ROOT)}  total ${total_cost:.4f}")
    return out


def evaluate(extracted: list[dict]) -> dict:
    """Field-by-field accuracy against the answer key."""
    truth = {t["contract_id"]: t for t in json.loads((config.ANSWERS_DIR / "contract_terms_truth.json").read_text())}
    got = {t["contract_id"]: t for t in extracted}
    checks, errors = 0, []

    def check(cid, field, expected, actual):
        nonlocal checks
        checks += 1
        if isinstance(expected, float) or isinstance(actual, float):
            ok = actual is not None and abs(float(expected) - float(actual)) < 0.005
        else:
            ok = expected == actual
        if not ok:
            errors.append(f"{cid}.{field}: expected {expected!r}, got {actual!r}")

    for cid, t in truth.items():
        g = got.get(cid)
        if not g:
            errors.append(f"{cid}: missing")
            continue
        for f in ("start_date", "end_date", "payment_terms_days"):
            check(cid, f, t[f], g.get(f))
        gi = {i["item_code"]: i for i in g["items"]}
        for item in t["items"]:
            x = gi.get(item["item_code"], {})
            check(cid, f"{item['item_code']}.unit_price", item["unit_price"], x.get("unit_price"))
            check(cid, f"{item['item_code']}.unit", item["unit"], x.get("unit"))
        check(cid, "item_count", len(t["items"]), len(g["items"]))
        tr, gr = t["rebate"], g.get("rebate")
        check(cid, "has_rebate", tr is not None, gr is not None)
        if tr and gr:
            check(cid, "rebate.percent", tr["percent"], gr["percent"])
            check(cid, "rebate.threshold", float(tr["quarterly_threshold_usd"]), gr["quarterly_threshold_usd"])
    accuracy = (checks - len(errors)) / checks if checks else 0
    return {"fields_checked": checks, "errors": errors, "accuracy": round(accuracy, 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-only", action="store_true", help="Score the existing terms.json without calling the API")
    args = ap.parse_args()
    extracted = json.loads(config.TERMS_PATH.read_text()) if args.eval_only else run_extraction()
    result = evaluate(extracted)
    print(f"Extraction accuracy: {result['accuracy']:.1%} of {result['fields_checked']} fields")
    for e in result["errors"]:
        print("  ERROR", e)
    config.RESULTS_DIR.mkdir(exist_ok=True)
    (config.RESULTS_DIR / "extraction_eval.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
