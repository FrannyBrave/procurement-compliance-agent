"""Streamlit UI for the procurement compliance agent.

Tabs: Ask (chat with live tool trace) · Audit (full sweep + brief) · Review queue (human in the loop)
· Evals. Demo mode replays saved runs from results/, so it works without an API key.

Run: streamlit run app.py
"""

from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st

from agent import config
from agent.db import fetch_all
from agent.tools import RunState

st.set_page_config(page_title="Procurement Compliance Agent", page_icon="📑", layout="wide")

ISSUE_LABELS = {
    "price_above_contract": "Price above contract",
    "off_contract": "Off-contract spend",
    "expired_contract": "Expired contract",
    "invoice_po_mismatch": "Invoice ≠ PO",
    "duplicate_invoice": "Duplicate invoice",
    "missed_rebate": "Missed rebate",
}
SUPPLIER_NAMES = {r["supplier_id"]: r["name"] for r in fetch_all("SELECT supplier_id, name FROM suppliers")}
STATUS_ICON = {"verified": "✅", "amount_mismatch": "⚠️", "not_supported": "❌", "unverifiable": "❔"}

# ---------------------------------------------------------------- state

ss = st.session_state
ss.setdefault("run_state", RunState())
ss.setdefault("audit", None)          # RunResult dict of the latest audit
ss.setdefault("chat", [])             # list of {"q", "answer", "trace", "meta"}


def load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def adopt_findings(run: dict):
    """Put a run's findings into the shared review state (keeps reviewer decisions)."""
    for f in run.get("findings", []):
        existing = ss.run_state.findings.get(f["finding_id"])
        if existing and existing.get("review_status") in ("approved", "rejected"):
            f["review_status"] = existing["review_status"]
        ss.run_state.findings[f["finding_id"]] = f
    ss.run_state._seq = max([ss.run_state._seq] + [int(fid[2:]) for fid in ss.run_state.findings])


# ---------------------------------------------------------------- sidebar

has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
with st.sidebar:
    st.title("📑 Compliance Agent")
    st.caption("Audits POs and invoices against supplier contracts. All data is synthetic.")
    live_possible = has_key and not config.DISABLE_LIVE
    demo = st.toggle("Demo mode (replay saved runs)", value=not live_possible, disabled=not live_possible,
                     help="Live mode calls the Claude API with your key." if live_possible
                     else "Live mode needs ANTHROPIC_API_KEY in .env.")
    st.divider()
    st.markdown(f"**Model** `{config.MODEL}` · effort `{config.EFFORT}`")
    st.markdown(f"**Budgets** {config.MAX_TOOL_CALLS} tool calls · ${config.MAX_COST_USD:.2f} per run")
    st.markdown(f"**Audit date** {config.AS_OF_DATE}")
    st.divider()
    st.markdown("**Guardrails**\n- Read-only SQL (3 layers)\n- Every finding re-checked in code\n"
                "- Low confidence → human review\n- Emails are drafts only")

    if demo and ss.audit is None and (saved := load_json(config.RESULTS_DIR / "demo_audit.json")):
        ss.audit = saved
        adopt_findings(saved)


# ---------------------------------------------------------------- trace rendering

def describe_event(e: dict) -> tuple[str, str | None]:
    """One-line label and optional detail for a trace event."""
    if e["kind"] == "tool":
        inp = e["input"]
        if e["name"] == "run_sql":
            label = f"🔎 SQL: {inp.get('purpose') or 'query'}"
            detail = f"```sql\n{inp.get('query', '')}\n```"
        elif e["name"] == "record_finding":
            v = json.loads(e["result"]).get("verification", {}) if not e["is_error"] else {}
            label = (f"{STATUS_ICON.get(v.get('status'), '📝')} Finding: {ISSUE_LABELS.get(inp['issue_type'])} "
                     f"{', '.join(inp['record_ids'])} · {v.get('status', 'error')} "
                     f"${(v.get('computed_amount_usd') or 0):,.2f}")
            detail = inp.get("evidence")
        else:
            label = f"🛠️ {e['name']}({', '.join(f'{k}={v}' for k, v in inp.items())})"
            detail = None
        if e["is_error"]:
            label = "⛔ " + label
            detail = e["result"][:500]
        return label, detail
    if e["kind"] == "thinking":
        return "💭 " + e["text"].strip().split("\n")[0][:140], e["text"]
    return "", None


def render_trace(trace: list[dict], container):
    for e in trace:
        label, detail = describe_event(e)
        if not label:
            continue
        if detail:
            with container.expander(label):
                st.markdown(detail)
        else:
            container.markdown(label)


def run_meta(r: dict) -> str:
    return (f"{len(r['findings'])} findings · {r['tool_calls']} tool calls · {r['turns']} turns · "
            f"${r['cost_usd']:.2f} · {r['duration_s']}s · {r['status']}")


def live_run(fn, status_label: str, **kwargs) -> dict:
    """Run the agent and stream its steps into a status box."""
    with st.status(status_label, expanded=True) as box:
        def on_event(e):
            label, detail = describe_event(e)
            if label:
                if detail:
                    with box.expander(label):
                        st.markdown(detail)
                else:
                    box.markdown(label)
        res = fn(on_event=on_event, **kwargs).to_dict()
        box.update(label=f"Done: {run_meta(res)}", state="complete" if res["status"] != "error" else "error",
                   expanded=False)
    return res


# ---------------------------------------------------------------- tabs

tab_ask, tab_audit, tab_review, tab_evals = st.tabs(["💬 Ask", "🧾 Audit", "👤 Review queue", "📊 Evals"])

# ---- Ask
with tab_ask:
    st.subheader("Ask a question about spend, contracts or invoices")
    examples = ["Which supplier has been charged the most above contract price, and on which POs?",
                "Are any contracts we still buy against already expired?",
                "Did Northline Polymers pay the volume rebates they owe us?"]
    if demo:
        saved = load_json(config.RESULTS_DIR / "demo_ask.json")
        if saved:
            st.info("Demo mode: replaying a saved run. Turn demo mode off in the sidebar to ask your own question.")
            with st.chat_message("user"):
                st.markdown(saved["task"].split("\n\n")[0])
            with st.chat_message("assistant"):
                with st.expander(f"Agent steps · {run_meta(saved)}"):
                    render_trace(saved["trace"], st)
                st.markdown(saved["final_text"])
    else:
        for turn in ss.chat:
            with st.chat_message("user"):
                st.markdown(turn["q"])
            with st.chat_message("assistant"):
                with st.expander(f"Agent steps · {turn['meta']}"):
                    render_trace(turn["trace"], st)
                st.markdown(turn["answer"])
        cols = st.columns(len(examples))
        clicked = next((ex for c, ex in zip(cols, examples) if c.button(ex, use_container_width=True)), None)
        question = st.chat_input("Ask the agent…") or clicked
        if question:
            from agent.loop import run_ask
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                res = live_run(run_ask, "Working…", question=question, state=ss.run_state)
                st.markdown(res["final_text"] or res["error"])
            ss.chat.append({"q": question, "answer": res["final_text"] or res["error"],
                            "trace": res["trace"], "meta": run_meta(res)})

# ---- Audit
with tab_audit:
    st.subheader("Full contract-compliance audit")
    st.caption("The agent sweeps all six issue types, records each finding with evidence, "
               "and writes a brief for the buyer.")
    if not demo and st.button("▶ Run full audit", type="primary"):
        from agent.loop import run_audit
        ss.run_state = RunState()
        ss.audit = live_run(run_audit, "Auditing…", state=ss.run_state)
        adopt_findings(ss.audit)

    audit = ss.audit
    if audit:
        findings = audit["findings"]
        verified = [f for f in findings if f["verification"]["status"] == "verified"]
        c = st.columns(4)
        c[0].metric("Findings", len(findings))
        c[1].metric("Verified $ at risk", f"${sum(f['verification']['computed_amount_usd'] for f in verified):,.0f}")
        c[2].metric("Needs review", sum(1 for f in findings if f["needs_review"]))
        c[3].metric("Run cost", f"${audit['cost_usd']:.2f}")
        c[3].caption(f"{audit['duration_s']:.0f}s · {audit['tool_calls']} tool calls · {audit['turns']} turns")

        df = pd.DataFrame([{
            "ID": f["finding_id"], "Type": ISSUE_LABELS[f["issue_type"]],
            "Supplier": SUPPLIER_NAMES.get(f["supplier_id"], f["supplier_id"]),
            "Records": ", ".join(f["record_ids"]), "$ at risk": f["verification"]["computed_amount_usd"],
            "Check": STATUS_ICON[f["verification"]["status"]] + " " + f["verification"]["status"],
            "Confidence": f["confidence"], "Evidence": f["evidence"]} for f in findings])
        left, right = st.columns([2, 3])
        with left:
            st.markdown("**$ at risk by issue type**")
            st.bar_chart(df.groupby("Type")["$ at risk"].sum().sort_values(), horizontal=True)
        with right:
            st.markdown("**$ at risk by supplier**")
            st.bar_chart(df.groupby("Supplier")["$ at risk"].sum().sort_values(), horizontal=True)
        st.dataframe(df, hide_index=True, use_container_width=True,
                     column_config={"$ at risk": st.column_config.NumberColumn(format="$%.2f")})
        st.markdown("---")
        st.markdown(audit["final_text"])
        with st.expander(f"Agent steps · {run_meta(audit)}"):
            render_trace(audit["trace"], st)
    elif demo:
        st.warning("No saved audit found in results/demo_audit.json.")

# ---- Review queue
with tab_review:
    st.subheader("Human review")
    st.caption("Findings the verifier couldn't confirm, or that the agent wasn't confident about, wait here. "
               "Nothing goes to a supplier until a person approves it.")
    findings = list(ss.run_state.findings.values())
    if not findings:
        st.info("Run an audit (or ask a question) to populate the queue.")
    show_all = st.toggle("Show all findings, not only those needing review")
    queue = [f for f in findings if show_all or f["needs_review"]]
    decided = sum(1 for f in findings if f["review_status"] in ("approved", "rejected"))
    st.markdown(f"**{len(queue)}** shown · **{decided}** decided · "
                f"**{sum(1 for f in findings if f['review_status'] == 'auto_accepted')}** auto-accepted")
    for f in queue:
        v = f["verification"]
        with st.container(border=True):
            a, b = st.columns([4, 1])
            a.markdown(f"**{f['finding_id']} · {ISSUE_LABELS[f['issue_type']]}** · "
                       f"{SUPPLIER_NAMES.get(f['supplier_id'], f['supplier_id'])} · "
                       f"{', '.join(f['record_ids'])} · **${(v['computed_amount_usd'] or 0):,.2f}**  \n"
                       f"{STATUS_ICON[v['status']]} verifier: {v['status']} · confidence {f['confidence']:.2f} · "
                       f"review: *{f['review_status']}*")
            a.markdown(f"> {f['evidence']}")
            if f.get("contract_clause"):
                a.caption(f"Clause: {f['contract_clause']}")
            a.caption(" · ".join(v["notes"]))
            if b.button("Approve", key=f"ap-{f['finding_id']}", use_container_width=True):
                f["review_status"] = "approved"
                st.rerun()
            if b.button("Reject", key=f"rj-{f['finding_id']}", use_container_width=True):
                f["review_status"] = "rejected"
                st.rerun()
            if not demo and b.button("Draft email", key=f"em-{f['finding_id']}", use_container_width=True):
                from agent.loop import run_ask
                live_run(run_ask, "Drafting…", state=ss.run_state,
                         question=f"Draft a short, polite dispute email to the supplier for finding "
                                  f"{f['finding_id']} and save it with draft_supplier_email.")
            for d in [d for d in ss.run_state.drafts if d["finding_id"] == f["finding_id"]]:
                with a.expander(f"✉️ Draft (not sent): {d['subject']}"):
                    st.text(d["body"])
    if findings:
        export = pd.DataFrame([{k: f[k] for k in ("finding_id", "issue_type", "supplier_id", "contract_id",
                                                   "amount_at_risk_usd", "review_status")}
                               | {"record_ids": ";".join(f["record_ids"]),
                                  "verified_amount_usd": f["verification"]["computed_amount_usd"],
                                  "verification": f["verification"]["status"]} for f in findings])
        st.download_button("Download findings (CSV)", export.to_csv(index=False), "findings.csv", "text/csv")

# ---- Evals
with tab_evals:
    st.subheader("How well does it work?")
    st.caption("Scored against an answer key of 26 planted issues ($74,037.25) that the agent never sees.")
    evals = sorted(config.RESULTS_DIR.glob("eval_*.json"))
    report = load_json(evals[-1]) if evals else None
    if report and report.get("audit"):
        runs = report["audit"]
        s = report["audit_summary"]
        c = st.columns(5)
        c[0].metric("Audit runs", len(runs))
        c[1].metric("Precision (mean)", f"{s['precision']['mean']:.0%}")
        c[2].metric("Recall (mean)", f"{s['recall']['mean']:.0%}")
        c[3].metric("Avg cost / audit", f"${s['cost_usd']['mean']:.2f}")
        c[4].metric("Avg time / audit", f"{s['duration_s']['mean']:.0f}s")
        st.dataframe(pd.DataFrame([{"Run": r["run_id"], "Precision": r["precision"], "Recall": r["recall"],
                                    "F1": r["f1"], "$ found": r["usd_found"], "Cost": r["cost_usd"],
                                    "Tool calls": r["tool_calls"], "Seconds": r["duration_s"]} for r in runs]),
                     hide_index=True, use_container_width=True)
        per_type = pd.DataFrame([{"Issue type": ISSUE_LABELS[t], "Planted": v["planted"],
                                  "Found (mean)": sum(r["per_type"][t]["found"] for r in runs) / len(runs),
                                  "False positives (total)": sum(r["per_type"][t]["false_positives"] for r in runs)}
                                 for t, v in runs[0]["per_type"].items()])
        st.markdown("**By issue type**")
        st.dataframe(per_type, hide_index=True, use_container_width=True)
    if report and report.get("qa"):
        qa = report["qa"]
        st.markdown(f"**Q&A eval:** {qa['accuracy']:.0%} of {qa['n']} questions correct · "
                    f"avg ${qa['avg_cost_usd']:.3f} and {qa['avg_duration_s']}s per question")
        st.dataframe(pd.DataFrame([{"Question": q["question"], "Correct": "✅" if q["correct"] else "❌",
                                    "Cost": q["cost_usd"], "Seconds": q["duration_s"]} for q in qa["questions"]]),
                     hide_index=True, use_container_width=True)
    if ext := load_json(config.RESULTS_DIR / "extraction_eval.json"):
        st.markdown(f"**Contract term extraction:** {ext['accuracy']:.0%} of {ext['fields_checked']} fields correct")
    if not report:
        st.info("No eval results yet. Run `python evals/run_eval.py --audit-runs 3 --qa`.")
