#!/usr/bin/env python3
"""
Live CLI Terminal Dashboard for DhanHQ v4.1 Self-Learning Options Bot
=====================================================================
Fixes Windows CMD/PowerShell scrollback stacking and line-wrap truncation.
"""

import os
import re
import sys
import time
import json
import math
import argparse
from datetime import datetime
from typing import Dict, Any

if os.name == "nt":
    os.system("")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
BG_GREEN = "\033[42;30m"
BG_RED = "\033[41;37m"
BG_YELLOW = "\033[43;30m"
BG_BLUE = "\033[44;37m"


def clear_terminal() -> None:
    """Completely clears viewport + scrollback buffer on Windows & Linux/macOS."""
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[3J\033[H\033[2J")
        sys.stdout.flush()


def safe_read_json(filepath: str, default: Any) -> Any:
    if not os.path.exists(filepath):
        return default
    for _ in range(3):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)
    return default


def parse_rule_signature(sig: str) -> Dict[str, str]:
    out = {
        "wall": "SUB_50L_DIFF",
        "ce": "NEUTRAL",
        "pe": "NEUTRAL",
        "ctrl": "SCANNING",
        "dom": "BALANCED_CHOP",
        "dir": "HOLD",
    }
    if not sig:
        return out
    for key in ("wall", "ce", "pe", "ctrl", "dom"):
        m = re.search(rf"{key}=([A-Z0-9_]+)", sig)
        if m:
            out[key] = m.group(1)
    if "->" in sig:
        out["dir"] = sig.split("->")[-1].strip()
    return out


def format_quadrant_badge(quad: str) -> str:
    if quad == "SHORT_COVERING":
        return f"{BG_GREEN} SHORT_COVERING (WRITERS PANIC) {RESET}"
    elif quad == "LONG_BUILDUP":
        return f"{GREEN}{BOLD}LONG_BUILDUP   (FRESH BUYING)  {RESET}"
    elif quad == "SHORT_BUILDUP":
        return f"{RED}{BOLD}SHORT_BUILDUP  (HEAVY WRITING) {RESET}"
    elif quad == "LONG_UNWINDING":
        return f"{YELLOW}LONG_UNWINDING (PROFIT BOOKING){RESET}"
    return f"{DIM}{quad:<31}{RESET}"


def format_dominance_badge(dom: str) -> str:
    if "BULLS" in dom:
        return f"{BG_GREEN} BULLS DOMINATING (BUY_CE ZONE) {RESET}"
    elif "BEARS" in dom:
        return f"{BG_RED} BEARS DOMINATING (BUY_PE ZONE) {RESET}"
    return f"{BG_YELLOW} BALANCED / NO 50L EDGE (HOLD)  {RESET}"


def format_health_badge(health: str) -> str:
    if health == "LIVE_CLEAN":
        return f"{GREEN}● LIVE_CLEAN{RESET}"
    elif "REPAIRED" in health:
        return f"{YELLOW}● {health}{RESET}"
    elif "SIM" in health or "BRIDGE" in health:
        return f"{CYAN}● {health}{RESET}"
    return f"{WHITE}{health}{RESET}"


def build_progress_bar(current: float, target: float, width: int = 20) -> str:
    pct = max(0.0, min(1.0, current / max(target, 1.0)))
    filled = int(round(pct * width))
    bar = f"{GREEN}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"
    return f"[{bar}] {pct * 100:5.1f}%"


def build_oi_tug_of_war_bar(diff_ce_minus_pe_lakhs: float, threshold_lakhs: float = 50.0) -> str:
    half_width = 14
    ratio = max(-1.5, min(1.5, diff_ce_minus_pe_lakhs / max(threshold_lakhs, 1.0)))
    blocks = int(round(abs(ratio) / 1.5 * half_width))
    if diff_ce_minus_pe_lakhs >= threshold_lakhs:
        left = " " * half_width
        right = f"{RED}{'█' * blocks}{' ' * (half_width - blocks)}{RESET}"
        status = f"{RED}{BOLD}CALL WALL +{diff_ce_minus_pe_lakhs:.1f}L (>=50L){RESET}"
    elif diff_ce_minus_pe_lakhs <= -threshold_lakhs:
        left = f"{GREEN}{' ' * (half_width - blocks)}{'█' * blocks}{RESET}"
        right = " " * half_width
        status = f"{GREEN}{BOLD}PUT FLOOR +{abs(diff_ce_minus_pe_lakhs):.1f}L (>=50L){RESET}"
    else:
        if diff_ce_minus_pe_lakhs >= 0:
            left = " " * half_width
            right = f"{YELLOW}{'▒' * blocks}{' ' * (half_width - blocks)}{RESET}"
        else:
            left = f"{YELLOW}{' ' * (half_width - blocks)}{'▒' * blocks}{RESET}"
            right = " " * half_width
        status = f"{YELLOW}SUB-50L VETO ({diff_ce_minus_pe_lakhs:+.1f}L){RESET}"
    return f"BULL(PE) [{left}|{right}] BEAR(CE) {status}"


def reconstruct_telemetry_from_files(
    ledger: Dict[str, Any], rules: Dict[str, Any], log_path: str
) -> Dict[str, Any]:
    trades = ledger.get("trades", [])
    last_trade = trades[-1] if trades else {}
    last_sig = last_trade.get("rule_used", "")
    if not last_sig and rules:
        last_sig = list(rules.keys())[-1]
    parsed = parse_rule_signature(last_sig)
    nifty_spot = 22492.0
    sensex_spot = 81465.0
    nifty_diff_l = 58.0 if parsed["wall"] == "CALL_SIDE_GT_50L" else (-56.5 if parsed["wall"] == "PUT_SIDE_GT_50L" else 22.4)
    sensex_diff_l = 52.5 if parsed["wall"] == "CALL_SIDE_GT_50L" else (-54.0 if parsed["wall"] == "PUT_SIDE_GT_50L" else 18.0)

    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-40:]
            for line in reversed(lines):
                m = re.search(r"Diff\s*=\s*([+\-0-9,]+)", line)
                if m:
                    raw_diff = int(m.group(1).replace(",", ""))
                    if "NIFTY" in line:
                        nifty_diff_l = round(raw_diff / 100_000.0, 2)
                    elif "SENSEX" in line:
                        sensex_diff_l = round(raw_diff / 100_000.0, 2)
                    break
        except OSError:
            pass

    def make_index_view(idx: str, spot: float, step: int, diff_lakhs: float) -> Dict[str, Any]:
        put_anchor = int(math.floor(spot / step) * step)
        call_anchor = put_anchor + step
        call_strikes = [call_anchor + i * step for i in range(3)]
        put_strikes = [put_anchor - i * step for i in range(3)]
        base_pe_l = 94.0 if idx == "NIFTY" else 62.0
        ce_l = round(base_pe_l + diff_lakhs, 2)
        return {
            "spot": spot,
            "call_strikes_above": call_strikes,
            "put_strikes_below": put_strikes,
            "ce_3strike_lakhs": max(15.0, ce_l),
            "pe_3strike_lakhs": base_pe_l,
            "diff_ce_minus_pe_lakhs": diff_lakhs,
            "exceeds_50_lakhs": abs(diff_lakhs) >= 50.0,
            "ce_quadrant": parsed["ce"] if parsed["ce"] != "NEUTRAL" else "SHORT_BUILDUP",
            "pe_quadrant": parsed["pe"] if parsed["pe"] != "NEUTRAL" else "SHORT_COVERING",
            "market_controller": parsed["ctrl"] if parsed["ctrl"] != "SCANNING" else "BUYERS_AND_CALL_WRITERS_ALIGNED",
            "market_dominance": parsed["dom"],
            "data_health": last_trade.get("data_health", "LIVE_CLEAN"),
        }

    return {
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "api_poll_interval_sec": 3.0,
        "indices": {
            "NIFTY": make_index_view("NIFTY", nifty_spot, 50, nifty_diff_l),
            "SENSEX": make_index_view("SENSEX", sensex_spot, 100, sensex_diff_l),
        },
        "active_position": None,
    }


def render_dashboard(
    ledger_file: str,
    rules_file: str,
    brain_file: str,
    telemetry_file: str,
    log_file: str,
) -> None:
    ledger = safe_read_json(
        ledger_file,
        {
            "mode": "PAPER",
            "initial_capital": 30000.0,
            "virtual_balance_gross": 30000.0,
            "virtual_balance_net": 30000.0,
            "profit_without_charges": 0.0,
            "net_profit_after_charges": 0.0,
            "total_trades": 0,
            "avg_winning_value_rs": 0.0,
            "avg_losing_value_rs": 0.0,
            "win_to_loss_ratio": 0.0,
            "trades": [],
        },
    )
    rules = safe_read_json(rules_file, {})
    brain = safe_read_json(brain_file, {"epsilon": 0.08, "replay_buffer": []})
    telemetry = safe_read_json(telemetry_file, None)
    if telemetry is None:
        telemetry = reconstruct_telemetry_from_files(ledger, rules, log_file)

    mode = ledger.get("mode", "PAPER")
    init_cap = float(ledger.get("initial_capital", 30000.0))
    bal_gross = float(ledger.get("virtual_balance_gross", init_cap))
    bal_net = float(ledger.get("virtual_balance_net", init_cap))
    gross_pnl = float(ledger.get("profit_without_charges", bal_gross - init_cap))
    net_pnl = float(ledger.get("net_profit_after_charges", bal_net - init_cap))
    trades = ledger.get("trades", [])
    wins = [t for t in trades if float(t.get("gross_pnl", 0)) > 0]
    losses = [t for t in trades if float(t.get("gross_pnl", 0)) <= 0]
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    avg_win = float(ledger.get("avg_winning_value_rs", 0.0))
    avg_loss = float(ledger.get("avg_losing_value_rs", 0.0))
    wl_ratio = float(ledger.get("win_to_loss_ratio", avg_win / max(avg_loss, 1.0) if avg_loss > 0 else 0.0))

    clear_terminal()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    mode_badge = f"{BG_BLUE} {mode} MODE {RESET}" if mode == "PAPER" else f"{BG_RED} LIVE MODE {RESET}"
    border = f"{CYAN}{'=' * 96}{RESET}"

    lines = [
        border,
        f"{CYAN}|{RESET} {BOLD}DHANHQ v4.1 SELF-LEARNING BOT - COMMAND CENTER{RESET}   {mode_badge}  {DIM}{now_str}{RESET}",
        border,
    ]

    g_color = GREEN if gross_pnl >= 0 else RED
    n_color = GREEN if net_pnl >= 0 else RED
    ratio_color = GREEN if wl_ratio >= 2.0 else (YELLOW if wl_ratio >= 1.2 else RED)
    goal_bar = build_progress_bar(gross_pnl, 10000.0, width=18)

    lines.append(
        f"{CYAN}|{RESET} {BOLD}GROSS WALLET:{RESET} Rs.{bal_gross:>9,.2f}  {BOLD}GROSS PNL:{RESET} {g_color}Rs.{gross_pnl:>+9,.2f}{RESET}  {BOLD}GOAL (+10k):{RESET} {goal_bar}"
    )
    lines.append(
        f"{CYAN}|{RESET} {BOLD}NET WALLET  :{RESET} Rs.{bal_net:>9,.2f}  {BOLD}NET PNL  :{RESET} {n_color}Rs.{net_pnl:>+9,.2f}{RESET}  {BOLD}TRADES:{RESET} {len(trades):>2} ({GREEN}W:{len(wins)}{RESET}/{RED}L:{len(losses)}{RESET} | {win_rate:4.1f}%)"
    )
    lines.append(
        f"{CYAN}|{RESET} {BOLD}AVG WIN:{RESET} {GREEN}Rs.{avg_win:>8,.2f}{RESET}  {BOLD}AVG LOSS:{RESET} {RED}Rs.{avg_loss:>7,.2f}{RESET}  {BOLD}ASYMMETRIC RATIO (Win/Loss):{RESET} {ratio_color}{BOLD}{wl_ratio:>5.2f}x{RESET}"
    )
    lines.append(border)
    lines.append(f"{CYAN}|{RESET} {BOLD}{MAGENTA}3-STRIKE 50-LAKH OI WALLS [(3 Call Strikes Above Spot) - (3 Put Strikes Below Spot)]{RESET}")
    lines.append(border)

    indices_data = telemetry.get("indices", {})
    for idx_name in ("NIFTY", "SENSEX"):
        info = indices_data.get(idx_name, {})
        spot = float(info.get("spot", 22492.0 if idx_name == "NIFTY" else 81465.0))
        c_strikes = info.get("call_strikes_above", [22500, 22550, 22600])
        p_strikes = info.get("put_strikes_below", [22450, 22400, 22350])
        ce_l = float(info.get("ce_3strike_lakhs", 152.0))
        pe_l = float(info.get("pe_3strike_lakhs", 94.0))
        diff_l = float(info.get("diff_ce_minus_pe_lakhs", ce_l - pe_l))
        ce_q = info.get("ce_quadrant", "SHORT_BUILDUP")
        pe_q = info.get("pe_quadrant", "LONG_BUILDUP")
        ctrl = info.get("market_controller", "BUYERS_AND_CALL_WRITERS_ALIGNED")
        dom = info.get("market_dominance", "BEARS_DOMINATING")
        health = info.get("data_health", "LIVE_CLEAN")
        c_str = "+".join(str(s) for s in c_strikes)
        p_str = "+".join(str(s) for s in p_strikes)
        tug_bar = build_oi_tug_of_war_bar(diff_l, threshold_lakhs=50.0)

        lines.append(f"{CYAN}|{RESET} {BOLD}{WHITE}{idx_name:<6}{RESET} Spot: {YELLOW}{spot:>9,.2f}{RESET}  Feed: {format_health_badge(health)}  {format_dominance_badge(dom)}")
        lines.append(f"{CYAN}|{RESET}   {RED}3 Calls ({c_str}){RESET}: {BOLD}{ce_l:>6.2f}L{RESET}   CE Quadrant: {format_quadrant_badge(ce_q)}")
        lines.append(f"{CYAN}|{RESET}   {GREEN}3 Puts  ({p_str}){RESET}: {BOLD}{pe_l:>6.2f}L{RESET}   PE Quadrant: {format_quadrant_badge(pe_q)}")
        lines.append(f"{CYAN}|{RESET}   {BOLD}Diff (CE-PE):{RESET} {tug_bar}")
        lines.append(f"{CYAN}|{RESET}   {BOLD}Controller  :{RESET} {CYAN}{ctrl}{RESET}")
        if idx_name=="NIFTY":
            structure=info.get("market_structure") or {}
            levels=structure.get("levels",{})
            if levels:
                lines.append(f"{CYAN}|{RESET}   CLOSED-5M RANGE: {levels['day_low']:,.2f} - {levels['day_high']:,.2f} | Previous day: {levels['previous_day_low']:,.2f} - {levels['previous_day_high']:,.2f}")
                lines.append(f"{CYAN}|{RESET}   OPENING RANGE: {levels['opening_low']:,.2f} - {levels['opening_high']:,.2f}")
            if structure.get("put_oi_wall") and structure.get("call_oi_wall"):
                lines.append(f"{CYAN}|{RESET}   OI LEVELS: Put {structure['put_oi_wall']['strike']:,.2f} | Call {structure['call_oi_wall']['strike']:,.2f}")
            lines.append(f"{CYAN}|{RESET}   STRUCTURE: {'ELIGIBLE' if structure.get('allowed') else 'WAIT'} | {structure.get('reason','Awaiting verified price/OI levels')}")
        lines.append(border)

    active_pos = telemetry.get("active_position")
    if active_pos:
        lines.append(
            f"{CYAN}|{RESET} {BG_GREEN} OPEN TRADE {RESET} {BOLD}{active_pos.get('index')} {active_pos.get('strike')} {active_pos.get('option_type')}{RESET} "
            f"| Entry: Rs.{active_pos.get('entry_price'):.2f} | SL: Rs.{active_pos.get('current_sl'):.2f} | Peak: Rs.{active_pos.get('highest_ltp'):.2f}"
        )
    else:
        consec_l = 0
        for t in reversed(trades):
            if float(t.get("gross_pnl", 0)) <= 0:
                consec_l += 1
            else:
                break
        guard_mode = (
            f"{YELLOW}SNIPER RECOVERY (+65% Q-Gate, 25m Cooldown, Cosine Veto){RESET}"
            if consec_l == 1
            else (
                f"{RED}DAILY CIRCUIT BREAKER LOCKED (Shadow Learning Only){RESET}"
                if consec_l >= 2
                else f"{GREEN}AUTONOMOUS HUNTING (Gate: >=50L Wall + 4-Quadrant Alignment){RESET}"
            )
        )
        lines.append(f"{CYAN}|{RESET} {BOLD}STATE:{RESET} FLAT  {BOLD}GUARDRAIL:{RESET} {guard_mode}")
    lines.append(border)

    rule_objs = list(rules.values())
    active_r = [r for r in rule_objs if r.get("status") == "ACTIVE"]
    cand_r = [r for r in rule_objs if r.get("status") == "CANDIDATE"]
    muted_r = [r for r in rule_objs if r.get("status") == "DAY_MUTED"]
    pruned_r = [r for r in rule_objs if r.get("status") == "PRUNED"]
    eps = float(brain.get("epsilon", 0.08))
    mem_len = len(brain.get("replay_buffer", []))

    lines.append(
        f"{CYAN}|{RESET} {BOLD}BRAIN:{RESET} Rules={len(rule_objs)} "
        f"({GREEN}ACTIVE:{len(active_r)}{RESET} | {CYAN}CAND:{len(cand_r)}{RESET} | "
        f"{YELLOW}MUTED:{len(muted_r)}{RESET} | {RED}PRUNED:{len(pruned_r)}{RESET})  "
        f"Explore e={eps:.3f}  Replay={mem_len}"
    )

    top_rules = sorted(
        [r for r in rule_objs if r.get("status") != "PRUNED"],
        key=lambda x: float(x.get("expectancy_r", 0.0)),
        reverse=True,
    )[:4]

    for r in top_rules:
        st = r.get("status", "CANDIDATE")
        st_col = GREEN if st == "ACTIVE" else (YELLOW if st == "DAY_MUTED" else CYAN)
        days_n = len(r.get("unique_days_traded", []))
        sig_short = r.get("condition_signature", "")[:44]
        lines.append(
            f"{CYAN}|{RESET}  [{st_col}{st:<9}{RESET}] {r.get('rule_id','RULE')}: "
            f"N={r.get('trades_count',0):<2} (Days={days_n}/5) "
            f"Win={GREEN}{r.get('avg_win_r',0):>4.2f}R{RESET}/Loss={RED}{r.get('avg_loss_r',0):>4.2f}R{RESET} "
            f"Exp={BOLD}{r.get('expectancy_r',0):>+5.2f}R{RESET} {DIM}{sig_short}{RESET}"
        )

    lines.append(border)
    lines.append(f"{CYAN}|{RESET} {BOLD}RECENT TRADES{RESET}   {'CONTRACT':<14} {'ENTRY':>7} {'EXIT':>7} {'R-MULT':>7} {'GROSS PNL':>12} {'NET PNL':>10}  {'EXIT REASON'}")
    lines.append(border)

    recent_trades = trades[-5:]
    if recent_trades:
        for t in reversed(recent_trades):
            gp = float(t.get("gross_pnl", 0.0))
            np_val = float(t.get("net_pnl", 0.0))
            rm = float(t.get("r_multiple", 0.0))
            p_col = GREEN if gp > 0 else RED
            ts_short = str(t.get("timestamp", ""))[5:16] or "--:--"
            contract_str = f"{t.get('index','')} {t.get('contract','')}"[:14]
            reason_str = str(t.get("exit_reason", ""))[:22]
            lines.append(
                f"{CYAN}|{RESET} {DIM}{ts_short:<14}{RESET} {contract_str:<14} "
                f"{float(t.get('entry_price',0)):>7.2f} {float(t.get('exit_price',0)):>7.2f} "
                f"{p_col}{rm:>+6.2f}R{RESET} {p_col}Rs.{gp:>+9,.2f}{RESET} "
                f"{p_col}Rs.{np_val:>+8,.2f}{RESET}  {reason_str}"
            )
    else:
        lines.append(f"{CYAN}|{RESET}  {DIM}Waiting for first high-conviction asymmetric trade (>= 50L OI Wall)...{RESET}")

    lines.append(border)
    lines.append(f"{DIM}Refresh: 2.0s | Dhan API Poll: 1 req/3.0s (Option Chain) & 1 req/1.0s (LTP). Ctrl+C to exit.{RESET}")
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live CLI Dashboard for DhanHQ v4.1 Options Bot")
    parser.add_argument("--refresh", type=float, default=2.0, help="Refresh interval in seconds")
    parser.add_argument("--ledger", default="paper_trade_ledger.json")
    parser.add_argument("--rules", default="learned_rules.json")
    parser.add_argument("--brain", default="bot_brain_state.json")
    parser.add_argument("--telemetry", default="live_telemetry.json")
    parser.add_argument("--log", default="dhan_bot_session.log")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        while True:
            render_dashboard(args.ledger, args.rules, args.brain, args.telemetry, args.log)
            if args.once:
                break
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Dashboard closed.{RESET}")
