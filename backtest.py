#!/usr/bin/env python3
"""
Backtest der LSOB-Entry-Signale ueber die komplette verfuegbare History
(gleiche Engine wie lsob_check.py). Pro Box wird nur der erste Retest als
Trade gewertet -- exakt wie das Alert-Verhalten. Entry = Schlusskurs der
Retest-Kerze, SL = abgewandte Boxseite, Ziele bei 1R und 2R.

Nutzung:
  python backtest.py                  # alle Assets x alle Timeframes
  python backtest.py --asset BTC      # nur ein Asset (Label aus ASSETS)
  python backtest.py --tf 4h          # nur ein Timeframe
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from lsob_check import (ASSETS, TIMEFRAMES, MIN_CANDLES, fetch_klines,
                        run_engine, evaluate_trade)


def backtest_one(asset, tf):
    candles = fetch_klines(asset, tf)
    if len(candles) < MIN_CANDLES:
        return None
    events, _ = run_engine(candles)
    taken = set()
    outcomes = {"tp2": 0, "tp1": 0, "sl": 0, "open": 0}
    for e in events:
        if not e["type"].endswith("_entry"):
            continue
        box_id = e["box"]["id"]
        if box_id in taken:
            continue
        taken.add(box_id)
        entry = candles[e["bar"]]["close"]
        sl = e["box"]["bottom"] if e["direction"] == "long" else e["box"]["top"]
        outcome = evaluate_trade(candles[e["bar"] + 1:], e["direction"], entry, sl)
        if outcome in outcomes:
            outcomes[outcome] += 1
    return outcomes


def main():
    ap = argparse.ArgumentParser(description="LSOB-Signal-Backtest")
    ap.add_argument("--asset", help="nur dieses Asset-Label (z.B. BTC)")
    ap.add_argument("--tf", choices=TIMEFRAMES, help="nur dieser Timeframe")
    args = ap.parse_args()

    assets = [a for a in ASSETS if not args.asset or a["label"].lower() == args.asset.lower()]
    tfs = [args.tf] if args.tf else TIMEFRAMES
    if not assets:
        raise SystemExit(f"Unbekanntes Asset-Label: {args.asset}")

    jobs = [(a, tf) for a in assets for tf in tfs]
    rows = []
    total = {"tp2": 0, "tp1": 0, "sl": 0, "open": 0}

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(backtest_one, a, tf): (a["label"], tf) for a, tf in jobs}
        for fut in as_completed(futures):
            label, tf = futures[fut]
            try:
                outcomes = fut.result()
            except Exception as e:
                print(f"Fehler bei {label} {tf}: {e}")
                continue
            if outcomes is None:
                continue
            rows.append((label, tf, outcomes))
            for k in total:
                total[k] += outcomes[k]

    rows.sort(key=lambda r: (r[0], TIMEFRAMES.index(r[1])))

    def fmt_row(label, tf, o):
        closed = o["tp2"] + o["tp1"] + o["sl"]
        trades = closed + o["open"]
        winrate = f"{100 * (o['tp2'] + o['tp1']) / closed:5.1f}%" if closed else "    -"
        return (f"{label:<10} {tf:<4} {trades:>6} {o['tp2']:>4} {o['tp1']:>4} "
                f"{o['sl']:>4} {o['open']:>5} {winrate:>7}")

    print(f"{'Asset':<10} {'TF':<4} {'Trades':>6} {'2R':>4} {'1R':>4} {'SL':>4} {'offen':>5} {'>=1R':>7}")
    print("-" * 50)
    for label, tf, o in rows:
        print(fmt_row(label, tf, o))
    print("-" * 50)
    print(fmt_row("GESAMT", "", total))


if __name__ == "__main__":
    main()
