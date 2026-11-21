# Procurement Contract-Compliance Agent

A Claude agent that audits purchase orders and invoices against supplier contracts, records evidence-backed findings, and writes a brief for a buyer. All data is synthetic.

## Run
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then put `ANTHROPIC_API_KEY` in `.env`
- Regenerate data and DB: `.venv/bin/python data/generate.py`
- Tests (no API calls): `.venv/bin/pytest -q`
- Extract contract terms: `.venv/bin/python -m agent.extract_terms`
- Ask: `.venv/bin/python -m agent.loop ask "question"`. Audit: `.venv/bin/python -m agent.loop audit`
- Evals: `.venv/bin/python evals/run_eval.py`
- UI: `.venv/bin/streamlit run app.py`
- MCP server: `.venv/bin/python mcp_server.py`

## Conventions
- The agent's tools must never read `data/answers/`. That folder is the answer key for evals only.
- SQL access is read-only (`agent/db.py`). Never add a tool that writes to the database or sends anything externally. Supplier emails are drafts only.
- Every finding is re-checked in code by `agent/verify.py`. Don't trust model-stated amounts.
- The model ID and budgets live in `agent/config.py` only.
- Tests must not call the API.
