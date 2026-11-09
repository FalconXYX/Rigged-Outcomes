"""
Fetch + clean every market in markets.json (or those matching a keyword):

    python3 run.py          # every market
    python3 run.py iran     # markets whose name/slug contains "iran"

Each market: polymarket_trades.main (Data API v2 -> data/<Output>.csv) then
cleandata.main (-> data/<Output>_clean.csv). Importing this module does
nothing; the pipeline runs only from main(), so `import run` cannot overwrite
the committed CSVs.
"""
import json
import sys

import cleandata
import polymarket_trades

MARKETS_FILE = "markets.json"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    with open(MARKETS_FILE) as f:
        markets = json.load(f)

    # Optional keyword filter: python3 run.py iran
    keyword = argv[0].lower() if argv else None
    if keyword:
        markets = [m for m in markets if keyword in m["name"].lower() or keyword in m["slug"].lower()]

    if not markets:
        print(f"No markets matched '{keyword}'." if keyword else "markets.json is empty.")
        return 1

    for m in markets:
        raw   = f"data/{m['output']}.csv"
        clean = f"data/{m['output']}_clean.csv"

        print(f"\n── {m['name']} ──")
        polymarket_trades.main(slug=m["slug"], output=raw)
        cleandata.main(input_csv=raw, output_csv=clean)
        print(f"✓ {raw}\n✓ {clean}")

    print(f"\nDone — processed {len(markets)} market(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
