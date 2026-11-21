import json

from agent.tools import TOOLS, RunState, execute


def call(state, name, **kwargs):
    out, is_error = execute(state, name, kwargs)
    return json.loads(out), is_error


def test_schemas_are_strict_and_closed():
    for t in TOOLS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False


def test_describe_schema_lists_tables():
    out, err = call(RunState(), "describe_schema")
    assert not err
    assert {"purchase_orders", "invoices", "contracts", "credits", "suppliers"} <= set(out["tables"])


def test_run_sql_returns_compact_rows():
    out, err = call(RunState(), "run_sql", query="SELECT supplier_id, COUNT(*) FROM purchase_orders "
                                               "GROUP BY 1 ORDER BY 1", purpose="test")
    assert not err and out["columns"] == ["supplier_id", "COUNT(*)"] and out["rows"][0][0] == "S01"


def test_run_sql_write_is_an_error_result():
    out, err = call(RunState(), "run_sql", query="DELETE FROM invoices", purpose="test")
    assert err and "rejected" in out["error"]


def test_bad_contract_id_is_an_error_result():
    out, err = call(RunState(), "get_contract", contract_id="../../.env")
    assert err and "Invalid contract ID" in out["error"]


def test_record_finding_verifies_and_dedupes(true_terms, ground_truth):
    gt = next(g for g in ground_truth if g["issue_type"] == "expired_contract")
    state = RunState()
    args = dict(issue_type=gt["issue_type"], supplier_id=gt["supplier_id"], contract_id=gt["contract_id"],
                record_ids=[gt["record_ids"]], amount_at_risk_usd=1.0, evidence="e", contract_clause="1. Term",
                confidence=0.9)
    first, err = call(state, "record_finding", **args)
    assert not err and first["verification"]["status"] == "amount_mismatch" and first["needs_review"]
    args["amount_at_risk_usd"] = float(gt["impact_usd"])
    second, _ = call(state, "record_finding", **args)
    assert second["finding_id"] == first["finding_id"] and second["replaced_existing"]
    assert second["verification"]["status"] == "verified" and not second["needs_review"]
    assert len(state.findings) == 1


def test_draft_email_requires_a_finding_and_never_sends():
    out, err = call(RunState(), "draft_supplier_email", finding_id="F-001", subject="s", body="b")
    assert err
