"""RiskGuard — hard circuit-breakers enforced at the emulator layer.

These rules are NEVER editable by HarnessEvolver. They run before every
order submission so no evolved prompt or skill can bypass them.
"""
from __future__ import annotations
from collections import deque
from datetime import datetime
from typing import Any, Dict, Tuple


class RiskGuard:
    # ================================================================
    # HARD LIMITS — DO NOT EXPOSE TO LLM OR HARNESS EVOLVER
    # ================================================================
    MAX_POSITION_PCT: float = 0.10        # No single buy > 10% of equity
    MAX_DAILY_DRAWDOWN_PCT: float = 0.03  # Halt session at -3% drawdown
    MAX_CONSECUTIVE_LOSSES: int = 3       # Halt after 3 losing trades in a row
    MAX_ORDERS_PER_HOUR: int = 30         # Rate limiter
    REQUIRE_BRACKET_ORDERS: bool = True   # Every buy must carry stop_loss

    def __init__(self, trading_client):
        self.trading = trading_client
        account = trading_client.get_account()
        self.peak_equity: float = float(account.equity)
        self.consecutive_losses: int = 0
        self._recent_orders: deque = deque(maxlen=200)
        self.halted_until_reset: bool = False
        self._loss_streak: int = 0

    # ------------------------------------------------------------------
    # Primary gate — call before every order
    # ------------------------------------------------------------------
    def allow(self, action: Dict[str, Any]) -> Tuple[bool, str]:
        """Return (True, 'ok') to proceed or (False, reason) to block."""
        if self.halted_until_reset:
            return False, "session_halted:reset_required"

        atype = action.get("type", "hold")
        if atype == "hold":
            return True, "ok"

        # --- Drawdown circuit breaker ---
        drawdown = self.current_drawdown()
        if drawdown >= self.MAX_DAILY_DRAWDOWN_PCT:
            self.halted_until_reset = True
            return False, f"daily_drawdown_breach:{drawdown:.2%}"

        # --- Consecutive loss circuit breaker ---
        if self._loss_streak >= self.MAX_CONSECUTIVE_LOSSES:
            self.halted_until_reset = True
            return False, f"consecutive_loss_breach:{self._loss_streak}"

        # --- Rate limiter ---
        now = datetime.utcnow()
        recent = [t for t in self._recent_orders
                  if (now - t).total_seconds() < 3600]
        if len(recent) >= self.MAX_ORDERS_PER_HOUR:
            return False, f"rate_limit:{self.MAX_ORDERS_PER_HOUR}_per_hour"

        # --- Buy-specific guards ---
        if atype in ("market_buy", "bracket_buy"):
            # Enforce bracket requirement
            if self.REQUIRE_BRACKET_ORDERS and atype != "bracket_buy":
                return False, "bracket_required:use_bracket_buy_with_stop_loss"

            # Position sizing check
            try:
                account = self.trading.get_account()
                equity = float(account.equity)
                est_price = action.get("est_price", 0)
                qty = action.get("qty", 0)
                if est_price > 0 and equity > 0:
                    est_cost = qty * est_price
                    if est_cost / equity > self.MAX_POSITION_PCT:
                        return False, (
                            f"position_too_large:{est_cost/equity:.1%}"
                            f"_exceeds_{self.MAX_POSITION_PCT:.0%}_limit"
                        )
            except Exception:
                pass  # Don't block on API errors during the guard check

        return True, "ok"

    # ------------------------------------------------------------------
    # Outcome tracking — call after every completed action
    # ------------------------------------------------------------------
    def record_outcome(self, action: Dict[str, Any], result: Dict[str, Any]):
        atype = action.get("type", "hold")
        if atype == "hold":
            return
        if result.get("success"):
            self._recent_orders.append(datetime.utcnow())

        # Track realized P&L for loss streak
        pnl = result.get("realized_pnl", None)
        if pnl is not None:
            if pnl < 0:
                self._loss_streak += 1
            else:
                self._loss_streak = 0  # Reset on any profit

        # Keep peak equity current
        try:
            equity = float(self.trading.get_account().equity)
            if equity > self.peak_equity:
                self.peak_equity = equity
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def current_drawdown(self) -> float:
        try:
            equity = float(self.trading.get_account().equity)
            return max(0.0, (self.peak_equity - equity) / self.peak_equity)
        except Exception:
            return 0.0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "halted": self.halted_until_reset,
            "drawdown_pct": round(self.current_drawdown() * 100, 3),
            "loss_streak": self._loss_streak,
            "peak_equity": self.peak_equity,
            "limits": {
                "max_drawdown_pct": self.MAX_DAILY_DRAWDOWN_PCT * 100,
                "max_consecutive_losses": self.MAX_CONSECUTIVE_LOSSES,
                "max_orders_per_hour": self.MAX_ORDERS_PER_HOUR,
            },
        }

    def reset_for_new_session(self):
        """Call at market open each day to reset session-level circuit breakers."""
        self.halted_until_reset = False
        self._loss_streak = 0
        self._recent_orders.clear()
        try:
            self.peak_equity = float(self.trading.get_account().equity)
        except Exception:
            pass
