"""The agent loop.

    send messages -> model replies -> if it asked for tools, run them and send the results back -> repeat

Written by hand (no framework) so every step is visible: budgets, error handling, tracing.

CLI:
    python -m agent.loop ask "Which supplier was overcharged the most?"
    python -m agent.loop audit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import anthropic

from . import config, prompts
from .tools import TOOLS, RunState, execute

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TURNS = 40  # hard stop on model round trips, on top of the tool-call and cost budgets


@dataclass
class RunResult:
    run_id: str
    mode: str
    task: str
    model: str
    effort: str
    status: str = "running"         # completed | budget_exhausted | max_tokens | refusal | error
    final_text: str = ""
    findings: list[dict] = field(default_factory=list)
    drafts: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    tool_calls: int = 0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    started_at: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _emit(on_event, run: RunResult, kind: str, **data):
    event = {"kind": kind, "t": round(time.monotonic(), 3), **data}
    run.trace.append(event)
    if on_event:
        on_event(event)


def _final_text(content) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def run_agent(task: str, mode: str = "ask", state: RunState | None = None,
              on_event: Callable[[dict], None] | None = None,
              model: str = config.MODEL, effort: str = config.EFFORT,
              max_tool_calls: int = config.MAX_TOOL_CALLS, max_cost_usd: float = config.MAX_COST_USD,
              client: anthropic.Anthropic | None = None) -> RunResult:
    client = client or anthropic.Anthropic()
    state = state or RunState()
    result = RunResult(run_id=f"{mode}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}", mode=mode,
                       task=task, model=model, effort=effort,
                       started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    t_start = time.monotonic()
    messages: list[dict] = [{"role": "user", "content": task}]
    out_of_budget = False

    try:
        while True:
            result.turns += 1
            if result.turns > MAX_TURNS:
                out_of_budget = True
            # Once a budget is spent, give the model one last turn with tools switched off to wrap up.
            tool_choice = {"type": "none"} if out_of_budget else {"type": "auto"}

            t0 = time.monotonic()
            with client.beta.messages.stream(
                model=model,
                max_tokens=config.MAX_TOKENS,
                system=[{"type": "text", "text": prompts.SYSTEM_PROMPT}],
                tools=TOOLS,
                tool_choice=tool_choice,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},   # cache the growing conversation each turn
                betas=[FALLBACK_BETA],
                fallbacks="default",                   # on a refusal, the API retries on a fallback model
            ) as stream:
                response = stream.get_final_message()

            u = response.usage
            result.input_tokens += u.input_tokens or 0
            result.output_tokens += u.output_tokens or 0
            result.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
            result.cost_usd += config.cost_usd(model, u)
            for b in response.content:
                if b.type == "thinking" and b.thinking:
                    _emit(on_event, result, "thinking", text=b.thinking)
                elif b.type == "text" and b.text.strip():
                    _emit(on_event, result, "text", text=b.text)
            _emit(on_event, result, "turn", turn=result.turns, stop_reason=response.stop_reason,
                  served_by=response.model, latency_s=round(time.monotonic() - t0, 2),
                  cost_usd=round(result.cost_usd, 4))

            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "refusal":
                result.status = "refusal"
                result.final_text = _final_text(response.content)
                break
            if response.stop_reason == "max_tokens":
                # A tool call cut off mid-input would parse as a partial object; never run it.
                result.status = "max_tokens"
                result.final_text = _final_text(response.content)
                break
            if response.stop_reason == "pause_turn":
                continue
            if not tool_uses:
                result.status = "budget_exhausted" if out_of_budget else "completed"
                result.final_text = _final_text(response.content)
                break

            # Run every tool the model asked for, and return all results in one message.
            tool_results = []
            for block in tool_uses:
                over = result.tool_calls >= max_tool_calls or result.cost_usd >= max_cost_usd
                if over:
                    out_of_budget = True
                    content, is_error = json.dumps({"error": "Budget exhausted. Stop calling tools and give "
                                                             "your answer with what you have."}), True
                    ms = 0
                else:
                    result.tool_calls += 1
                    t1 = time.monotonic()
                    content, is_error = execute(state, block.name, block.input)
                    ms = round((time.monotonic() - t1) * 1000)
                _emit(on_event, result, "tool", name=block.name, input=block.input, is_error=is_error,
                      result=content[:4000], ms=ms)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": tool_results})

    except anthropic.APIStatusError as e:
        result.status, result.error = "error", f"API error {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        result.status, result.error = "error", f"Connection error: {e}"

    result.findings = list(state.findings.values())
    result.drafts = state.drafts
    result.cost_usd = round(result.cost_usd, 4)
    result.duration_s = round(time.monotonic() - t_start, 1)
    _emit(on_event, result, "done", status=result.status, error=result.error)
    save_run(result)
    return result


def save_run(result: RunResult) -> str:
    config.RUNS_DIR.mkdir(exist_ok=True)
    path = config.RUNS_DIR / f"{result.run_id}.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return str(path)


def run_audit(**kwargs) -> RunResult:
    return run_agent(prompts.AUDIT_TASK, mode="audit", **kwargs)


def run_ask(question: str, state: RunState | None = None, **kwargs) -> RunResult:
    context = ""
    if state and state.findings:
        context = "\n".join(f"- {f['finding_id']}: {f['issue_type']} {', '.join(f['record_ids'])} "
                            f"${(f['verification']['computed_amount_usd'] or 0):,.2f}" for f in state.findings.values())
    return run_agent(prompts.ask_task(question, context), mode="ask", state=state, **kwargs)


# ---------------------------------------------------------------- CLI

def _print_event(e: dict):
    k = e["kind"]
    if k == "tool":
        mark = "x" if e["is_error"] else ">"
        inp = e["input"].get("purpose") or e["input"].get("query") or json.dumps(e["input"])[:120]
        print(f"  {mark} {e['name']}: {inp}", flush=True)
        if e["name"] == "record_finding":
            v = json.loads(e["result"]).get("verification", {})
            print(f"      -> {v.get('status')} ${v.get('computed_amount_usd')}", flush=True)
        elif e["is_error"]:
            print(f"      -> {e['result'][:200]}", flush=True)
    elif k == "turn":
        print(f"[turn {e['turn']} {e['stop_reason']} {e['latency_s']}s ${e['cost_usd']}]", flush=True)
    elif k == "done":
        print(f"[done: {e['status']}] {e.get('error', '')}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Procurement compliance agent")
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("ask")
    a.add_argument("question")
    sub.add_parser("audit")
    for p in sub.choices.values():
        p.add_argument("--model", default=config.MODEL)
        p.add_argument("--effort", default=config.EFFORT)
    args = ap.parse_args()

    kw = dict(on_event=_print_event, model=args.model, effort=args.effort)
    res = run_ask(args.question, **kw) if args.mode == "ask" else run_audit(**kw)
    print("\n" + "=" * 70 + "\n" + (res.final_text or res.error))
    print("=" * 70)
    verified = [f for f in res.findings if f["verification"]["status"] == "verified"]
    print(f"{len(res.findings)} findings ({len(verified)} verified, "
          f"${sum(f['verification']['computed_amount_usd'] for f in verified):,.2f}); "
          f"{res.tool_calls} tool calls, {res.turns} turns, ${res.cost_usd:.2f}, {res.duration_s}s")
    sys.exit(0 if res.status in ("completed", "budget_exhausted") else 1)


if __name__ == "__main__":
    main()
