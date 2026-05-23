#!/usr/bin/env python3
"""run_trading.py — Phase 1 entrypoint for Alpaca paper trading.

Wires together:
  - AlpacaEmulator (trading_env/) as the environment
  - continualharness scaffold + HarnessEvolver for self-improving strategy
  - Gemini 3.5 Flash as the primary VLM backend
  - Neon/file-backed stores for memory, skills, subagents

Usage:
  export ALPACA_API_KEY=PK...
  export ALPACA_SECRET_KEY=...
  export GEMINI_API_KEY=...

  python run_trading.py \\
    --backend gemini \\
    --model-name gemini-3-5-flash-preview \\
    --enable-prompt-optimization \\
    --optimization-window-length 30 \\
    --bar-interval-seconds 60 \\
    --max-steps 5000
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_trading")

# Ensure repo root is on path (same pattern as run.py)
sys.path.insert(0, str(Path(__file__).parent))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Alpaca paper trading via continual-harness + Gemini 3.5 Flash"
    )
    p.add_argument("--backend", default="gemini",
                   choices=["gemini", "openai", "anthropic", "openrouter", "vertex"],
                   help="VLM backend (default: gemini)")
    p.add_argument("--model-name", default="gemini-3-5-flash-preview",
                   help="Model name for backend")
    p.add_argument("--enable-prompt-optimization", action="store_true",
                   help="Activate HarnessEvolver evolution loop")
    p.add_argument("--optimization-window-length", type=int, default=30,
                   help="Trajectory steps per evolution window")
    p.add_argument("--bar-interval-seconds", type=int, default=60,
                   help="Seconds between steps (align with bar timeframe)")
    p.add_argument("--max-steps", type=int, default=5000,
                   help="Total agent steps before exit")
    p.add_argument("--bootstrap-from", default=None,
                   help="Path to prior run bootstrap dir to load skills/memory")
    p.add_argument("--watchlist", nargs="+",
                   default=["SPY", "QQQ", "AAPL", "NVDA", "MSFT"],
                   help="Symbols to track")
    p.add_argument("--paper", action="store_true", default=True,
                   help="Use Alpaca paper trading endpoint (default: True)")
    p.add_argument("--run-name", default="trading",
                   help="Optional run name suffix for cache dir")
    p.add_argument("--dry-run", action="store_true",
                   help="Print state each step without submitting orders")
    return p.parse_args()


def build_agent_prompt(state: Dict[str, Any], system_prompt: str) -> str:
    """Compose the full prompt the VLM sees each step."""
    return f"""{system_prompt}

---
## Current State (Step {state['step']})

{state['account_summary']}

### Market Data
```
{state['map_ascii']}
```

### Portfolio
{state['state_text']}

### Risk Status
{json.dumps(state['risk_status'], indent=2)}

---
Emit your action as a single JSON object on the last line of your response.
"""


def parse_action_from_response(response: str) -> Dict[str, Any]:
    """Extract JSON action from VLM response."""
    import re
    # Try last line first
    lines = [l.strip() for l in response.strip().splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "type" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    # Fallback: find any JSON object with "type" key
    matches = re.findall(r"\{[^{}]+\}", response)
    for m in reversed(matches):
        try:
            obj = json.loads(m)
            if "type" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    log.warning("Could not parse action from response, defaulting to hold")
    return {"type": "hold"}


def load_bootstrap(bootstrap_path: str, evolver) -> None:
    """Hydrate skills/memory/subagents from a prior run's bootstrap dir."""
    path = Path(bootstrap_path)
    if not path.exists():
        log.warning("Bootstrap path not found: %s", bootstrap_path)
        return

    for store_name, loader in [
        ("skills.json", evolver._get_skill_store),
        ("memory.json", evolver._get_memory_store),
        ("subagents.json", evolver._get_subagent_store),
    ]:
        store_file = path / store_name
        if store_file.exists():
            try:
                store = loader()
                with open(store_file) as f:
                    entries = json.load(f)
                log.info("Bootstrap: loaded %d entries from %s", len(entries), store_name)
            except Exception as e:
                log.error("Bootstrap load failed for %s: %s", store_name, e)

    evolved_prompt = path / "evolved_prompt.md"
    if evolved_prompt.exists():
        try:
            with open(evolved_prompt) as f:
                evolver.prompt_optimizer._current_prompt = f.read()
            log.info("Bootstrap: loaded evolved orchestrator prompt")
        except Exception as e:
            log.error("Bootstrap prompt load failed: %s", e)


def main():
    args = parse_args()

    # Validate required env vars early
    missing = [v for v in ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"] if not os.environ.get(v)]
    if missing:
        log.error("Missing required env vars: %s", missing)
        sys.exit(1)

    if not os.environ.get(f"{args.backend.upper()}_API_KEY") and args.backend == "gemini":
        if not os.environ.get("GOOGLE_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
            log.error("Missing GEMINI_API_KEY or GOOGLE_API_KEY for gemini backend")
            sys.exit(1)

    # ----------------------------------------------------------------
    # Initialize core components
    # ----------------------------------------------------------------
    from trading_env.alpaca_emulator import AlpacaEmulator
    from trading_env.milestones import check_milestones

    emulator = AlpacaEmulator(
        watchlist=args.watchlist,
        paper=args.paper,
    )

    # VLM backend — same factory as run.py
    from utils.agent_infrastructure.vlm_backends import create_vlm_backend
    vlm = create_vlm_backend(args.backend, args.model_name)
    log.info("VLM backend: %s / %s", args.backend, args.model_name)

    # Run data manager for trajectory persistence
    from utils.data_persistence.run_data_manager import RunDataManager
    run_manager = RunDataManager(run_name=args.run_name)

    # HarnessEvolver wired to trading prompts
    from agents.utils.harness_evolver import create_harness_evolver
    evolver = create_harness_evolver(
        vlm=vlm,
        run_data_manager=run_manager,
        base_prompt_path="agents/prompts/trading-directives/trading_orchestrator_policy.md",
        system_prompt_path="agents/prompts/trading-directives/trading_system_prompt.md",
    )

    if args.bootstrap_from:
        load_bootstrap(args.bootstrap_from, evolver)

    # ----------------------------------------------------------------
    # Main agent loop
    # ----------------------------------------------------------------
    log.info("Starting trading loop | max_steps=%d | paper=%s", args.max_steps, args.paper)
    achieved_milestones: set = set()
    trajectory: List[Dict[str, Any]] = []

    for step in range(args.max_steps):
        state = emulator.get_state()

        # Wait out closed market
        if state["location"] == "market_closed":
            log.info("Step %d: market closed, sleeping 60s", step)
            time.sleep(60)
            continue

        # Build prompt and get VLM decision
        system_prompt = evolver.get_current_prompt()
        full_prompt = build_agent_prompt(state, system_prompt)

        try:
            response = vlm.get_text_query(full_prompt, context_id=f"trade_step_{step}")
        except Exception as e:
            log.error("VLM query failed at step %d: %s", step, e)
            time.sleep(5)
            continue

        action = parse_action_from_response(response)
        log.info("Step %d | action=%s", step, action)

        # Execute (or dry-run)
        if args.dry_run:
            result = {"success": True, "action": "dry_run", "would_execute": action}
            log.info("DRY RUN — would execute: %s", action)
        else:
            result = emulator.take_action(action)

        log.info("Step %d | result=%s", step, result)

        # Record trajectory for HarnessEvolver
        traj_entry = {
            "step": step,
            "pre_state": {
                "location": state["location"],
                "player_position": state["player_position"],
                "metrics": state["metrics"],
            },
            "action": {
                "tool_calls": [{
                    "name": "take_action",
                    "args": action,
                    "result": result,
                }]
            },
            "reasoning": response,
        }
        trajectory.append(traj_entry)
        run_manager.record_trajectory(step, traj_entry)

        # Milestone check + bootstrap save
        new_milestones = check_milestones(
            state["metrics"],
            emulator.get_trade_history(),
            already_achieved=achieved_milestones,
        )
        for milestone in new_milestones:
            achieved_milestones.add(milestone)
            log.info("🏆 Milestone achieved: %s", milestone)
            try:
                run_manager.create_bootstrap_bundle(milestone=milestone)
            except Exception as e:
                log.warning("Bootstrap save failed: %s", e)

        # HarnessEvolver — adaptive schedule (every 25 early, every 100 stable)
        if args.enable_prompt_optimization and evolver.should_evolve(
            step, args.optimization_window_length
        ):
            log.info("=== HarnessEvolver firing at step %d ===", step)
            try:
                evolution_results = evolver.evolve(
                    current_step=step,
                    num_trajectory_steps=args.optimization_window_length,
                )
                log.info("Evolution results: %s", evolution_results)
            except Exception as e:
                log.error("Evolution failed at step %d: %s", step, e)

        # Pace to bar cadence
        time.sleep(args.bar_interval_seconds)

    log.info("Trading loop complete after %d steps", args.max_steps)


if __name__ == "__main__":
    main()
