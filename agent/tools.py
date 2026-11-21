"""The agent's tools: JSON schemas the model sees, and the Python handlers that run them.

Design rules:
- Every tool is read-only except record_finding and draft_supplier_email, which only write
  to this run's in-memory state. Nothing is ever sent outside the app.
- Schemas use strict mode, so the model's inputs always match the schema.
- Handlers still validate inputs and raise ToolError with a message the model can act on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import config, db
from .verify import ISSUE_TYPES, load_terms, verify_finding


class ToolError(Exception):
    """Returned to the model as an is_error tool result."""


@dataclass
class RunState:
    """What one agent run has produced so far."""
    findings: dict[str, dict] = field(default_factory=dict)
    drafts: list[dict] = field(default_factory=list)
    _seq: int = 0

    def next_id(self) -> str:
        self._seq += 1
        return f"F-{self._seq:03d}"


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


TOOLS = [
    {
        "name": "describe_schema",
        "description": "List the database tables, their columns, row counts and two sample rows each. "
                       "Call this first before writing SQL.",
        "input_schema": _obj({}),
    },
    {
        "name": "run_sql",
        "description": (
            "Run one read-only SQLite SELECT (or WITH ... SELECT) query against the procurement database. "
            f"Returns at most {config.SQL_ROW_LIMIT} rows; aggregate in SQL rather than pulling raw rows. "
            "Dates are ISO strings (YYYY-MM-DD). Writes and PRAGMAs are rejected."),
        "input_schema": _obj({
            "query": {"type": "string", "description": "A single SQLite SELECT statement."},
            "purpose": {"type": "string", "description": "One short sentence: what this query checks."},
        }),
    },
    {
        "name": "list_contracts",
        "description": "List all supplier contracts: contract ID, supplier, category, start and end dates.",
        "input_schema": _obj({}),
    },
    {
        "name": "get_contract",
        "description": "Read the full text of one contract document (pricing, payment terms, rebates, exclusivity).",
        "input_schema": _obj({"contract_id": {"type": "string", "description": "e.g. C03"}}),
    },
    {
        "name": "get_contract_terms",
        "description": ("Get the structured terms of one contract, extracted from its document: item codes "
                        "with contracted unit prices, term dates, payment terms, and volume rebate rules. "
                        "Cheaper than reading the full contract."),
        "input_schema": _obj({"contract_id": {"type": "string", "description": "e.g. C03"}}),
    },
    {
        "name": "record_finding",
        "description": (
            "Record one compliance finding with its evidence. Record each distinct issue separately "
            "(one PO, invoice, or rebate quarter per finding, except duplicate invoices: cite every "
            "invoice in the duplicate set). The finding is checked in code against the data right away; "
            "the result tells you whether it was verified and the computed dollar amount. If it comes "
            "back not_supported or amount_mismatch, investigate and record it again with the same "
            "record_ids to replace it.\n"
            "record_ids formats: PO IDs (PO-10123) for price_above_contract, off_contract and "
            "expired_contract; invoice IDs (INV-500123) for invoice_po_mismatch and duplicate_invoice; "
            "contract:quarter keys (C02:2026Q1) for missed_rebate.\n"
            "amount_at_risk_usd: price_above_contract = (billed - contract price) x quantity; "
            "off_contract and expired_contract = the PO amount; invoice_po_mismatch = invoice amount "
            "minus PO amount; duplicate_invoice = the duplicated amount; missed_rebate = rebate owed "
            "minus any rebate credited."),
        "input_schema": _obj({
            "issue_type": {"type": "string", "enum": ISSUE_TYPES},
            "supplier_id": {"type": "string"},
            "contract_id": {"type": "string", "description": "Related contract ID, or empty string if none."},
            "record_ids": {"type": "array", "items": {"type": "string"}},
            "amount_at_risk_usd": {"type": "number"},
            "evidence": {"type": "string", "description": "What the data shows, with the numbers."},
            "contract_clause": {"type": "string",
                                "description": "The contract section that is breached, or empty string."},
            "confidence": {"type": "number", "description": "0 to 1."},
        }),
    },
    {
        "name": "draft_supplier_email",
        "description": ("Save a draft email to a supplier about one recorded finding, for a buyer to review. "
                        "This only saves a draft; nothing is sent. Only use it when the user asks for a draft."),
        "input_schema": _obj({
            "finding_id": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }),
    },
]
for _t in TOOLS:
    _t["strict"] = True

TOOL_NAMES = [t["name"] for t in TOOLS]
_CONTRACT_ID = re.compile(r"^C\d{2}$")


# ---------------------------------------------------------------- handlers

def describe_schema(state: RunState, **_) -> dict:
    tables = [r["name"] for r in db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    out = {}
    for t in tables:
        cols = db.fetch_all(f"SELECT name, type FROM pragma_table_info('{t}')")
        out[t] = {
            "columns": {c["name"]: c["type"] for c in cols},
            "row_count": db.fetch_all(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"],
            "sample_rows": db.fetch_all(f"SELECT * FROM {t} LIMIT 2"),
        }
    return {"as_of_date": config.AS_OF_DATE, "tables": out}


def run_sql(state: RunState, query: str, purpose: str = "") -> dict:
    try:
        res = db.run_query(query)
    except db.SQLRejected as e:
        raise ToolError(f"Query rejected: {e}") from e
    except Exception as e:  # sqlite errors: bad column names, syntax
        raise ToolError(f"SQL error: {e}") from e
    # Compact form: column list plus rows as lists, which uses far fewer tokens than dicts.
    out = {"columns": res["columns"], "rows": [list(r.values()) for r in res["rows"]],
           "row_count": res["row_count"]}
    if res["truncated"]:
        out["warning"] = f"Truncated to {config.SQL_ROW_LIMIT} rows. Aggregate or filter in SQL."
    return out


def list_contracts(state: RunState, **_) -> dict:
    return {"as_of_date": config.AS_OF_DATE, "contracts": db.fetch_all(
        "SELECT c.contract_id, c.supplier_id, s.name AS supplier_name, c.category, c.start_date, c.end_date "
        "FROM contracts c JOIN suppliers s USING (supplier_id) ORDER BY c.contract_id")}


def _contract_id(contract_id: str) -> str:
    cid = contract_id.strip().upper()
    if not _CONTRACT_ID.match(cid):
        raise ToolError(f"Invalid contract ID {contract_id!r}. Use list_contracts to see valid IDs.")
    return cid


def get_contract(state: RunState, contract_id: str) -> dict:
    cid = _contract_id(contract_id)
    path = config.CONTRACTS_DIR / f"{cid}.md"
    if not path.exists():
        raise ToolError(f"No contract {cid}. Use list_contracts to see valid IDs.")
    return {"contract_id": cid, "text": path.read_text()}


def get_contract_terms(state: RunState, contract_id: str) -> dict:
    cid = _contract_id(contract_id)
    terms = load_terms()
    if terms is None:
        raise ToolError("Structured terms are not available. Use get_contract to read the document.")
    if cid not in terms:
        raise ToolError(f"No terms for {cid}. Use list_contracts to see valid IDs.")
    return terms[cid]


def record_finding(state: RunState, issue_type: str, supplier_id: str, contract_id: str,
                   record_ids: list[str], amount_at_risk_usd: float, evidence: str,
                   contract_clause: str, confidence: float) -> dict:
    if issue_type not in ISSUE_TYPES:
        raise ToolError(f"issue_type must be one of {ISSUE_TYPES}.")
    ids = sorted({r.strip() for r in record_ids if r.strip()})
    if not ids:
        raise ToolError("Cite at least one record ID.")
    if not 0 <= confidence <= 1:
        raise ToolError("confidence must be between 0 and 1.")
    finding = {
        "issue_type": issue_type, "supplier_id": supplier_id.strip(), "contract_id": contract_id.strip(),
        "record_ids": ids, "amount_at_risk_usd": round(float(amount_at_risk_usd), 2),
        "evidence": evidence, "contract_clause": contract_clause, "confidence": confidence,
    }
    finding["verification"] = verify_finding(finding)
    v = finding["verification"]
    finding["needs_review"] = v["status"] != "verified" or confidence < config.REVIEW_CONFIDENCE_THRESHOLD
    finding["review_status"] = "pending" if finding["needs_review"] else "auto_accepted"

    # Re-recording the same issue replaces the earlier finding instead of double counting.
    existing = next((fid for fid, f in state.findings.items()
                     if f["issue_type"] == issue_type and set(f["record_ids"]) == set(ids)), None)
    finding_id = existing or state.next_id()
    finding["finding_id"] = finding_id
    state.findings[finding_id] = finding
    return {"finding_id": finding_id, "replaced_existing": bool(existing), "verification": v,
            "needs_review": finding["needs_review"]}


def draft_supplier_email(state: RunState, finding_id: str, subject: str, body: str) -> dict:
    if finding_id not in state.findings:
        raise ToolError(f"No finding {finding_id} in this run.")
    state.drafts.append({"finding_id": finding_id, "subject": subject, "body": body})
    return {"saved": True, "sent": False, "note": "Saved as a draft for buyer review. Nothing was sent."}


HANDLERS = {
    "describe_schema": describe_schema,
    "run_sql": run_sql,
    "list_contracts": list_contracts,
    "get_contract": get_contract,
    "get_contract_terms": get_contract_terms,
    "record_finding": record_finding,
    "draft_supplier_email": draft_supplier_email,
}


def execute(state: RunState, name: str, tool_input: dict) -> tuple[str, bool]:
    """Run a tool. Returns (result as a JSON string, is_error)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool {name}."}), True
    if not isinstance(tool_input, dict):
        return json.dumps({"error": "Tool input must be a JSON object."}), True
    try:
        return json.dumps(handler(state, **tool_input), default=str), False
    except ToolError as e:
        return json.dumps({"error": str(e)}), True
    except TypeError as e:  # wrong or missing arguments
        return json.dumps({"error": f"Bad arguments for {name}: {e}"}), True
