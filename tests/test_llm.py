"""Tests for the news+LLM funnel (fake client, no network)."""

from __future__ import annotations

import asyncio

from polybot.data.models import Market
from polybot.llm.client import _extract_json
from polybot.llm.news_signal import NewsLLMAnalyzer


def _market(mid: str, q: str = "Q") -> Market:
    return Market.model_validate({
        "id": mid, "question": q, "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.4", "0.6"], "endDate": "2030-01-01T00:00:00Z",
    })


def _run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete_json(self, system, user, model, *, live_search=False, temperature=0.2, max_tokens=900):
        self.calls.append((model, live_search))
        return self.responses.pop(0) if self.responses else None


def test_extract_json():
    assert _extract_json('noise {"a": 1} tail') == {"a": 1}
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


def test_triage_selects_subset():
    markets = [_market("1"), _market("2"), _market("3")]
    fc = FakeClient([{"selected": ["2", "3"]}])
    analyzer = NewsLLMAnalyzer(fc, "triageM", "deepM", live_search=True)
    selected = _run(analyzer.triage(markets, limit=5))
    assert [m.id for m in selected] == ["2", "3"]
    assert fc.calls[0][0] == "triageM"


def test_triage_empty_on_no_response():
    analyzer = NewsLLMAnalyzer(FakeClient([None]), "t", "d")
    assert _run(analyzer.triage([_market("1")], limit=5)) == []


def test_deep_analyze():
    ok = NewsLLMAnalyzer(FakeClient([{"prob_yes": 0.7, "confidence": 0.8, "rationale": "x"}]),
                         "t", "deepM", live_search=True)
    sig = _run(ok.deep_analyze(_market("1"), 0.4))
    assert sig is not None and abs(sig.prob_yes - 0.7) < 1e-9 and sig.confidence == 0.8

    # low confidence still yields a Signal — the engine records it and gates itself
    low = NewsLLMAnalyzer(FakeClient([{"prob_yes": 0.7, "confidence": 0.3}]), "t", "d")
    low_sig = _run(low.deep_analyze(_market("1"), 0.4))
    assert low_sig is not None and low_sig.confidence == 0.3

    none = NewsLLMAnalyzer(FakeClient([None]), "t", "d")
    assert _run(none.deep_analyze(_market("1"), 0.4)) is None

    # prob is clamped, deep model + live_search are used
    fc = FakeClient([{"prob_yes": 1.5, "confidence": 0.9}])
    clamp = NewsLLMAnalyzer(fc, "t", "deepM", live_search=True)
    sig2 = _run(clamp.deep_analyze(_market("1"), 0.4))
    assert sig2 is not None and sig2.prob_yes == 0.99 and fc.calls[0] == ("deepM", True)
