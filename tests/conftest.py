import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402


@pytest.fixture
def true_terms(tmp_path, monkeypatch):
    """Point the agent at a terms.json built from the answer key (tests only)."""
    truth = json.loads((config.ANSWERS_DIR / "contract_terms_truth.json").read_text())
    path = tmp_path / "terms.json"
    path.write_text(json.dumps(truth))
    monkeypatch.setattr(config, "TERMS_PATH", path)
    return {t["contract_id"]: t for t in truth}


@pytest.fixture
def ground_truth():
    with (config.ANSWERS_DIR / "ground_truth.csv").open() as f:
        return list(csv.DictReader(f))
