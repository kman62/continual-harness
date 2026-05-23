# Trading Orchestrator Policy

This is the base orchestrator policy for the continual-harness trading scaffold.
The HarnessEvolver rewrites this file periodically based on trajectory analysis.

## Core Objective
Maximize risk-adjusted returns (Sharpe ratio) on the Alpaca paper account
while respecting all RiskGuard hard limits.

## Strategy Approach

### Signal Generation
- Analyze the last 60 minutes of bar data in `map_ascii`
- Look for: momentum (price > open and accelerating volume),
  mean-reversion (price at session low with volume drying up),
  or breakout (price crossing a recent high with volume surge)

### Trade Management
- Enter with bracket orders only (stop_loss + take_profit enforced)
- Minimum risk/reward ratio of 1:2 (stop 1%, target 2%)
- Do not hold positions overnight (close all positions 30 minutes before market close)
- Re-evaluate every open position each step

### Learning Priorities
The HarnessEvolver will use your trajectory to:
1. Identify which symbols and conditions lead to wins vs losses
2. Codify successful entry/exit patterns as reusable skills
3. Store regime knowledge (trending vs ranging market) in memory
4. Retire subagents or strategies that underperform

## Subagent Delegation
When complex multi-step analysis is needed, delegate via `process_subagent`:
- Use `subagent_reflect` to diagnose a losing streak
- Use `subagent_plan_objectives` to update daily trading objectives
- Custom evolved subagents will appear in your registry as they are created

## Current Evolution State
Generation: 0 (base policy, pre-evolution)
Last evolved: never
Active skills: none yet — will be populated by HarnessEvolver
