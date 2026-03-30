import os
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import html
from template_VisualizeInsiders import create_layout

JET_BLACK   = "#223843"
TEAL        = "#0f766e"
RED         = "#be123c"
HIGH        = "#ea580c"
BURNT_PEACH = "#d77a61"
PERCENT_VOLUME = 80.0
FIRST_X_TRADES = 10

def get_market_title(m_dict, df):
    if "market_title" in df.columns and pd.notnull(df["market_title"].iloc[0]):
        return str(df["market_title"].iloc[0])
    slug = m_dict.get("slug", "")
    parts = slug.split("-")
    clean_parts = [p for p in parts if not p.isdigit()]
    if clean_parts:
        return " ".join(clean_parts).title()
    return m_dict.get("name", "Unknown Market")

def load_data_for_market(m_dict):
    insider_csv = f"data/{m_dict['output']}_insiders.csv"
    
    if not os.path.exists(insider_csv):
        return pd.DataFrame(), m_dict.get("name", "Unknown Market")
        
    df = pd.read_csv(insider_csv)
    
    def categorize(row):
        if row["is_first_x"] and row["Is_percent"]:
            return "Critical Risk (Both Flags)"
        if row["is_first_x"]:
            return f"Suspicious (New Account({FIRST_X_TRADES} Trades))"
        if row["Is_percent"]:
            return f"Suspicious (>{PERCENT_VOLUME}% Concentration)"
        return "Normal Whale"
    
    df["category"] = df.apply(categorize, axis=1)
    market_title = get_market_title(m_dict, df)
    return df, market_title

def calculate_network_metrics(df):
    if df.empty: return df
    df = df.copy()
    vol_factor = (df["portfolio_concentration"] / 100.0) * 50
    history_factor = np.maximum(0, (10 - df["prior_bet_count"]) / 10.0) * 50
    df["risk_score"] = vol_factor + history_factor
    np.random.seed(42)
    base_radius = 110 - df["risk_score"]
    df["radius"] = base_radius + np.random.uniform(-4, 4, len(df))
    df["theta_rad"] = df["user_id"].apply(lambda x: int(str(x)[2:8], 16) % 360) * (np.pi / 180)
    df["x"] = df["radius"] * np.cos(df["theta_rad"])
    df["y"] = df["radius"] * np.sin(df["theta_rad"])
    return df

def fig_threat_constellation(df):
    cat_colors = {
        "Normal Whale": TEAL,
        f"Suspicious (New Account({FIRST_X_TRADES} Trades))": BURNT_PEACH,
        f"Suspicious (>{PERCENT_VOLUME}% Concentration)": HIGH,
        "Critical Risk (Both Flags)": RED
    }
    
    fig = go.Figure()
    if df.empty: return fig

    max_vol = df["market_position"].max() if not df.empty else 1
    
    for cat, color in cat_colors.items():
        sub = df[df["category"] == cat]
        if sub.empty: continue
        edge_x = []
        edge_y = []
        for _, row in sub.iterrows():
            edge_x.extend([0, row["x"], None])
            edge_y.extend([0, row["y"], None])
        fig.add_trace(go.Scatter(
            x=edge_x, y=edge_y, mode="lines",
            line=dict(color=color, width=0.5), opacity=0.25,
            hoverinfo="skip", showlegend=False
        ))

    for cat, color in cat_colors.items():
        sub = df[df["category"] == cat]
        if sub.empty: continue
        sizes = 8 + 40 * np.sqrt(sub["market_position"] / max_vol)
        fig.add_trace(go.Scatter(
            x=sub["x"], y=sub["y"], mode="markers", name=cat,
            marker=dict(size=sizes, color=color, line=dict(color="white", width=1.2), opacity=0.95),
            customdata=np.stack((sub["user_id"], sub["prior_bet_count"], sub["portfolio_concentration"], sub["market_position"]), axis=-1),
            hovertemplate=(
                "<b>User:</b> %{customdata[0]}<br><b>Prior Bets:</b> %{customdata[1]}<br>"
                "<b>Concentration:</b> %{customdata[2]:.1f}%<br><b>Position Size:</b> $%{customdata[3]:,.0f}<extra></extra>"
            )
        ))
        
    fig.add_trace(go.Scatter(
        x=[0], y=[0], mode="markers+text",
        marker=dict(size=35, color=JET_BLACK, symbol="hexagon", line=dict(color=RED, width=2)),
        text=["<b>THE MARKET</b>"], textposition="top center",
        textfont=dict(color=JET_BLACK, size=12), hoverinfo="skip", showlegend=False
    ))

    fig.add_annotation(
        x=15, y=15, text="<b>Core Threat Cluster</b><br>(High Concentration, Zero History)",
        showarrow=True, arrowhead=2, arrowsize=1, arrowwidth=2, arrowcolor=RED,
        ax=40, ay=40, font=dict(color=RED, size=13), bgcolor="rgba(255,255,255,0.85)", borderpad=4
    )
    
    fig.add_annotation(
        x=-80, y=-80, text="<b>Normal Orbit</b><br>(Established Traders)",
        showarrow=True, arrowhead=2, arrowsize=1, arrowwidth=1.5, arrowcolor=TEAL,
        ax=-40, ay=-40, font=dict(color=TEAL, size=13), bgcolor="rgba(255,255,255,0.85)", borderpad=4
    )

    fig.update_layout(
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, range=[-130, 130]),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, range=[-130, 130]),
        font=dict(family="Roboto, sans-serif", size=13, color=JET_BLACK),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=20, r=20, t=20, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=-0.1, xanchor="center", x=0.5)
    )
    return fig

def fig_treemap_concentration(df):
    if df.empty: return go.Figure()
    total_vol = df["market_position"].sum()
    labels = ["Total Whale Stake"]
    parents = [""]
    values = [total_vol]
    colors = [JET_BLACK]
    
    cat_colors = {
        "Normal Whale": TEAL,
        f"Suspicious (New Account({FIRST_X_TRADES} Trades))": BURNT_PEACH,
        f"Suspicious (>{PERCENT_VOLUME}% Concentration)": HIGH,
        "Critical Risk (Both Flags)": RED
    }
    
    for cat, color in cat_colors.items():
        cat_df = df[df["category"] == cat]
        cat_vol = cat_df["market_position"].sum()
        if cat_vol > 0:
            cat_pct = (cat_vol / total_vol * 100) if total_vol > 0 else 0
            cat_label = f"{cat} ({cat_pct:.1f}%)"
            labels.append(cat_label)
            parents.append("Total Whale Stake")
            values.append(cat_vol)
            colors.append(color)
            for _, row in cat_df.nlargest(15, "market_position").iterrows():
                user_pct = (row['market_position'] / total_vol * 100) if total_vol > 0 else 0
                user_label = f"{row['user_id'][:6]}... ({user_pct:.1f}%)"
                labels.append(user_label)
                parents.append(cat_label)
                values.append(row["market_position"])
                colors.append(color)

    fig = go.Figure(go.Treemap(
        labels=labels, parents=parents, values=values, marker=dict(colors=colors), branchvalues="total",
        textinfo="label+value", texttemplate="%{label}<br>$%{value:,.0f}", hovertemplate="<b>%{label}</b><br>Position: $%{value:,.0f}<extra></extra>"
    ))
    fig.update_layout(font=dict(family="Roboto, sans-serif", size=14), paper_bgcolor="rgba(0,0,0,0)", margin=dict(l=10, r=10, t=10, b=10))
    return fig

def get_layout(m_dict):
    df, market_title = load_data_for_market(m_dict)
    
    if df.empty:
        return html.Div([
            html.H3(f"Data Missing for {market_title}", style={"color": "#be123c"}),
            html.P("Run the scraper to generate the insider data first.")
        ], style={"padding": "40px", "textAlign": "center", "fontFamily": "Inter, sans-serif"})
    
    total_vol = df["market_position"].sum()
    suspicious_df = df[df["category"] != "Normal Whale"]
    suspicious_vol = suspicious_df["market_position"].sum()
    
    normal_vol = total_vol - suspicious_vol
    pct_suspicious = (suspicious_vol / total_vol * 100) if total_vol > 0 else 0
    pct_normal = (normal_vol / total_vol * 100) if total_vol > 0 else 0
    
    total_whales = len(df)
    flagged_accounts = len(suspicious_df)
    
    df_mapped = calculate_network_metrics(df)
    fig_constellation = fig_threat_constellation(df_mapped)
    fig_treemap = fig_treemap_concentration(df)
    
    return create_layout(
        market_title, total_vol, suspicious_vol, pct_suspicious, pct_normal,
        total_whales, flagged_accounts, fig_constellation, fig_treemap
    )