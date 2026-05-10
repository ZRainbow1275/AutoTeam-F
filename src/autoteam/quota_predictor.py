"""Quota usage predictor — Round 12 S5.

Records per-account quota observations to a JSONL log and fits a simple
least-squares linear regression to estimate when each ACTIVE account's
``p_remain`` will hit zero. Used by :func:`autoteam.manager.cmd_rotate`
to pre-emptively swap soon-to-be-exhausted seats with STANDBY candidates
*before* the quota actually runs out — minimising user-visible "Team full
but no usable seat" windows.

Design choices
--------------
* **Storage** — append-only JSONL at ``PROJECT_ROOT / "quota_history.jsonl"``;
  no DB dependency, survives crashes, easy to tail / inspect / archive.
* **Algorithm** — minimum viable: ``p_remain = a * ts + b`` via NumPy-free
  closed-form OLS. We only need the *direction* and *zero-crossing*; full
  exponential-decay / piecewise-linear modelling is out of scope (see PRD
  Risk Notes — non-linear 5h reset stairs would distort it anyway, so we
  ship with ``PREDICTIVE_ENABLED=False`` by default).
* **Safety** — < 3 data points returns ``None`` (no prediction); positive
  slope (quota growing or flat) returns ``None``; any numeric exception is
  swallowed and returns ``None``. The caller treats ``None`` as "do not
  preempt", so the predictor can never *cause* an outage — only fail to
  prevent one.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_HISTORY_PATH = _PROJECT_ROOT / "quota_history.jsonl"


class QuotaPredictor:
    """Append-only JSONL store + linear-fit exhaust-time predictor."""

    def __init__(self, history_path: Path | None = None) -> None:
        self._history_path: Path = Path(history_path) if history_path else DEFAULT_HISTORY_PATH
        # File-level lock — record() and load_history() can be called from
        # the SSE pusher, cmd_check and cmd_rotate threads concurrently.
        self._lock = threading.Lock()

    @property
    def history_path(self) -> Path:
        return self._history_path

    # ------------------------------------------------------------------ write
    def record(
        self,
        email: str,
        p_remain: float,
        t_remain: float | None = None,
        ts: float | None = None,
    ) -> None:
        """Append one observation. ``ts`` defaults to ``time.time()``.

        ``p_remain`` is the "primary quota remaining percent" (0-100).
        ``t_remain`` is the optional "seconds until reset" — recorded for
        future modelling but unused by the linear fit today.
        """
        if not email:
            return
        try:
            row: dict[str, Any] = {
                "email": str(email),
                "p_remain": float(p_remain),
                "t_remain": None if t_remain is None else float(t_remain),
                "ts": float(ts) if ts is not None else time.time(),
            }
        except (TypeError, ValueError):
            logger.warning("quota_predictor.record: bad payload for %s", email)
            return

        line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._history_path, "a", encoding="utf-8") as fp:
                    fp.write(line)
                    # Round 12 wire-up — flush + fsync so a crash mid-write does
                    # not leave a half line (load_history skips JSONDecodeError
                    # lines, but predictor accuracy suffers from gaps).
                    try:
                        fp.flush()
                        import os
                        os.fsync(fp.fileno())
                    except OSError as exc:
                        logger.warning(
                            "quota_predictor.record: fsync failed for %s: %s",
                            self._history_path, exc,
                        )
            except OSError as exc:
                # Round 12 wire-up — downgrade exception to warning so a missing
                # disk does not crash the rotate hot path.
                logger.warning(
                    "quota_predictor.record: cannot append to %s: %s",
                    self._history_path, exc,
                )

    # ------------------------------------------------------------------- read
    def load_history(self, email: str, *, max_points: int = 50) -> list[dict[str, Any]]:
        """Return chronologically-sorted observations for ``email``.

        Truncated to the most recent ``max_points`` rows so long-running
        deployments do not blow up memory. Malformed lines are skipped
        silently — JSONL is append-only and a partial line is recoverable
        on the next successful append.
        """
        if not email or max_points <= 0:
            return []
        path = self._history_path
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            try:
                with open(path, encoding="utf-8") as fp:
                    for raw in fp:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("email") != email:
                            continue
                        try:
                            obj["ts"] = float(obj.get("ts", 0.0))
                            obj["p_remain"] = float(obj.get("p_remain", 0.0))
                        except (TypeError, ValueError):
                            continue
                        rows.append(obj)
            except OSError:
                logger.exception("quota_predictor.load_history: cannot read %s", path)
                return []
        rows.sort(key=lambda r: r["ts"])
        if len(rows) > max_points:
            rows = rows[-max_points:]
        return rows

    # ------------------------------------------------------------------- fit
    def predict_exhaust_time(self, email: str) -> float | None:
        """Predict the wall-clock ts at which ``p_remain`` crosses 0.

        Returns ``None`` when:
        * history < 3 points (insufficient signal);
        * slope >= 0 (quota stable or growing — nothing to preempt);
        * numeric instability (degenerate variance, NaN);
        * caught exception (best-effort — predictor never raises).
        """
        rows = self.load_history(email)
        if len(rows) < 3:
            return None

        try:
            xs = [r["ts"] for r in rows]
            ys = [r["p_remain"] for r in rows]
            n = len(xs)
            mean_x = sum(xs) / n
            mean_y = sum(ys) / n
            num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
            den = sum((xs[i] - mean_x) ** 2 for i in range(n))
            if den == 0:
                return None
            slope = num / den
            if slope >= 0:
                # quota constant or growing — no exhaust prediction.
                return None
            intercept = mean_y - slope * mean_x
            # solve 0 = slope * t + intercept → t = -intercept / slope
            t_exhaust = -intercept / slope
            if not (t_exhaust == t_exhaust):  # NaN check
                return None
            return float(t_exhaust)
        except (TypeError, ValueError, ZeroDivisionError):
            logger.exception("quota_predictor.predict_exhaust_time: fit failed for %s", email)
            return None

    # --------------------------------------------------------------- decision
    def should_preempt(
        self,
        email: str,
        lead_minutes: float,
        *,
        now: float | None = None,
    ) -> bool:
        """Return True when predicted exhaust is within ``lead_minutes`` of now."""
        t_exhaust = self.predict_exhaust_time(email)
        if t_exhaust is None:
            return False
        current = float(now) if now is not None else time.time()
        return (t_exhaust - current) < float(lead_minutes) * 60.0


# ---------------------------------------------------------------------------
# Module-level default predictor — production code uses this; tests inject
# their own QuotaPredictor instance with a tmp_path history_path fixture.
# ---------------------------------------------------------------------------
default_predictor = QuotaPredictor()


__all__ = [
    "DEFAULT_HISTORY_PATH",
    "QuotaPredictor",
    "default_predictor",
]
