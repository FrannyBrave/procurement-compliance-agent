"""Tests for the agent loop with a scripted fake client (no API calls)."""

from contextlib import contextmanager
from types import SimpleNamespace as NS

from agent import config
from agent.loop import run_agent


def usage():
    return NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def reply(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop, usage=usage(), model="claude-opus-5-5")


def text(t):
    return NS(type="text", text=t)


def tool(name, id_, **inp):
    return NS(type="tool_use", name=name, id=id_, input=inp)


class FakeClient:
    """Replays scripted responses and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        r = self.responses.pop(0)
        yield NS(get_final_message=lambda: r)


def test_loop_runs_tools_and_returns_final_text(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    client = FakeClient([
        reply(tool("run_sql", "t1", query="SELECT COUNT(*) FROM invoices", purpose="count"), stop="tool_use"),
        reply(text("There are 220 invoices.")),
    ])
    res = run_agent("How many invoices?", client=client)
    assert res.status == "completed" and res.final_text == "There are 220 invoices."
    assert res.tool_calls == 1
    # The tool result went back in the next request, matched to the tool_use id.
    last = client.requests[1]["messages"][-1]["content"][0]
    assert last["tool_use_id"] == "t1" and last["is_error"] is False and "220" in last["content"]


def test_tool_budget_stops_tools_and_forces_wrap_up(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    sql = dict(query="SELECT 1", purpose="p")
    client = FakeClient([
        reply(tool("run_sql", "a", **sql), tool("run_sql", "b", **sql), stop="tool_use"),
        reply(text("Partial answer.")),
    ])
    res = run_agent("q", client=client, max_tool_calls=1)
    assert res.tool_calls == 1 and res.status == "budget_exhausted"
    assert client.requests[1]["tool_choice"] == {"type": "none"}
    results = client.requests[1]["messages"][-1]["content"]
    assert results[1]["is_error"] and "Budget exhausted" in results[1]["content"]


def test_truncated_tool_call_is_never_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    client = FakeClient([reply(tool("run_sql", "a", query="SELECT"), stop="max_tokens")])
    res = run_agent("q", client=client)
    assert res.status == "max_tokens" and res.tool_calls == 0


def test_refusal_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    res = run_agent("q", client=FakeClient([reply(stop="refusal")]))
    assert res.status == "refusal"
