import json
import sys
import pandas as pd
import plotly.graph_objects as go

MARKETS_FILE = "markets.json"

with open(MARKETS_FILE) as f:
    markets = json.load(f)

keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None
if keyword:
    markets = [m for m in markets if keyword in m["name"].lower()]
if not markets:
    print(f"No market matched '{keyword}'." if keyword else "markets.json is empty.")
    sys.exit(1)

m = markets[0]
df = pd.read_csv(f"data/{m['output']}.csv", parse_dates=["timestamp"])
df["hour"] = df["timestamp"].dt.floor("h")
vol = df.groupby(["hour", "outcome"])["dollar_amount"].sum().reset_index()

COLORS       = {"Yes": "#22c55e",  "No": "#ef4444"}
TREND_COLORS = {"Yes": "#facc15",  "No": "#a78bfa"}  # yellow / purple
fig = go.Figure()
for outcome in ["Yes", "No"]:
    if outcome not in df["outcome"].values:
        continue
    sub   = vol[vol["outcome"] == outcome].copy()
    color = COLORS.get(outcome, "#888")
    tcolor = TREND_COLORS.get(outcome, "#fff")

    # volume line
    fig.add_trace(go.Scatter(x=sub["hour"], y=sub["dollar_amount"],
                             name=outcome, mode="lines",
                             line=dict(color=color, width=2)))

    # rolling 24h average — hidden by default, shown only in single-outcome view
    trend = sub["dollar_amount"].rolling(24, min_periods=1, center=True).mean()
    fig.add_trace(go.Scatter(x=sub["hour"], y=trend,
                             name=f"{outcome} trend", mode="lines",
                             visible=False,
                             line=dict(color=tcolor, width=2)))

fig.update_layout(
    title=df["market_title"].iloc[0],
    xaxis_title="Time",
    yaxis_title="Volume (USD)",
    template="plotly_dark",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    updatemenus=[dict(
        type="buttons",
        direction="right",
        x=0.0, y=1.15,
        xanchor="left",
        buttons=[
            dict(label="Both", method="update", args=[{"visible": [True,  False, True,  False]}]),
            dict(label="Yes",  method="update", args=[{"visible": [True,  True,  False, False]}]),
            dict(label="No",   method="update", args=[{"visible": [False, False, True,  True]}]),
        ],
    )],
)
fig.show()

