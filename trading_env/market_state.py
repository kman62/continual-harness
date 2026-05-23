"""market_state.py — Format Alpaca data into LLM-readable state.

Mirrors the role of utils/mapping/map_formatter.py in the original harness.
"""
from __future__ import annotations
from typing import Any, Dict, List
import pandas as pd


def format_market_ascii(bars: pd.DataFrame, symbols: List[str]) -> str:
    """Compact ASCII price table — replaces the game's ASCII tile map."""
    if bars is None or bars.empty:
        return "(no bar data available)"

    header = f"{'SYM':6}| {'LAST':>8} | {'OPEN':>8} | {'HIGH':>8} | {'LOW':>8} | {'VOL':>12} | {'CHG%':>7}"
    sep = "-" * len(header)
    lines = [header, sep]

    for sym in symbols:
        try:
            if isinstance(bars.index, pd.MultiIndex):
                sym_bars = bars.xs(sym, level=0)
            else:
                sym_bars = bars[bars.index == sym]

            if sym_bars.empty:
                lines.append(f"{sym:6}| (no data)")
                continue

            last = sym_bars.iloc[-1]
            first = sym_bars.iloc[0]
            chg_pct = ((last["close"] - first["open"]) / first["open"]) * 100
            chg_sign = "+" if chg_pct >= 0 else ""
            lines.append(
                f"{sym:6}| {last['close']:>8.2f} | {last['open']:>8.2f} | "
                f"{last['high']:>8.2f} | {last['low']:>8.2f} | "
                f"{int(last['volume']):>12,} | {chg_sign}{chg_pct:>6.2f}%"
            )
        except (KeyError, IndexError):
            lines.append(f"{sym:6}| (data error)")

    return "\n".join(lines)


def format_positions_text(
    positions: List[Any],
    open_orders: List[Any],
    latest_quotes: Dict[str, Dict[str, float]],
) -> str:
    """Structured text of current portfolio state for LLM context."""
    sections: List[str] = []

    # --- Positions ---
    sections.append("## Open Positions")
    if not positions:
        sections.append("  (none)")
    else:
        for p in positions:
            unr = float(p.unrealized_pl)
            unr_pct = float(p.unrealized_plpc) * 100
            sign = "+" if unr >= 0 else ""
            sections.append(
                f"  {p.symbol:6} qty={p.qty:>6}  "
                f"avg={float(p.avg_entry_price):>8.2f}  "
                f"current={float(p.current_price):>8.2f}  "
                f"unrealized={sign}{unr:>8.2f} ({sign}{unr_pct:.2f}%)"
            )

    # --- Open orders ---
    sections.append("\n## Pending Orders")
    if not open_orders:
        sections.append("  (none)")
    else:
        for o in open_orders:
            sections.append(
                f"  {o.symbol:6} {str(o.side):4} qty={o.qty:>6} "
                f"type={str(o.order_type):10} status={o.status}"
            )

    # --- Quotes ---
    sections.append("\n## Latest Quotes")
    for sym, q in latest_quotes.items():
        mid = (q["bid"] + q["ask"]) / 2
        spread = q["ask"] - q["bid"]
        sections.append(
            f"  {sym:6} bid={q['bid']:>8.2f}  ask={q['ask']:>8.2f}  "
            f"mid={mid:>8.2f}  spread={spread:.4f}"
        )

    return "\n".join(sections)


def format_account_summary(account, metrics: Dict[str, Any]) -> str:
    """One-line account health for LLM header."""
    return (
        f"Portfolio: ${float(account.portfolio_value):,.2f}  "
        f"Cash: ${float(account.cash):,.2f}  "
        f"Buying Power: ${float(account.buying_power):,.2f}  "
        f"Today P&L: ${metrics.get('realized_pnl_today', 0):+,.2f}  "
        f"Unrealized: ${metrics.get('unrealized_pnl', 0):+,.2f}  "
        f"Drawdown: {metrics.get('drawdown_from_peak', 0)*100:.2f}%"
    )
