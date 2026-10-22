#!/usr/bin/env python3
import json
import os

ledger_path = "paper_trade_ledger.json"
if not os.path.exists(ledger_path):
    print("No paper_trade_ledger.json found. Run a backtest first.")
    exit(1)

with open(ledger_path, "r", encoding="utf-8") as f:
    data = json.load(f)

trades = data.get("trades", [])
print(f"\n{'='*96}")
print(f" DETAILED BACKTEST REPORT: {len(trades)} Total Trades Recorded")
print(f" Gross Wallet: Rs.{data.get('virtual_balance_gross', 0):,.2f} | Net PnL: Rs.{data.get('net_profit_after_charges', 0):,.2f}")
print(f" Avg Win: Rs.{data.get('avg_winning_value_rs', 0):,.2f} | Avg Loss: Rs.{data.get('avg_losing_value_rs', 0):,.2f} | Ratio: {data.get('win_to_loss_ratio', 0):.2f}x")
print(f"{'='*96}")
print(f" {'TIMESTAMP':<18} {'CONTRACT':<14} {'ENTRY':>8} {'EXIT':>8} {'R-MULT':>7} {'GROSS PNL':>12} {'EXIT REASON'}")
print(f"{'='*96}")

# Show last 30 trades (or all if you prefer)
for t in trades[-30:]:
    pnl = float(t.get("gross_pnl", 0))
    color_prefix = "\033[92m" if pnl > 0 else "\033[91m"
    reset = "\033[0m"
    print(
        f" {str(t.get('timestamp','')):<18} "
        f"{str(t.get('index',''))} {str(t.get('contract','')):<10} "
        f"{float(t.get('entry_price',0)):>8.2f} "
        f"{float(t.get('exit_price',0)):>8.2f} "
        f"{color_prefix}{float(t.get('r_multiple',0)):>+6.2f}R{reset} "
        f"{color_prefix}Rs.{pnl:>+9,.2f}{reset} "
        f"{str(t.get('exit_reason',''))}"
    )
print(f"{'='*96}\n")