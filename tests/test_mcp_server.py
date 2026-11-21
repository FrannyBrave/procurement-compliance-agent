"""Connect an MCP client to the server in-process and call its tools."""

import asyncio
import json

from mcp import Client

from mcp_server import server


async def _session():
    async with Client(server) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        res = await client.call_tool("run_sql", {"query": "SELECT COUNT(*) AS n FROM suppliers"})
        check = await client.call_tool("check_finding", {"issue_type": "expired_contract",
                                                         "record_ids": ["PO-10162"], "amount_at_risk_usd": 9400})
        bad = await client.call_tool("run_sql", {"query": "DROP TABLE invoices"})
        return names, res, check, bad


def test_mcp_tools_work_end_to_end(true_terms):
    names, res, check, bad = asyncio.run(_session())
    assert names == {"describe_schema", "run_sql", "list_contracts", "get_contract",
                     "get_contract_terms", "check_finding"}
    assert json.loads(res.content[0].text)["rows"] == [[15]]
    assert json.loads(check.content[0].text)["status"] == "verified"
    assert bad.is_error
