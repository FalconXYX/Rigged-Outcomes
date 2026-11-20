"""
Consolidate a market's raw trades (one row per fill) into positions: one row per (wallet, outcome, side), with
the binary market's complementary SELL folded into its BUY.

    python3 cleandata.py --input data/<Output>.csv --output data/<Output>_clean.csv

clean_rows() does the work in memory (add_market feeds it a market's fills as they come off the feed, with no
file); main() is the same over CSV files.
"""
import csv
import io
import os
from dataclasses import dataclass

COLUMNS = ["user_id", "eoa_address", "market_id", "market_title", "outcome", "side", "avg_odds", "contracts",
           "dollar_amount", "win_status", "first_trade", "last_trade", "num_trades"]

INPUT_CSV  = "data/USxIranStrikesFeb28.csv"
OUTPUT_CSV = "data/USxIranStrikesFeb28_clean.csv"


@dataclass
class Group:
    contracts:        float = 0.0
    dollar_amount:    float = 0.0
    odds_x_contracts: float = 0.0
    first:            str | None = None
    last:             str | None = None
    num:              int = 0
    meta:             dict[str, str] | None = None


def clean_rows(rows):
    """The positions (dicts with COLUMNS) of raw trade rows: an iterable of dicts with the raw CSV's columns, the
    numbers as numbers or as their text, consumed once and never held (a market's fills can be hundreds of
    thousands). Returns (positions, number of raw rows)."""
    all_outcomes: set[str] = set()
    groups: dict[tuple[str, str, str], Group] = {}
    n = 0
    for row in rows:
        n += 1
        all_outcomes.add(row["outcome"])
        key = (row["user_id"], row["outcome"], row["side"])
        g = groups.setdefault(key, Group())
        c = float(row["contracts"])
        g.contracts        += c
        g.dollar_amount    += float(row["dollar_amount"])
        g.odds_x_contracts += float(row["odds"]) * c
        ts = row["timestamp"]
        g.first = ts if g.first is None or ts < g.first else g.first
        g.last = ts if g.last is None or ts > g.last else g.last
        g.num += 1
        if g.meta is None:
            g.meta = {k: row.get(k, "") for k in ("eoa_address", "market_id", "market_title", "win_status")}

    # ── Build output rows ────────────────────────────────────────────────────
    clean = []
    for (user_id, outcome, side), g in groups.items():
        assert g.meta is not None
        clean.append({
            "user_id":       user_id,
            "eoa_address":   g.meta.get("eoa_address", ""),
            "market_id":     g.meta["market_id"],
            "market_title":  g.meta["market_title"],
            "outcome":       outcome,
            "side":          side,
            "avg_odds":      round(g.odds_x_contracts / g.contracts, 4) if g.contracts > 0 else 0,
            "contracts":     round(g.contracts, 4),
            "dollar_amount": round(g.dollar_amount, 4),
            "win_status":    g.meta["win_status"],
            "first_trade":   g.first,
            "last_trade":    g.last,
            "num_trades":    g.num,
        })

    # ── Merge complementary positions in binary markets ──────────────────────
    # In a 2-outcome market, (No, SELL) represents capital RECEIVED back, not
    # capital spent.  It is the economic complement of a (Yes, BUY).
    # Net cost of the Yes position = Yes_BUY_dollars − No_SELL_dollars.
    # We subtract the SELL's dollar_amount from its complement BUY row and
    # remove the standalone SELL row.
    if len(all_outcomes) == 2:
        out_a, out_b = sorted(all_outcomes)
        complement = {out_a: out_b, out_b: out_a}

        # Index clean rows by (user_id, outcome, side)
        idx: dict[tuple, int] = {
            (r["user_id"], r["outcome"], r["side"]): i
            for i, r in enumerate(clean)
        }
        to_remove: set[int] = set()

        for i, r in enumerate(clean):
            if r["side"] != "SELL" or i in to_remove:
                continue
            buy_key = (r["user_id"], complement[r["outcome"]], "BUY")
            if buy_key not in idx:
                continue  # no matching BUY — leave SELL row alone
            j = idx[buy_key]
            buy = clean[j]
            # Subtract SELL dollars from BUY net cost; keep contracts/odds on BUY side
            buy["dollar_amount"] = round(
                max(float(buy["dollar_amount"]) - float(r["dollar_amount"]), 0), 4
            )
            buy["num_trades"]  = int(buy["num_trades"]) + int(r["num_trades"])
            buy["first_trade"] = min(buy["first_trade"], r["first_trade"])
            buy["last_trade"]  = max(buy["last_trade"],  r["last_trade"])
            to_remove.add(i)

        clean = [r for i, r in enumerate(clean) if i not in to_remove]

    clean.sort(key=lambda r: r["first_trade"])
    return clean, n


def write(clean, f):
    """The positions as CSV into an open text file (newline=""), exactly as main() saves them."""
    writer = csv.DictWriter(f, fieldnames=clean[0].keys())
    writer.writeheader()
    writer.writerows(clean)


def to_text(clean):
    """The clean CSV's text, byte for byte what main() writes to its file."""
    buf = io.StringIO(newline="")
    write(clean, buf)
    return buf.getvalue()


def main(input_csv: str = INPUT_CSV, output_csv: str = OUTPUT_CSV) -> None:
    with open(input_csv, newline="", encoding="utf-8") as f:
        clean, n = clean_rows(csv.DictReader(f))

    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        write(clean, f)

    print(f"Input:  {n:,} raw trades")
    print(f"Output: {len(clean):,} consolidated user positions → {output_csv}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",  default=INPUT_CSV)
    ap.add_argument("--output", default=OUTPUT_CSV)
    args = ap.parse_args()
    main(input_csv=args.input, output_csv=args.output)
