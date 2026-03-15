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

    all_outcomes = {r["outcome"] for r in rows}

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
            "eoa_address":   g.meta.get("eoa_address", ""),
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
