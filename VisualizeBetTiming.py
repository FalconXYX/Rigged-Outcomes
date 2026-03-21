#!/usr/bin/env python3
import json
import sys
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output
import dash_mantine_components as dmc
from template_VisualizeBetTiming import material_layout, stat_block

EASTERN = ZoneInfo("America/New_York")  # handles EST/EDT automatically

def visualize(keyword=None, port=5001):
 
    with open("markets.json") as f:
        ALL_MARKETS = json.load(f)
    
    if keyword:
        keyword = keyword.lower()
        matches = [m for m in ALL_MARKETS
                   if keyword in m["name"].lower() or keyword in m["slug"].lower()]
        if not matches:
            raise ValueError(f"No market matched '{keyword}'.")
        m = matches[0]
    else:
        m = ALL_MARKETS[0]
    
    # -- 1. Load data ---------------------------------------------------------
    df = pd.read_csv(f"data/{m['output']}_clean.csv")
    for col in ("first_trade", "last_trade"):
        df[col] = (pd.to_datetime(df[col], utc=True)
                     .dt.tz_convert(EASTERN)
                     .dt.tz_localize(None))
    market_title  = df["market_title"].iloc[0] if "market_title" in df.columns else m["name"]
    highlighted   = m.get("highlightedUsers", {})  # { address: label }

    # Build a normalized market-odds series from trade rows.
    # For "No" outcome trades, flip p -> (1 - p) so everything is event probability.
    ODDS_ROLLING_WINDOW = 12
    odds_line = pd.DataFrame(columns=["odds_time", "odds_pct"])
    if "avg_odds" in df.columns:
        p = pd.to_numeric(df["avg_odds"], errors="coerce")
        if p.max(skipna=True) and p.max(skipna=True) > 1.0:
            p = p / 100.0

        p = p.clip(lower=0.0, upper=1.0)
        if "outcome" in df.columns:
            out = df["outcome"].astype(str).str.strip().str.lower()
            is_no = out.eq("no") | out.str.startswith("no ")
            p = pd.Series(np.where(is_no, 1.0 - p, p), index=df.index)

        tmp = pd.DataFrame({
            "odds_time": df["first_trade"].dt.floor("min"),
            "odds_pct": p * 100.0,
        }).dropna(subset=["odds_time", "odds_pct"])

        if len(tmp):
            odds_line = (
                tmp.groupby("odds_time")["odds_pct"]
                .median()
                .reset_index()
                .sort_values("odds_time")
            )
            odds_line["odds_pct"] = (
                odds_line["odds_pct"]
                .rolling(window=ODDS_ROLLING_WINDOW, min_periods=1, center=True)
                .median()
            )

    # -- 2. Whale / crowd split -----------------------------------------------
    total_vol  = df["dollar_amount"].sum()
    threshold  = min(5000.0, 0.001 * total_vol)
    if threshold < 1000:
        threshold = 1000.0
        
    correct_outcome = m.get("correct_outcome")
    if correct_outcome and "outcome" in df.columns:
        winner_mask = df["outcome"].astype(str).str.lower() == correct_outcome.lower()
    else:
        winner_mask = df["win_status"] == "WIN"
        
    df["is_winner"] = winner_mask
    whale_mask = df["is_winner"] & (df["dollar_amount"] >= threshold)

    # Always include highlighted users in the whale set (they may have net $0 after
    # hedging but still hold a significant position sized by contracts)
    hi_mask    = df["user_id"].isin(highlighted)
    raw_whales = df[whale_mask | hi_mask].copy()
    crowd      = df[~(whale_mask | hi_mask)].copy()

    if len(raw_whales) >= 4:
        ts     = raw_whales["first_trade"].astype("int64") // 10**9
        q1, q3 = ts.quantile(0.25), ts.quantile(0.75)
        whales = raw_whales[ts >= q1 - 1.5 * (q3 - q1)].copy() # type: ignore
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

    def get_net_profit(row):
        stake = float(row["dollar_amount"])
        # Prefer using contracts as it's the literal payout on Polymarket ($1 per contract)
        if "contracts" in row and pd.notnull(row["contracts"]):
            payout = float(row["contracts"])
        else:
            implied_odds = float(row["avg_odds"])
            if implied_odds > 1:
                implied_odds = implied_odds / 100.0
            if implied_odds <= 0.001: # Prevent division by zero or extreme ballooning
                implied_odds = 0.001
            payout = stake / implied_odds
            
        # Return Net Profit rather than Gross Payout
        return max(0.0, payout - stake)

    # Calculate net profit for whales (0 if they lost)
    whales["win_amount"] = whales.apply(
        lambda r: get_net_profit(r) if r.get("is_winner", True) else 0.0,
        axis=1,
    )
    
    # Calculate total market net profit to correctly denominate the percentage
    df_winners = df[df["is_winner"]]
    total_market_winnings = df_winners.apply(get_net_profit, axis=1).sum()

    crowd["hour"] = crowd["first_trade"].dt.floor("h")
    hourly        = crowd.groupby("hour")["dollar_amount"].sum().reset_index(name="vol")
    hourly["avg"] = hourly["vol"].rolling(6, min_periods=1, center=True).mean()

    # -- 3. Key events --------------------------------------------------------
    events = {}
    for k, v in m.get("importantDates", {}).items():
        try:
            # JSON times are EST - trades are now also in EST, so use directly
            events[pd.to_datetime(k, format="%Y-%m-%d-%H:%M")] = v
        except Exception:
            pass

    time_ranges = {}
    for label, spec in m.get("timeRanges", {}).items():
        try:
            time_ranges[label] = {
                "start": pd.to_datetime(spec["start"], format="%Y-%m-%d-%H:%M"),
                "end": pd.to_datetime(spec["end"], format="%Y-%m-%d-%H:%M"),
            }
        except Exception:
            continue

    default_range = "Total" if "Total" in time_ranges else (next(iter(time_ranges), None))

    # -- 4. Colours -----------------------------------------------------------
    JET_BLACK   = "#223843"
    PLATINUM    = "#eff1f3"
    DUST_GREY   = "#dbd3d8"
    DESERT_SAND = "#d8b4a0"
    BURNT_PEACH = "#d77a61"
    
    TEAL  = "#0f766e"
    BLUE  = "#1d4ed8"
    RED   = "#be123c"
    HIGH  = "#ea580c"   # orange - highlighted users
    
    GRAY  = DUST_GREY
    MUTED = "#9ca3af"
    TEXT  = JET_BLACK

    def base_layout(**extra):
        return dict(
            font=dict(family="Roboto, 'Helvetica Neue', Arial, sans-serif", size=13, color=JET_BLACK),
            plot_bgcolor=PLATINUM,
            paper_bgcolor=PLATINUM,
            margin=dict(l=52, r=48, t=30, b=52),
            xaxis=dict(
                showgrid=True, gridcolor=DUST_GREY, zeroline=False,
                linecolor=DUST_GREY, tickcolor=DUST_GREY,
                tickfont=dict(size=12, color=JET_BLACK),
            ),
            yaxis=dict(
                showgrid=True, gridcolor=DUST_GREY, zeroline=False,
                linecolor=DUST_GREY, tickcolor=DUST_GREY,
                tickfont=dict(size=12, color=JET_BLACK),
                tickprefix="$", tickformat=",.0f",
            ),
            showlegend=False,
            hovermode="closest",
            hoverlabel=dict(
                bgcolor=PLATINUM, bordercolor=DUST_GREY,
                font=dict(size=13, color=JET_BLACK),
            ),
            **extra,
        )

    def add_events(fig):
        for dt, label in sorted(events.items()):
            fig.add_shape(
                type="line", x0=dt, x1=dt, y0=0, y1=1,
                xref="x", yref="paper",
                line=dict(color=RED, width=2.4, dash="dot"),
            )
            fig.add_annotation(
                x=dt, y=0.97, xref="x", yref="paper",
                text=label, textangle=-90,
                font=dict(size=10, color=RED),
                showarrow=False, xanchor="right",
                bgcolor="rgba(255,255,255,0.85)", borderpad=3,
            )

    def apply_time_range(fig, selected_range):
        if not selected_range or selected_range not in time_ranges:
            return
        spec = time_ranges[selected_range]
        fig.update_xaxes(range=[spec["start"], spec["end"]])

    def _add_odds_line(fig, axis="y2"):
        if not len(odds_line):
            return

        fig.add_trace(go.Scatter(
            x=odds_line["odds_time"],
            y=odds_line["odds_pct"],
            mode="lines",
            line=dict(color=RED, width=4, shape="spline"),
            name="Odds (% chance)",
            yaxis=axis,
            hovertemplate="<b>Odds:</b> %{y:.1f}%<br>%{x|%b %d · %H:%M}<extra></extra>",
        ))

    # -- 5. Figures -----------------------------------------------------------
    def _whale_traces(fig, df_w, yaxis=None):
        """Add whale scatter traces to fig - teal for normal, orange+label for highlighted."""
        if not len(df_w):
            return
        mx    = df_w["display_dollar"].max() or 1
        extra = dict(yaxis=yaxis) if yaxis else {}

        hi_mask  = df_w["user_id"].isin(highlighted)
        normal   = df_w[~hi_mask]
        hi_users = df_w[hi_mask]

        # Normal whales - teal dots
        if len(normal):
            sz = normal["display_dollar"].values
            fig.add_trace(go.Scatter(
                x=normal["first_trade"], y=normal["display_dollar"],
                mode="markers",
                marker=dict(
                    size=9 + 38 * np.sqrt(sz / mx),
                    color=TEAL, opacity=0.72,
                    line=dict(width=1.6, color="rgba(15,118,110,0.22)"),
                ),
                hovertemplate=(
                    "<b>User:</b> %{customdata[0]}<br>"
                    "<b>Bet:</b> $%{customdata[1]:,.0f}<br>"
                    "<b>Odds:</b> %{customdata[2]:.2f}<br>"
                    "<b>Net Profit:</b> $%{customdata[3]:,.0f}<br>"
                    "%{x|%b %d · %H:%M}<extra></extra>"
                ),
                customdata=np.stack([
                    normal["user_id"],
                    normal["dollar_amount"],
                    normal["avg_odds"],
                    normal["win_amount"] if "win_amount" in normal else np.zeros(len(normal))
                ], axis=-1),
                **extra,
            ))

        # Highlighted users - orange dots with name label above
        for _, row in hi_users.iterrows():
            label   = highlighted.get(row["user_id"], row["user_id"][:8])
            disp    = row["display_dollar"]
            sz      = 8 + 36 * np.sqrt(disp / mx)
            is_hedged = float(row["dollar_amount"]) == 0
            hover_extra = f"<br>{row['contracts']:,.0f} contracts" if is_hedged else ""
            fig.add_trace(go.Scatter(
                x=[row["first_trade"]], y=[disp],
                mode="markers+text",
                text=[label],
                textposition="top center",
                textfont=dict(size=12, color=HIGH, family="Inter, sans-serif"),
                marker=dict(
                    size=sz, color=HIGH, opacity=0.9,
                    line=dict(width=2.2, color="white"),
                ),
                hovertemplate=(
                    f"<b>{label}</b><br>"
                    f"<b>Bet:</b> ${row['dollar_amount']:,.0f}<br>"
                    f"<b>Odds:</b> {row['avg_odds']:.2f}<br>"
                    f"<b>Net Profit:</b> ${row['win_amount'] if 'win_amount' in row else 0:,.0f}<br>"
                    f"%{{x|%b %d · %H:%M}}<extra></extra>"
                ),
                **extra,
            ))


    def fig_whales(show_odds=True):
        layout_extra = {}
        if show_odds:
            layout_extra["yaxis2"] = dict(
                overlaying="y", side="right",
                range=[0, 100],
                showgrid=False, zeroline=False,
                title="Odds (%)",
                title_font=dict(color=RED),
                tickfont=dict(size=11, color=RED),
                ticksuffix="%",
            )
        fig = go.Figure(layout=base_layout(**layout_extra))
        if len(whales):
            _whale_traces(fig, whales)
            fig.add_hline(y=threshold, line_dash="dot", line_color=GRAY, line_width=2)
        if show_odds:
            _add_odds_line(fig, axis="y2")
        add_events(fig)
        fig.update_layout(yaxis_title="Position size (USD)")
        return fig


    def fig_crowd(show_odds=True):
        layout_extra = {}
        if show_odds:
            layout_extra["yaxis2"] = dict(
                overlaying="y", side="right",
                range=[0, 100],
                showgrid=False, zeroline=False,
                title="Odds (%)",
                title_font=dict(color=RED),
                tickfont=dict(size=11, color=RED),
                ticksuffix="%",
            )
        fig = go.Figure(layout=base_layout(**layout_extra))
        if len(hourly):
            fig.add_trace(go.Scatter(
                x=hourly["hour"], y=hourly["vol"],
                mode="lines", line=dict(width=0),
                fill="tozeroy", fillcolor="rgba(29,78,216,0.06)",
                hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=hourly["hour"], y=hourly["avg"],
                mode="lines", line=dict(color=BLUE, width=3.4),
                hovertemplate="<b>$%{y:,.0f} / hr</b><br>%{x|%b %d · %H:%M}<extra></extra>",
            ))
        if show_odds:
            _add_odds_line(fig, axis="y2")
        add_events(fig)
        # Use same Y axis cap as combined visualization
        max_y = hourly["avg"].max() if len(hourly) else 0
        fig.update_layout(yaxis_title="Volume per hour (USD)", yaxis=dict(range=[0, max_y * 1.1]))
        return fig


    def fig_comparison(show_odds=True):
        if show_odds:
            fig = go.Figure(layout=base_layout(
                yaxis2=dict(
                    overlaying="y", side="right",
                    showgrid=False, zeroline=False,
                    tickfont=dict(size=11, color=TEAL),
                    tickprefix="$", tickformat=",.0f",
                    linecolor="rgba(0,0,0,0)", tickcolor="rgba(0,0,0,0)",
                ),
                yaxis3=dict(
                    overlaying="y", side="right",
                    anchor="free", position=1.0,
                    range=[0, 100],
                    showgrid=False, zeroline=False,
                    title="Odds (%)",
                    title_font=dict(color=RED),
                    tickfont=dict(size=11, color=RED),
                    ticksuffix="%",
                ),
            ))
        else:
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
                mode="lines", line=dict(color=BLUE, width=3.4),
                fill="tozeroy", fillcolor="rgba(29,78,216,0.06)",
                hovertemplate="<b>Crowd $%{y:,.0f}/hr</b><br>%{x|%b %d · %H:%M}<extra></extra>",
            ))
        if len(whales):
            _whale_traces(fig, whales, yaxis="y2")
        if show_odds:
            _add_odds_line(fig, axis="y3")
        add_events(fig)
        fig.update_layout(
            margin=dict(l=72, r=82 if show_odds else 52, t=28, b=52),
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

    # -- 6. Dash app ----------------------------------------------------------
    whale_vol_sum = whales["dollar_amount"].sum()
    wpct = round(whale_vol_sum / total_vol * 100, 1) if total_vol else 0.0
    
    # Calculate exactly how much money the whales "took home" in net profit
    whale_winnings = whales["win_amount"].sum() if len(whales) else 0.0
    
    # The percentage of the total market profit that was taken home by the whales
    whale_winnings_pct = round(whale_winnings / total_market_winnings * 100, 1) if total_market_winnings else 0.0

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
    app.layout = material_layout(
        market_title,
        [
            stat_block("Total volume",    f"${total_vol:,.0f}"),
            stat_block("Whale threshold", f"\u2265 ${threshold:,.0f}", TEAL),
            stat_block("Whale volume",    f"${whales['dollar_amount'].sum():,.0f}", BLUE),
            stat_block("Crowd volume",    f"${crowd['dollar_amount'].sum():,.0f}", BLUE),
            stat_block("Whale bettors",   str(len(whales)),            TEAL),
            stat_block("Whale share",     f"{wpct}%",                  TEAL),
            stat_block("Whale winnings",  f"${whale_winnings:,.0f}",  TEAL),
            stat_block("Whale win / vol", f"{whale_winnings_pct}%",   TEAL),
            stat_block("Crowd bettors",   f"{len(crowd):,}",           BLUE),
            stat_block("Crowd avg bet",   f"${crowd['dollar_amount'].mean():,.0f}", BLUE),
        ],
        events,
        time_ranges,
        default_range
    )


    @app.callback(
        Output("chart-content", "children"),
        Input("tabs", "value"),
        Input("show-odds-toggle", "checked"),
        Input("time-range-selector", "value"),
    )
    def render_tab(tab, show_odds_checked, selected_range):
        show_odds = bool(show_odds_checked)
        fig = {
            "whales": lambda: fig_whales(show_odds=show_odds),
            "crowd": lambda: fig_crowd(show_odds=show_odds),
            "cmp": lambda: fig_comparison(show_odds=show_odds),
        }[tab]()
        apply_time_range(fig, selected_range)
        return dcc.Graph(
            figure=fig,
            config={"displayModeBar": True, "responsive": True},
            style={"height": "640px", "background": "#fff"},
        )

    @app.callback(
        Output("stats-container", "children"),
        Input("time-range-selector", "value"),
        Input("filter-stats-toggle", "checked")
    )
    def update_stats(selected_range, is_filtered):
        if is_filtered and selected_range and selected_range in time_ranges:
            spec = time_ranges[selected_range]
            
            def filter_df(d):
                return d[(d["first_trade"] >= spec["start"]) & (d["first_trade"] <= spec["end"])]
                
            f_whales = filter_df(whales)
            f_crowd = filter_df(crowd)
            f_df_winners = filter_df(df_winners)
            
            f_total_vol = f_whales["dollar_amount"].sum() + f_crowd["dollar_amount"].sum()
            f_whale_winnings = f_whales["win_amount"].sum() if len(f_whales) else 0.0
            f_total_market_winnings = f_df_winners.apply(get_net_profit, axis=1).sum() if len(f_df_winners) else 0.0
        else:
            f_whales = whales
            f_crowd = crowd
            f_total_vol = total_vol
            f_whale_winnings = whale_winnings
            f_total_market_winnings = total_market_winnings
            
        wpct = round(f_whales["dollar_amount"].sum() / f_total_vol * 100, 1) if f_total_vol else 0.0
        wwpct = round(f_whale_winnings / f_total_market_winnings * 100, 1) if f_total_market_winnings else 0.0
        
        crowd_mean = f_crowd['dollar_amount'].mean()
        crowd_mean_val = f"${crowd_mean:,.0f}" if pd.notnull(crowd_mean) else "$0"

        updated_stats = [
            stat_block("Total volume",    f"${f_total_vol:,.0f}"),
            stat_block("Whale threshold", f"\u2265 ${threshold:,.0f}", TEAL),
            stat_block("Whale volume",    f"${f_whales['dollar_amount'].sum():,.0f}", BLUE),
            stat_block("Crowd volume",    f"${f_crowd['dollar_amount'].sum():,.0f}", BLUE),
            stat_block("Whale bettors",   str(len(f_whales)),            TEAL),
            stat_block("Whale share",     f"{wpct}%",                  TEAL),
            stat_block("Whale winnings",  f"${f_whale_winnings:,.0f}",  TEAL),
            stat_block("Whale win / vol", f"{wwpct}%",   TEAL),
            stat_block("Crowd bettors",   f"{len(f_crowd):,}",           BLUE),
            stat_block("Crowd avg bet",   crowd_mean_val, BLUE),
        ]
        
        return [
            dmc.Group(
                children=updated_stats[:len(updated_stats)//2] if updated_stats else [],
                gap=12,
                grow=True,
                wrap="nowrap",
            ),
            dmc.Group(
                children=updated_stats[len(updated_stats)//2:] if updated_stats else [],
                gap=12,
                grow=True,
                wrap="nowrap",
            )
        ]

    print(f"\n  Market  : {market_title}")
    print(f"  Rows    : {len(df):,}")
    print(f"  Whales  : {len(whales)}  ({wpct}%,  threshold ${threshold:,.0f})")
    print(f"  Open    : http://127.0.0.1:{port}\n")
    app.run(port=port, debug=False)


if __name__ == "__main__":
    keyword = sys.argv[1].lower() if len(sys.argv) > 1 else None
    visualize(keyword=keyword)