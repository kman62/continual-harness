"""AlpacaEmulator — drop-in replacement for EmeraldEmulator / RedEmulator.

Exposes the same get_state() / take_action() surface used by the
continual-harness agent loop so HarnessEvolver works with zero changes.

Requires env vars:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY

Set paper=True (default) to target Alpaca's paper trading endpoint.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    ClosePositionRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from trading_env.risk_guard import RiskGuard
from trading_env.market_state import (
    format_market_ascii,
    format_positions_text,
    format_account_summary,
)

log = logging.getLogger(__name__)


class AlpacaEmulator:
    """Wraps Alpaca paper/live account as a continual-harness environment."""

    DEFAULT_WATCHLIST = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "TSLA", "AMZN"]

    def __init__(
        self,
        watchlist: Optional[List[str]] = None,
        paper: bool = True,
        bar_timeframe: TimeFrame = TimeFrame.Minute,
        history_window_minutes: int = 60,
    ):
        api_key = os.environ["ALPACA_API_KEY"]
        secret_key = os.environ["ALPACA_SECRET_KEY"]

        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        self.watchlist = watchlist or self.DEFAULT_WATCHLIST
        self.bar_timeframe = bar_timeframe
        self.history_window_minutes = history_window_minutes
        self.step_count = 0
        self.risk = RiskGuard(self.trading)
        self._trade_history: List[Dict[str, Any]] = []

        log.info(
            "AlpacaEmulator ready | paper=%s | watchlist=%s | bars=%s",
            paper, self.watchlist, bar_timeframe,
        )

    # ------------------------------------------------------------------
    # State surface — mirrors get_game_state()
    # ------------------------------------------------------------------
    def get_state(self) -> Dict[str, Any]:
        """Return structured market state; shape mirrors PokeAgent state."""
        account = self.trading.get_account()
        positions = self.trading.get_all_positions()
        open_orders = self.trading.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )

        # Fetch recent bars for the full watchlist
        bars = self._fetch_bars()

        # Latest quotes per symbol
        latest_quotes: Dict[str, Dict[str, float]] = {}
        for sym in self.watchlist:
            try:
                q = self.data.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=sym)
                )
                latest_quotes[sym] = {
                    "bid": float(q[sym].bid_price),
                    "ask": float(q[sym].ask_price),
                }
            except Exception as e:
                log.warning("Quote fetch failed for %s: %s", sym, e)
                latest_quotes[sym] = {"bid": 0.0, "ask": 0.0}

        metrics = self._compute_metrics(account, positions, bars)

        return {
            # Standard continual-harness state keys
            "step": self.step_count,
            "location": "market_open" if self._is_market_open() else "market_closed",
            "player_position": {
                "cash": float(account.cash),
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
            },
            "state_text": format_positions_text(positions, open_orders, latest_quotes),
            "map_ascii": format_market_ascii(bars, self.watchlist),
            "account_summary": format_account_summary(account, metrics),
            "metrics": metrics,
            "risk_status": self.risk.snapshot(),
        }

    # ------------------------------------------------------------------
    # Action surface — mirrors press_buttons()
    # ------------------------------------------------------------------
    def take_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a trading action; shape mirrors the game's button press.

        Supported action types:
          {"type": "hold"}  — no-op
          {"type": "bracket_buy", "symbol": "SPY", "qty": 10,
           "stop_loss": 450.0, "take_profit": 470.0, "est_price": 460.0}
          {"type": "market_sell", "symbol": "SPY", "qty": 10}
          {"type": "close", "symbol": "SPY"}  — close entire position
        """
        # === HARD GATE: risk check before any SDK call ===
        allowed, reason = self.risk.allow(action)
        if not allowed:
            self.step_count += 1
            result = {"success": False, "error": f"risk_blocked:{reason}"}
            log.warning("Action blocked by RiskGuard: %s", reason)
            return result

        try:
            atype = action["type"]
            result: Dict[str, Any]

            if atype == "hold":
                result = {"success": True, "action": "hold"}

            elif atype == "close":
                sym = action["symbol"]
                self.trading.close_position(sym)
                result = {"success": True, "action": "closed", "symbol": sym}

            elif atype == "market_sell":
                order = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=action["symbol"],
                        qty=action["qty"],
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                result = {
                    "success": True,
                    "order_id": str(order.id),
                    "symbol": action["symbol"],
                    "side": "sell",
                    "qty": action["qty"],
                }

            elif atype == "bracket_buy":
                # Bracket order: entry + stop_loss + take_profit in one submission
                from alpaca.trading.requests import (
                    TakeProfitRequest,
                    StopLossRequest,
                )
                order = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=action["symbol"],
                        qty=action["qty"],
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                        order_class=OrderClass.BRACKET,
                        take_profit=TakeProfitRequest(
                            limit_price=float(action["take_profit"])
                        ),
                        stop_loss=StopLossRequest(
                            stop_price=float(action["stop_loss"])
                        ),
                    )
                )
                result = {
                    "success": True,
                    "order_id": str(order.id),
                    "symbol": action["symbol"],
                    "side": "buy",
                    "qty": action["qty"],
                    "stop_loss": action["stop_loss"],
                    "take_profit": action["take_profit"],
                }

            else:
                result = {"success": False, "error": f"unknown_action_type:{atype}"}

        except Exception as e:
            log.error("Order submission error: %s", e, exc_info=True)
            result = {"success": False, "error": str(e)}

        self.step_count += 1
        self.risk.record_outcome(action, result)

        if result.get("success") and atype != "hold":
            self._trade_history.append({
                "step": self.step_count,
                "action": action,
                "result": result,
                "timestamp": datetime.utcnow().isoformat(),
            })

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _fetch_bars(self) -> Any:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=self.watchlist,
                timeframe=self.bar_timeframe,
                start=datetime.utcnow() - timedelta(minutes=self.history_window_minutes),
            )
            return self.data.get_stock_bars(req).df
        except Exception as e:
            log.warning("Bar fetch failed: %s", e)
            import pandas as pd
            return pd.DataFrame()

    def _is_market_open(self) -> bool:
        try:
            return self.trading.get_clock().is_open
        except Exception:
            return False

    def _compute_metrics(self, account, positions, bars) -> Dict[str, float]:
        unrealized = sum(float(p.unrealized_pl) for p in positions)
        realized_today = float(account.equity) - float(account.last_equity)
        total_trades = len(self._trade_history)
        wins = sum(
            1 for t in self._trade_history
            if t.get("result", {}).get("realized_pnl", 0) > 0
        )
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        return {
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl_today": round(realized_today, 2),
            "cumulative_pnl": sum(
                t.get("result", {}).get("realized_pnl", 0)
                for t in self._trade_history
            ),
            "position_count": len(positions),
            "total_trades": total_trades,
            "win_rate": round(win_rate, 4),
            "drawdown_from_peak": round(self.risk.current_drawdown(), 4),
            "consecutive_losses": self.risk._loss_streak,
            "sharpe": 0.0,  # computed externally after 30+ trades
            "weekly_pnl": 0.0,  # computed externally over 5-day window
        }

    def get_trade_history(self) -> List[Dict[str, Any]]:
        return list(self._trade_history)

    def reset_session(self):
        """Call at market open each day to reset intraday circuit breakers."""
        self.risk.reset_for_new_session()
        log.info("AlpacaEmulator session reset for new trading day.")
