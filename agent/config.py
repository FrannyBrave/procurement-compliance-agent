"""All tunable settings in one place. Values can be overridden in .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "procurement.db"
CONTRACTS_DIR = DATA_DIR / "contracts"
TERMS_PATH = CONTRACTS_DIR / "terms.json"     # written by agent/extract_terms.py
ANSWERS_DIR = DATA_DIR / "answers"            # eval answer key: tools must never read this
RUNS_DIR = ROOT / "runs"                      # every run's trace (gitignored)
RESULTS_DIR = ROOT / "results"                # curated runs committed for demo mode

# The audit is run "as of" the end of the data period.
AS_OF_DATE = "2026-09-30"

MODEL = os.getenv("AGENT_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("AGENT_EFFORT", "medium")          # low | medium | high | xhigh | max
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", MODEL)
MAX_TOKENS = 32000                                     # per model response (streamed)

# Guardrails
MAX_TOOL_CALLS = int(os.getenv("AGENT_MAX_TOOL_CALLS", "60"))
MAX_COST_USD = float(os.getenv("AGENT_MAX_COST_USD", "3.00"))
SQL_ROW_LIMIT = 200
SQL_TIMEOUT_SECONDS = 5
REVIEW_CONFIDENCE_THRESHOLD = 0.8   # below this, a finding goes to the review queue even if verified
DISABLE_LIVE = os.getenv("AGENT_DISABLE_LIVE", "0") == "1"

# USD per million tokens: (input, output, cache read, cache write 5m)
PRICING = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}


def cost_usd(model: str, usage) -> float:
    """Cost of one response from its usage block."""
    inp, out, cache_read, cache_write = PRICING.get(model, PRICING["claude-opus-5-5"])
    return (
        (getattr(usage, "input_tokens", 0) or 0) * inp
        + (getattr(usage, "output_tokens", 0) or 0) * out
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * cache_read
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * cache_write
    ) / 1_000_000
