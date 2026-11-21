"""MCP server exposing the agent's read-only procurement tools.

Any MCP client (Claude Desktop, Claude Code, an agent platform) can plug into the same tools
the agent uses: query spend, read contracts, and check a claimed finding against the data.

Run (stdio): python mcp_server.py
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from agent import tools
from agent.verify import verify_finding

server = MCPServer(
    name="procurement-compliance",
    instructions=("Read-only access to a synthetic procurement dataset: purchase orders, invoices, credits "
                  "and supplier contracts. Use check_finding to verify a suspected compliance issue."),
)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_state = tools.RunState()


def _call(name: str, **kwargs) -> str:
    out, is_error = tools.execute(_state, name, kwargs)
    if is_error:
        raise ValueError(json.loads(out)["error"])
    return out


@server.tool(annotations=READ_ONLY)
def describe_schema() -> str:
    """List tables, columns, row counts and sample rows of the procurement database."""
    return _call("describe_schema")


@server.tool(annotations=READ_ONLY)
def run_sql(query: str, purpose: str = "") -> str:
    """Run one read-only SQLite SELECT against the procurement database (max 200 rows)."""
    return _call("run_sql", query=query, purpose=purpose)


@server.tool(annotations=READ_ONLY)
def list_contracts() -> str:
    """List supplier contracts with supplier, category and term dates."""
    return _call("list_contracts")


@server.tool(annotations=READ_ONLY)
def get_contract(contract_id: str) -> str:
    """Full text of one contract, e.g. C03."""
    return _call("get_contract", contract_id=contract_id)


@server.tool(annotations=READ_ONLY)
def get_contract_terms(contract_id: str) -> str:
    """Structured terms of one contract: item prices, dates, payment terms, rebate rules."""
    return _call("get_contract_terms", contract_id=contract_id)


@server.tool(annotations=READ_ONLY)
def check_finding(issue_type: str, record_ids: list[str], amount_at_risk_usd: float) -> str:
    """Check a suspected compliance issue against the data and recompute its dollar impact.

    issue_type: price_above_contract | off_contract | expired_contract | invoice_po_mismatch |
    duplicate_invoice | missed_rebate. record_ids: PO IDs, invoice IDs, or contract:quarter keys
    (e.g. C02:2026Q1) depending on the type.
    """
    return json.dumps(verify_finding({"issue_type": issue_type, "record_ids": record_ids,
                                      "amount_at_risk_usd": amount_at_risk_usd}))


if __name__ == "__main__":
    server.run("stdio")
