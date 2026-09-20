"""Learned per-gate failure prediction (4.42).

Gates no longer wait for a fixed "N opens since repair" threshold. Each gate's
own repair history - how many opens it survived, how many opens it had when it
broke - trains a small logistic model, and the gate is repaired once the next
open is predicted to break it. The deterministic opens rule remains the cold
start and the fallback when the model cannot discriminate (4.31's lesson: a
model that answers 0.99 to everything must never be allowed to queue work).
"""
import pytest

from app import db, ml_agent
from app.config import settings

GATES = ("PRED1", "PRED2", "PRED3")


@pytest.fixture(autouse=True)
def clean_history():
    for name in GATES:
        db._conn.execute("DELETE FROM component_events WHERE name = ?", (name,))
    db._conn.commit()
    ml_agent.reset_gate_models()
    yield
    for name in GATES:
        db._conn.execute("DELETE FROM component_events WHERE name = ?", (name,))
    db._conn.commit()
    ml_agent.reset_gate_models()


def _survived(gate, opens):
    db.record_component_event(gate, "BarrierGate", "gate_opens_at_repair", amount=float(opens))


def _broke(gate, opens):
    db.record_component_event(gate, "BarrierGate", "gate_opens_at_break", amount=float(opens))


# --------------------------------------------------------------------------- #
# Cold start: no history at all
# --------------------------------------------------------------------------- #
def test_cold_start_uses_the_opens_heuristic_not_a_model():
    fresh = ml_agent.gate_failure_prediction("PRED1", 0)
    assert fresh["source"] == "heuristic"
    assert not fresh["needs_repair"], "a gate repaired one open ago is not due"

    due = ml_agent.gate_failure_prediction("PRED1", settings.gate_expected_break_opens)
    assert due["needs_repair"], "at the expected break point the gate must be repaired"
    assert due["failure_probability"] >= settings.gate_failure_probability


def test_cold_start_probability_rises_with_opens():
    probs = [ml_agent.gate_failure_prediction("PRED1", n)["failure_probability"] for n in range(0, 12)]
    assert probs == sorted(probs)
    assert probs[0] < probs[-1]


# --------------------------------------------------------------------------- #
# A trained per-gate model
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (ml_agent._SKLEARN_AVAILABLE and ml_agent._NUMPY_AVAILABLE),
                    reason="scikit-learn/numpy not installed")
def test_a_gate_that_keeps_breaking_early_is_repaired_earlier_than_the_heuristic():
    # PRED2 breaks at 4 opens every time, and has survived being repaired at 1-2.
    for _ in range(4):
        _survived("PRED2", 1)
        _survived("PRED2", 2)
        _broke("PRED2", 4)
        _broke("PRED2", 5)
    ml_agent.reset_gate_models()
    prediction = ml_agent.gate_failure_prediction("PRED2", 3)
    assert prediction["source"] == "model", "enough of its own history to learn from"
    assert prediction["needs_repair"], "it must be repaired before its 4th open"
    assert not ml_agent.gate_failure_prediction("PRED2", 0)["needs_repair"]


@pytest.mark.skipif(not (ml_agent._SKLEARN_AVAILABLE and ml_agent._NUMPY_AVAILABLE),
                    reason="scikit-learn/numpy not installed")
def test_a_degenerate_model_is_rejected_and_the_heuristic_takes_over():
    # 4.31: trained on breakages alone the model answers "about to fail" at every
    # wear level, and with no cap on concurrent repairs that would put every gate
    # in the park into repair at once. A model that calls a just-repaired gate
    # due is not used.
    for opens in (1, 2, 3, 4, 5, 6, 7, 8):
        _broke("PRED3", opens)
    _survived("PRED3", 1)
    ml_agent.reset_gate_models()
    prediction = ml_agent.gate_failure_prediction("PRED3", 0)
    assert prediction["source"] == "heuristic", "a model that fires at 0 opens is discarded"
    assert not prediction["needs_repair"]


def test_history_from_other_gates_is_not_mixed_into_a_gates_own_model():
    for _ in range(6):
        _broke("PRED2", 3)
        _survived("PRED2", 1)
    ml_agent.reset_gate_models()
    # PRED1 has no history of its own; it must not inherit PRED2's break point.
    assert ml_agent.gate_failure_prediction("PRED1", 2)["source"] == "heuristic"


def test_prediction_never_raises_without_sklearn(monkeypatch):
    monkeypatch.setattr(ml_agent, "_SKLEARN_AVAILABLE", False)
    ml_agent.reset_gate_models()
    prediction = ml_agent.gate_failure_prediction("PRED1", 7)
    assert prediction["source"] == "heuristic"
    assert 0.0 <= prediction["failure_probability"] <= 1.0
