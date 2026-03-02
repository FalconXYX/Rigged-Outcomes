import json
import sys

import cleandata
import polymarket_trades

MARKETS_FILE = "markets.json"

with open(MARKETS_FILE) as f:
    markets = json.load(f)

# Optional keyword filter: python3 run.py iran
keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None
if keyword:
    markets = [m for m in markets if keyword in m["name"].lower() or keyword in m["slug"].lower()]

if not markets:
    print(f"No markets matched '{keyword}'." if keyword else "markets.json is empty.")
    sys.exit(1)

for m in markets:
    raw   = f"data/{m['output']}.csv"
    clean = f"data/{m['output']}_clean.csv"

    print(f"\n── {m['name']} ──")
    polymarket_trades.main(slug=m["slug"], output=raw)
    cleandata.main(input_csv=raw, output_csv=clean)
    print(f"✓ {raw}\n✓ {clean}")

print(f"\nDone — processed {len(markets)} market(s).")
