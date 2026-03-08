#!/usr/bin/env python3
import json
import sys
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output

EASTERN = ZoneInfo("America/New_York")  # handles EST/EDT automatically

def visualize(keyword=None, port=5001):
    """Run the betting pattern visualization dashboard.
    
    Args:
        keyword: Market name/slug fragment to filter (e.g. 'usiran'). 
                 If None, uses first market from markets.json.
        port: Port to run Dash app on (default 5001).
    """
    with open("markets.json") as f:
        ALL_MARKETS = json.load(f)
    
    if keyword:
        keyword = keyword.lower()
        matches = [m for m in ALL_MARKETS
                   if keyword in m["name"].lower() or keyword in m["slug"].lower()]
        if not matches:
            raise ValueError(f"No market matched '{keyword}'.")
        m = matches[0]
    
    # ── 1. Load data ─────────────────────────────────────────────────────────
    df = pd.read_csv(f"data/{m['output']}_clean.csv")
    for col in ("first_trade", "last_trade"):
        df[col] = (pd.to_datetime(df[col], utc=True)
                     .dt.tz_convert(EASTERN)
                     .dt.tz_localize(None))
    market_title  = df["market_title"].iloc[0] if "market_title" in df.columns else m["name"]
    highlighted   = m.get("highlightedUsers", {})  # { address: label }

    # ── 2. Whale / crowd split ───────────────────────────────────────────────
    total_vol  = df["dollar_amount"].sum()
    threshold  = min(5000.0, 0.001 * total_vol)
    whale_mask = (df["win_status"] == "WIN") & (df["dollar_amount"] >= threshold)

    # Always include highlighted users in the whale set (they may have net $0 after
    # hedging but still hold a significant position sized by contracts)
    hi_mask    = df["user_id"].isin(highlighted)
    raw_whales = df[whale_mask | hi_mask].copy()
    crowd      = df[~(whale_mask | hi_mask)].copy()

    if len(raw_whales) >= 4:
        ts     = raw_whales["first_trade"].astype("int64") // 10**9
        q1, q3 = ts.quantile(0.25), ts.quantile(0.75)
        whales = raw_whales[ts >= q1 - 1.5 * (q3 - q1)].copy()
    else:
        whales = raw_whales.copy()

    # display_dollar: use dollar_amount normally, but for highlighted users whose
    # net cost is 0 (fully hedged), fall back to contracts * avg_odds as a size proxy
    whales["display_dollar"] = whales.apply(
        lambda r: float(r["contracts"]) * float(r["avg_odds"])
        if r["user_id"] in highlighted and float(r["dollar_amount"]) == 0
        else float(r["dollar_amount"]),
        axis=1,
    )

    crowd["hour"] = crowd["first_trade"].dt.floor("h")
    hourly        = crowd.groupby("hour")["dollar_amount"].sum().reset_index(name="vol")
    hourly["avg"] = hourly["vol"].rolling(6, min_periods=1, center=True).mean()

    # ── 3. Key events ────────────────────────────────────────────────────────
    events = {}
    for k, v in m.get("importantDates", {}).items():
        try:
            # JSON times are EST — trades are now also in EST, so use directly
            events[pd.to_datetime(k, format="%Y-%m-%d-%H:%M")] = v
        except Exception:
            pass

    # ── 4. Colours ───────────────────────────────────────────────────────────
    TEAL  = "#0f766e"
    BLUE  = "#1d4ed8"
    RED   = "#be123c"
    HIGH  = "#ea580c"   # orange — highlighted users
    GRAY  = "#e5e7eb"
    MUTED = "#9ca3af"
    TEXT  = "#374151"

    def base_layout(**extra):
        return dict(
            font=dict(family="Inter, 'Helvetica Neue', Arial, sans-serif", size=12, color=TEXT),
            plot_bgcolor="#ffffff",
            paper_bgcolor="#ffffff",
            margin=dict(l=72, r=52, t=28, b=52),
            xaxis=dict(
                showgrid=True, gridcolor=GRAY, zeroline=False,
                linecolor=GRAY, tickcolor=GRAY,
                tickfont=dict(size=11, color=MUTED),
            ),
            yaxis=dict(
                showgrid=True, gridcolor=GRAY, zeroline=False,
                linecolor=GRAY, tickcolor=GRAY,
                tickfont=dict(size=11, color=MUTED),
                tickprefix="$", tickformat=",.0f",
            ),
            showlegend=False,
            hovermode="closest",
            hoverlabel=dict(
                bgcolor="white", bordercolor=GRAY,
                font=dict(size=12, color=TEXT),
            ),
            **extra,
        )

    def add_events(fig):
        for dt, label in sorted(events.items()):
            fig.add_shape(
                type="line", x0=dt, x1=dt, y0=0, y1=1,
                xref="x", yref="paper",
                line=dict(color=RED, width=1.5, dash="dot"),
            )
            fig.add_annotation(
                x=dt, y=0.97, xref="x", yref="paper",
                text=label, textangle=-90,
                font=dict(size=10, color=RED),
                showarrow=False, xanchor="right",
                bgcolor="rgba(255,255,255,0.85)", borderpad=3,
            )

    # ── 5. Figures ───────────────────────────────────────────────────────────
    def _whale_traces(fig, df_w, yaxis=None):
        """Add whale scatter traces to fig — teal for normal, orange+label for highlighted."""
        if not len(df_w):
            return
        mx    = df_w["display_dollar"].max() or 1
        extra = dict(yaxis=yaxis) if yaxis else {}

        hi_mask  = df_w["user_id"].isin(highlighted)
        normal   = df_w[~hi_mask]
        hi_users = df_w[hi_mask]

        # Normal whales — teal dots
        if len(normal):
            sz = normal["display_dollar"].values
            fig.add_trace(go.Scatter(
                x=normal["first_trade"], y=normal["display_dollar"],
                mode="markers",
                marker=dict(
                    size=8 + 36 * np.sqrt(sz / mx),
                    color=TEAL, opacity=0.7,
                    line=dict(width=1, color="rgba(15,118,110,0.2)"),
                ),
                hovertemplate="<b>$%{y:,.0f}</b><br>%{x|%b %d · %H:%M}<extra></extra>",
                **extra,
            ))

        # Highlighted users — orange dots with name label above
        for _, row in hi_users.iterrows():
            label   = highlighted.get(row["user_id"], row["user_id"][:8])
            disp    = row["display_dollar"]
            sz      = 8 + 36 * np.sqrt(disp / mx)
            # Show contracts held in hover for hedged users (net dollar = 0)
            is_hedged = float(row["dollar_amount"]) == 0
            hover_extra = f"<br>{row['contracts']:,.0f} contracts" if is_hedged else ""
            fig.add_trace(go.Scatter(
                x=[row["first_trade"]], y=[disp],
                mode="markers+text",
                text=[label],
                textposition="top center",
                textfont=dict(size=11, color=HIGH, family="Inter, sans-serif"),
                marker=dict(
                    size=sz, color=HIGH, opacity=0.9,
                    line=dict(width=2, color="white"),
                ),
                hovertemplate=f"<b>{label}</b><br><b>$%{{y:,.0f}}</b>{hover_extra}<br>%{{x|%b %d · %H:%M}}<extra></extra>",
                **extra,
            ))


    def fig_whales():
        fig = go.Figure(layout=base_layout())
        if len(whales):
            _whale_traces(fig, whales)
            fig.add_hline(y=threshold, line_dash="dot", line_color=GRAY, line_width=1.5)
        add_events(fig)
        fig.update_layout(yaxis_title="Position size (USD)")
        return fig


    def fig_crowd():
        fig = go.Figure(layout=base_layout())
        if len(hourly):
            fig.add_trace(go.Scatter(
                x=hourly["hour"], y=hourly["vol"],
                mode="lines", line=dict(width=0),
                fill="tozeroy", fillcolor="rgba(29,78,216,0.06)",
                hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=hourly["hour"], y=hourly["avg"],
                mode="lines", line=dict(color=BLUE, width=2.5),
                hovertemplate="<b>$%{y:,.0f} / hr</b><br>%{x|%b %d · %H:%M}<extra></extra>",
            ))
        add_events(fig)
        fig.update_layout(yaxis_title="Volume per hour (USD)")
        return fig


    def fig_comparison():
        fig = go.Figure(layout=base_layout(
            yaxis2=dict(
                overlaying="y", side="right",
                showgrid=False, zeroline=False,
                tickfont=dict(size=11, color=TEAL),
                tickprefix="$", tickformat=",.0f",
                linecolor="rgba(0,0,0,0)", tickcolor="rgba(0,0,0,0)",
            ),
        ))
        if len(hourly):
            fig.add_trace(go.Scatter(
                x=hourly["hour"], y=hourly["avg"],
                mode="lines", line=dict(color=BLUE, width=2.5),
                fill="tozeroy", fillcolor="rgba(29,78,216,0.06)",
                hovertemplate="<b>Crowd $%{y:,.0f}/hr</b><br>%{x|%b %d · %H:%M}<extra></extra>",
            ))
        if len(whales):
            _whale_traces(fig, whales, yaxis="y2")
        add_events(fig)
        fig.update_layout(
            yaxis=dict(
                title="Crowd volume / hr",
                title_font=dict(color=BLUE),
                tickfont=dict(color=BLUE, size=11),
            ),
            yaxis2=dict(
                title="Whale position",
                title_font=dict(color=TEAL),
                tickfont=dict(color=TEAL, size=11),
            ),
        )
        return fig

    # ── 6. Dash app ───────────────────────────────────────────────────────────
    wpct = round(whales["dollar_amount"].sum() / total_vol * 100, 1) if total_vol else 0.0

    _T = dict(
        padding="10px 24px 12px",
        border="none",
        borderBottom="2.5px solid transparent",
        background="transparent",
        color="#6b7280",
        fontWeight="500",
        fontSize="0.84rem",
        fontFamily="Inter, system-ui, sans-serif",
        letterSpacing="0.01em",
        cursor="pointer",
    )
    _TS = {
        **_T,
        "color": BLUE,
        "borderBottom": "2.5px solid " + BLUE,
        "fontWeight": "600",
    }


    def stat_block(label, value, color="#111827"):
        return html.Div(
            style={"display": "flex", "flexDirection": "column", "gap": "3px"},
            children=[
                html.Span(label, style={
                    "fontSize": "0.62rem",
                    "fontWeight": "600",
                    "letterSpacing": "0.09em",
                    "textTransform": "uppercase",
                    "color": MUTED,
                }),
                html.Span(value, style={
                    "fontSize": "1.1rem",
                    "fontWeight": "700",
                    "color": color,
                }),
            ],
        )


    app = Dash(__name__, title="Betting Pattern Analysis")

    app.index_string = (
        "<!DOCTYPE html>"
        "<html><head>"
        "{%metas%}<title>{%title%}</title>{%favicon%}{%css%}"
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap' rel='stylesheet'>"
        "<style>"
        "*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0 }"
        "body { background: #f8fafc; font-family: Inter, system-ui, sans-serif; }"
        ".dash-tabs-content { border: none !important; }"
        "</style>"
        "</head><body>"
        "{%app_entry%}"
        "<footer>{%config%}{%scripts%}{%renderer%}</footer>"
        "</body></html>"
    )

    app.layout = html.Div(
        style={
            "minHeight": "100vh",
            "background": "#f8fafc",
            "fontFamily": "Inter, system-ui, sans-serif",
        },
        children=[
            # ── top bar ──────────────────────────────────────────────────────
            html.Div(
                style={
                    "background": "#ffffff",
                    "borderBottom": "1px solid #e5e7eb",
                    "padding": "28px 48px 0",
                },
                children=[
                    html.H1(
                        "Betting Pattern Analysis",
                        style={
                            "fontSize": "1.25rem",
                            "fontWeight": "700",
                            "color": "#0f172a",
                            "letterSpacing": "-0.02em",
                        },
                    ),
                    html.P(
                        market_title,
                        style={
                            "fontSize": "0.82rem",
                            "color": "#6b7280",
                            "marginTop": "4px",
                            "maxWidth": "700px",
                            "lineHeight": "1.5",
                        },
                    ),
                    # stats ───────────────────────────────────────────────────
                    html.Div(
                        style={
                            "display": "flex",
                            "gap": "44px",
                            "margin": "22px 0 0",
                            "flexWrap": "wrap",
                        },
                        children=[
                            stat_block("Total volume",    f"${total_vol:,.0f}"),
                            stat_block("Whale threshold", f"\u2265 ${threshold:,.0f}", TEAL),
                            stat_block("Whale bettors",   str(len(whales)),            TEAL),
                            stat_block("Whale share",     f"{wpct}%",                  TEAL),
                            stat_block("Crowd bettors",   f"{len(crowd):,}",           BLUE),
                        ],
                    ),
                    # events ──────────────────────────────────────────────────
                    html.Div(
                        style={
                            "display": "flex",
                            "gap": "24px",
                            "margin": "14px 0 0",
                            "flexWrap": "wrap",
                        },
                        children=[
                            html.Span(
                                f"{lbl}  \u00b7  {dt.strftime('%b %d, %H:%M')}",
                                style={"fontSize": "0.74rem", "color": RED},
                            )
                            for dt, lbl in sorted(events.items())
                        ],
                    ),
                    # tabs ────────────────────────────────────────────────────
                    dcc.Tabs(
                        id="tabs",
                        value="whales",
                        style={"marginTop": "20px", "border": "none"},
                        children=[
                            dcc.Tab(label="Whale Timing", value="whales",
                                    style=_T, selected_style=_TS),
                            dcc.Tab(label="Crowd Volume", value="crowd",
                                    style=_T, selected_style=_TS),
                            dcc.Tab(label="Comparison",   value="cmp",
                                    style=_T, selected_style=_TS),
                        ],
                    ),
                ],
            ),
            # ── chart ────────────────────────────────────────────────────────
            html.Div(
                style={"padding": "0 48px 52px", "background": "#f8fafc"},
                children=[
                    html.Div(
                        id="chart-content",
                        style={
                            "background": "#ffffff",
                            "borderRadius": "0 8px 8px 8px",
                            "overflow": "hidden",
                            "boxShadow": "0 1px 3px rgba(0,0,0,0.07)",
                        },
                    ),
                ],
            ),
            # ── footer ───────────────────────────────────────────────────────
            html.Div(
                "Polymarket \u00b7 Rigged Outcomes \u00b7 Data via Polymarket API",
                style={
                    "textAlign": "center",
                    "padding": "14px 48px",
                    "fontSize": "0.68rem",
                    "color": MUTED,
                    "borderTop": "1px solid #e5e7eb",
                    "background": "#ffffff",
                },
            ),
        ],
    )


    @app.callback(Output("chart-content", "children"), Input("tabs", "value"))
    def render_tab(tab):
        fig = {"whales": fig_whales, "crowd": fig_crowd, "cmp": fig_comparison}[tab]()
        return dcc.Graph(
            figure=fig,
            config={"displayModeBar": False},
            style={"height": "540px"},
        )

    print(f"\n  Market  : {market_title}")
    print(f"  Rows    : {len(df):,}")
    print(f"  Whales  : {len(whales)}  ({wpct}%,  threshold ${threshold:,.0f})")
    print(f"  Open    : http://127.0.0.1:{port}\n")
    app.run(port=port, debug=False)


if __name__ == "__main__":
    keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None
    visualize(keyword=keyword)
