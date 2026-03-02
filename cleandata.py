import csv
import os
from dataclasses import dataclass, field

INPUT_CSV  = "data/USxIranStrikesFeb28.csv"
OUTPUT_CSV = "data/USxIranStrikesFeb28_clean.csv"

@dataclass
class Group:
    contracts:        float = 0.0
    dollar_amount:    float = 0.0
    odds_x_contracts: float = 0.0
    timestamps:       list[str] = field(default_factory=list)
    meta:             dict[str, str] | None = None

def main(input_csv: str = INPUT_CSV, output_csv: str = OUTPUT_CSV) -> None:
    # ── Load ─────────────────────────────────────────────────────────────────
    with open(input_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    groups: dict[tuple[str, str, str], Group] = {}
    for row in rows:
        key = (row["user_id"], row["outcome"], row["side"])
        g = groups.setdefault(key, Group())
        c = float(row["contracts"])
        g.contracts        += c
        g.dollar_amount    += float(row["dollar_amount"])
        g.odds_x_contracts += float(row["odds"]) * c
        g.timestamps.append(row["timestamp"])
        if g.meta is None:
            g.meta = row

    # ── Build output rows ────────────────────────────────────────────────────
    clean = []
    for (user_id, outcome, side), g in groups.items():
        assert g.meta is not None
        clean.append({
            "user_id":       user_id,
            "market_id":     g.meta["market_id"],
            "market_title":  g.meta["market_title"],
            "outcome":       outcome,
            "side":          side,
            "avg_odds":      round(g.odds_x_contracts / g.contracts, 4) if g.contracts > 0 else 0,
            "contracts":     round(g.contracts, 4),
            "dollar_amount": round(g.dollar_amount, 4),
            "win_status":    g.meta["win_status"],
            "first_trade":   min(g.timestamps),
            "last_trade":    max(g.timestamps),
            "num_trades":    len(g.timestamps),
        })

    clean.sort(key=lambda r: r["first_trade"])

    # ── Save ─────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=clean[0].keys())
        writer.writeheader()
        writer.writerows(clean)

    print(f"Input:  {len(rows):,} raw trades")
    print(f"Output: {len(clean):,} consolidated user positions → {output_csv}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",  default=INPUT_CSV)
    ap.add_argument("--output", default=OUTPUT_CSV)
    args = ap.parse_args()
    main(input_csv=args.input, output_csv=args.output)
