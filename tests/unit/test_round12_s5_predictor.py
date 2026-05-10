"""Round 12 S5 — Quota predictor (linear-fit exhaust-time prediction).

Verifies the predictor behaviour described in
`.trellis/tasks/05-11-s5s6-predictive-concurrent-rotate/prd.md`:

* < 3 history points → predict_exhaust_time returns None
* linear decreasing samples → predict within ±10 percent of the analytic root
* flat / increasing slope → None
* should_preempt honours the lead window
* record + load_history roundtrip is correct
* load_history(max_points=N) truncates to the most recent N
"""
from __future__ import annotations

import json
import time

import pytest

from autoteam.quota_predictor import QuotaPredictor


@pytest.fixture
def predictor(tmp_path):
    return QuotaPredictor(history_path=tmp_path / "quota_history.jsonl")


class TestRecordAndLoad:
    def test_record_creates_jsonl_file_and_roundtrip(self, predictor):
        ts0 = 1_700_000_000.0
        predictor.record("a@x.com", 90.0, t_remain=3600, ts=ts0)
        predictor.record("a@x.com", 80.0, t_remain=3000, ts=ts0 + 60)
        predictor.record("b@x.com", 50.0, ts=ts0 + 30)

        history_a = predictor.load_history("a@x.com")
        history_b = predictor.load_history("b@x.com")
        assert [r["p_remain"] for r in history_a] == [90.0, 80.0]
        assert [r["ts"] for r in history_a] == [ts0, ts0 + 60]
        assert len(history_b) == 1 and history_b[0]["p_remain"] == 50.0

    def test_load_history_skips_malformed_lines(self, predictor):
        # Manually append a malformed line — load_history must keep working.
        predictor.record("a@x.com", 90.0, ts=1.0)
        with open(predictor.history_path, "a", encoding="utf-8") as fp:
            fp.write("{NOT_JSON\n")
            fp.write("\n")  # blank line too
        predictor.record("a@x.com", 85.0, ts=2.0)
        history = predictor.load_history("a@x.com")
        assert [r["p_remain"] for r in history] == [90.0, 85.0]

    def test_load_history_max_points_truncates_to_most_recent(self, predictor):
        for i in range(100):
            predictor.record("a@x.com", 100.0 - i, ts=float(i))
        history = predictor.load_history("a@x.com", max_points=5)
        assert len(history) == 5
        assert [r["ts"] for r in history] == [95.0, 96.0, 97.0, 98.0, 99.0]

    def test_record_with_empty_email_is_noop(self, predictor):
        predictor.record("", 50.0, ts=1.0)
        assert not predictor.history_path.exists()

    def test_record_bad_payload_does_not_raise(self, predictor):
        # p_remain unconvertible to float — log warning, no crash, no row written.
        predictor.record("a@x.com", "not-a-number", ts=1.0)  # type: ignore[arg-type]
        assert not predictor.history_path.exists() or predictor.load_history("a@x.com") == []


class TestPredictExhaust:
    def test_insufficient_history_returns_none(self, predictor):
        predictor.record("a@x.com", 90.0, ts=1.0)
        predictor.record("a@x.com", 80.0, ts=2.0)
        assert predictor.predict_exhaust_time("a@x.com") is None

    def test_linear_decline_predicts_within_ten_percent(self, predictor):
        # Quota drops 5% every 60s starting from 100% at t=0 → exhaust at t = 100/5 * 60 = 1200.
        for i in range(10):
            predictor.record("a@x.com", 100.0 - i * 5.0, ts=float(i * 60))
        predicted = predictor.predict_exhaust_time("a@x.com")
        assert predicted is not None
        # exact analytic root is 1140 (last point is t=540, p_remain=55 → projects to 0 at t=1200
        # actually fitting all 10 points: linear through (0,100), (60,95), ..., (540,55) → exhaust at t=1200
        analytic_root = 1200.0
        assert abs(predicted - analytic_root) / analytic_root < 0.10

    def test_flat_history_returns_none(self, predictor):
        for i in range(5):
            predictor.record("a@x.com", 80.0, ts=float(i * 60))
        assert predictor.predict_exhaust_time("a@x.com") is None

    def test_increasing_slope_returns_none(self, predictor):
        for i in range(5):
            predictor.record("a@x.com", 50.0 + i * 5.0, ts=float(i * 60))
        # quota growing — no exhaust prediction
        assert predictor.predict_exhaust_time("a@x.com") is None


class TestShouldPreempt:
    def test_within_lead_window_returns_true(self, predictor):
        # Set up exhaust prediction at t=600 (10 minutes from now=0).
        # 5 points: (0, 100), (60, 90), (120, 80), (180, 70), (240, 60) → linear to 0 at t=600
        for i in range(5):
            predictor.record("a@x.com", 100.0 - i * 10.0, ts=float(i * 60))
        # lead=15min, now=0 → exhaust at t=600 < 15*60=900 → True
        assert predictor.should_preempt("a@x.com", lead_minutes=15, now=0.0)

    def test_outside_lead_window_returns_false(self, predictor):
        # exhaust at t=600, but lead=5min (300s) → 600 - 0 = 600 > 300 → False
        for i in range(5):
            predictor.record("a@x.com", 100.0 - i * 10.0, ts=float(i * 60))
        assert not predictor.should_preempt("a@x.com", lead_minutes=5, now=0.0)

    def test_unpredictable_returns_false(self, predictor):
        predictor.record("a@x.com", 90.0, ts=1.0)
        predictor.record("a@x.com", 90.0, ts=2.0)
        # <3 points: unpredictable
        assert not predictor.should_preempt("a@x.com", lead_minutes=999, now=0.0)

    def test_default_now_uses_walltime(self, predictor, monkeypatch):
        """now=None falls through to time.time()."""
        # Setup historical exhaust well in the past → guaranteed within any lead window.
        base = time.time()
        for i in range(5):
            predictor.record("a@x.com", 100.0 - i * 50.0, ts=base - 300 + i * 30)
        # slope is steep negative → predicted exhaust is in the past → should_preempt=True for any lead
        assert predictor.should_preempt("a@x.com", lead_minutes=1)


class TestFileFormat:
    def test_jsonl_schema_matches_prd(self, predictor):
        predictor.record("user@example.com", 50.0, t_remain=7200.0, ts=1_700_000_000.0)
        line = predictor.history_path.read_text(encoding="utf-8").strip()
        obj = json.loads(line)
        assert set(obj.keys()) >= {"email", "p_remain", "t_remain", "ts"}
        assert obj["email"] == "user@example.com"
        assert obj["p_remain"] == 50.0
        assert obj["t_remain"] == 7200.0
        assert obj["ts"] == 1_700_000_000.0
