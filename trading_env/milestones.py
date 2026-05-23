"""milestones.py — Trading performance milestones replacing gym badges.

Each milestone triggers a bootstrap bundle save (checkpoint + skills +
memory) so runs can be resumed or cross-session strategy transfer works
via --bootstrap-from, mirroring the Pokemon game-transfer mechanism.
"""
from __future__ import annotations
from typing import Any, Dict, List

# Ordered list mirrors ORDERED_PROGRESS_MILESTONES from pokemon_env/emulator.py
TRADING_MILESTONES_ORDER: List[str] = [
    "paper_account_active",          # First successful order submitted
    "first_profitable_session",       # Any day with realized_pnl_today > 0
    "five_trades_completed",          # Execution fluency check
    "win_rate_above_50pct_10trades",  # Early signal quality gate
    "sharpe_above_1_0",               # Risk-adjusted threshold
    "weekly_pnl_positive",            # 5-day rolling P&L > 0
    "drawdown_under_3pct_30days",     # Consistency gate
    "sharpe_above_1_5",               # Strong performance gate
    "25_trades_60pct_win_rate",       # Scale-up readiness
    "10k_cumulative_profit",          # Absolute P&L gate (paper)
]


def check_milestones(
    metrics: Dict[str, Any],
    history: List[Dict[str, Any]],
    already_achieved: set | None = None,
) -> List[str]:
    """Return list of newly achieved milestone names."""
    achieved = already_achieved or set()
    newly_achieved: List[str] = []

    def _check(name: str, condition: bool):
        if condition and name not in achieved:
            newly_achieved.append(name)

    total_trades = len(history)
    wins = sum(1 for t in history if t.get("realized_pnl", 0) > 0)
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    _check("paper_account_active", total_trades >= 1)
    _check("first_profitable_session", metrics.get("realized_pnl_today", 0) > 0)
    _check("five_trades_completed", total_trades >= 5)
    _check("win_rate_above_50pct_10trades", total_trades >= 10 and win_rate > 0.50)
    _check("sharpe_above_1_0", metrics.get("sharpe", 0) > 1.0)
    _check("weekly_pnl_positive", metrics.get("weekly_pnl", 0) > 0)
    _check("drawdown_under_3pct_30days",
           metrics.get("max_drawdown_30d", 1.0) < 0.03)
    _check("sharpe_above_1_5", metrics.get("sharpe", 0) > 1.5)
    _check("25_trades_60pct_win_rate", total_trades >= 25 and win_rate > 0.60)
    _check("10k_cumulative_profit", metrics.get("cumulative_pnl", 0) > 10000)

    return newly_achieved
