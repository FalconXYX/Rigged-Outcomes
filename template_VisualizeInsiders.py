from dash import dcc, html
import dash_mantine_components as dmc

JET_BLACK   = "#223843"
PLATINUM    = "#eff1f3"
SLATE_GREY  = "#475569"
TEAL        = "#0f766e"
BLUE        = "#1d4ed8"
RED         = "#be123c"
HIGH        = "#ea580c"
BURNT_PEACH = "#d77a61"

def stat_block(label, value, color=BURNT_PEACH):
    return html.Div(
        style={
            "padding": "16px 20px",
            "background": PLATINUM,
            "borderRadius": "12px",
            "borderLeft": f"6px solid {color}",
            "flex": "1 1 auto",
            "minWidth": "150px",
            "boxShadow": "0 2px 8px rgba(0,0,0,0.04)"
        },
        children=[
            html.Span(label, style={"color": JET_BLACK, "fontSize": 13, "fontWeight": 700, "textTransform": "uppercase", "letterSpacing": "0.5px"}),
            html.Br(),
            html.Span(value, style={"color": JET_BLACK, "fontSize": 26, "fontWeight": 800, "fontFamily": "Roboto"})
        ]
    )

def create_layout(market_title, total_vol, suspicious_vol, pct_suspicious, pct_normal, total_whales, flagged_accounts, fig_constellation, fig_treemap):
    return dmc.MantineProvider(
        theme={"fontFamily": "Inter, sans-serif"},
        children=html.Div(
            style={"background": "#fafafa", "minHeight": "100vh", "padding": "24px 32px"},
            children=dmc.Container(
                fluid=True,
                style={"maxWidth": "1600px"},
                children=[
                    dmc.Stack(
                        gap="xs",
                        style={"marginBottom": "24px"},
                        children=[
                            dmc.Text("INSIDER TRADING DETECTION", fz="sm", fw=700, c=RED, style={"letterSpacing": "1px"}),
                            dmc.Title(market_title, order=2, c=JET_BLACK, style={"fontFamily": "Roboto", "fontWeight": 900}),
                        ],
                    ),
                    
                    html.Div(
                        style={"display": "flex", "gap": "16px", "marginBottom": "20px", "flexWrap": "wrap"},
                        children=[
                            stat_block("Total Whale Vol", f"${total_vol:,.0f}", BLUE),
                            stat_block("Suspicious Vol", f"${suspicious_vol:,.0f}", RED),
                            stat_block("Suspicious Ratio", f"{pct_suspicious:.1f}%", HIGH),
                            stat_block("Total Whales", str(total_whales), TEAL),
                            stat_block("Flagged Accounts", str(flagged_accounts), RED),
                        ]
                    ),
                    
                    html.Div(
                        style={
                            "background": "white", "padding": "24px", "borderRadius": "12px",
                            "border": "1px solid #e2e8f0", "marginBottom": "32px",
                            "boxShadow": "0 2px 4px rgba(0,0,0,0.02)"
                        },
                        children=[
                            html.P(f"Analysis of '{market_title}' reveals a critical structural imbalance in capital deployment.", style={"color": JET_BLACK, "fontSize": "18px", "fontWeight": 800, "marginBottom": "12px", "marginTop": "0"}),
                            html.P(f"{pct_suspicious:.1f}% of the capital (${suspicious_vol:,.0f}) is artificially concentrated among flagged accounts. These accounts exhibit suspicious signatures: they possess low prior trading history on the platform, yet they executed large, targeted positions exclusively on this outcome.", style={"color": RED, "fontSize": "16px", "fontWeight": 600, "marginBottom": "12px", "lineHeight": "1.6"}),
                            html.P("A user is flagged as suspicious if they match one or both of the following criteria: they are a new account with less than 10 total trades, or more than 80% of their historical trading volume has been bet entirely within this single market.", style={"color": SLATE_GREY, "fontSize": "14px", "fontWeight": 400, "marginBottom": "0", "lineHeight": "1.5"}),
                            html.P("Individually, these factors might not raise red flags. However, because they apply only to high-volume bettors targeting volatile events, they point toward potential insider trading despite a lack of absolute proof.", style={"color": SLATE_GREY, "fontSize": "14px", "fontWeight": 400, "marginBottom": "0", "lineHeight": "1.5"}),

                        ]
                    ),
                    
                    html.Div(
                        style={"display": "flex", "gap": "24px", "flexWrap": "wrap"},
                        children=[
                            html.Div(
                                style={"flex": "1 1 0", "minWidth": "500px"},
                                children=dmc.Paper(
                                    radius="lg", p="xl", shadow="sm", style={"background": "white", "height": "750px"},
                                    children=[
                                        dmc.Text("Threat Constellation Network", fw=800, fz="xl", c=JET_BLACK, style={"marginBottom": "8px"}),
                                        dmc.Text("Users are mapped by gravitational pull to the market center. Flagged accounts with zero history and extreme volume are pulled directly into the core, while normal organic trading forms the outer orbit.", fz="sm", c=SLATE_GREY, style={"marginBottom": "16px", "lineHeight": "1.5"}),
                                        dcc.Graph(figure=fig_constellation, style={"height": "580px"}, config={"displayModeBar": False})
                                    ]
                                )
                            ),
                            html.Div(
                                style={"flex": "1 1 0", "minWidth": "500px"},
                                children=dmc.Paper(
                                    radius="lg", p="xl", shadow="sm", style={"background": "white", "height": "750px"},
                                    children=[
                                        dmc.Text("Capital Concentration", fw=800, fz="xl", c=JET_BLACK, style={"marginBottom": "8px"}),
                                        dmc.Text("A hierarchical breakdown of how much market volume is being hoarded by each risk category, highlighting the specific wallets driving the anomalous activity.", fz="sm", c=SLATE_GREY, style={"marginBottom": "16px", "lineHeight": "1.5"}),
                                        dcc.Graph(figure=fig_treemap, style={"height": "580px"}, config={"displayModeBar": False})
                                    ]
                                )
                            )
                        ]
                    )
                ]
            )
        )
    )