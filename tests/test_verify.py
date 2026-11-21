import pytest

from agent.verify import verify_finding


def as_finding(gt):
    return {"issue_type": gt["issue_type"], "record_ids": gt["record_ids"].split(";"),
            "amount_at_risk_usd": float(gt["impact_usd"])}


def test_every_planted_issue_verifies_with_exact_amount(true_terms, ground_truth):
    for gt in ground_truth:
        v = verify_finding(as_finding(gt), true_terms)
        assert v["status"] == "verified", (gt["issue_id"], v)
        assert v["computed_amount_usd"] == pytest.approx(float(gt["impact_usd"]), abs=0.01), gt["issue_id"]


def test_wrong_amount_is_flagged(true_terms, ground_truth):
    f = as_finding(ground_truth[0])
    f["amount_at_risk_usd"] *= 3
    assert verify_finding(f, true_terms)["status"] == "amount_mismatch"


def test_made_up_id_is_not_supported(true_terms):
    f = {"issue_type": "price_above_contract", "record_ids": ["PO-99999"], "amount_at_risk_usd": 100}
    assert verify_finding(f, true_terms)["status"] == "not_supported"


def test_clean_po_is_not_a_price_issue(true_terms, ground_truth):
    planted = {r for gt in ground_truth for r in gt["record_ids"].split(";")}
    from agent.db import fetch_all
    clean = next(p["po_id"] for p in fetch_all("SELECT po_id FROM purchase_orders WHERE contract_id = 'C03'")
                 if p["po_id"] not in planted)
    f = {"issue_type": "price_above_contract", "record_ids": [clean], "amount_at_risk_usd": 10}
    assert verify_finding(f, true_terms)["status"] == "not_supported"


def test_q3_rebate_is_not_yet_due(true_terms):
    f = {"issue_type": "missed_rebate", "record_ids": ["C02:2026Q3"], "amount_at_risk_usd": 3000}
    v = verify_finding(f, true_terms)
    assert v["status"] == "not_supported" and "not due" in v["notes"][0]


def test_credited_rebate_is_not_missed(true_terms):
    f = {"issue_type": "missed_rebate", "record_ids": ["C01:2026Q1"], "amount_at_risk_usd": 3562.27}
    assert verify_finding(f, true_terms)["status"] == "not_supported"


def test_professional_services_spend_is_not_off_contract(true_terms):
    from agent.db import fetch_all
    po = fetch_all("SELECT po_id FROM purchase_orders WHERE supplier_id = 'S13' LIMIT 1")[0]["po_id"]
    f = {"issue_type": "off_contract", "record_ids": [po], "amount_at_risk_usd": 1}
    assert verify_finding(f, true_terms)["status"] == "not_supported"


def test_price_check_without_terms_is_unverifiable(ground_truth, tmp_path, monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "TERMS_PATH", tmp_path / "missing.json")
    gt = next(g for g in ground_truth if g["issue_type"] == "price_above_contract")
    assert verify_finding(as_finding(gt))["status"] == "unverifiable"
