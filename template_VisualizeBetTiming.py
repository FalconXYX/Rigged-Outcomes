# Modern Material Design Dash Template for VisualizeBetTiming
# Place this file as template_material.py and import/use in VisualizeBetTiming.py

import dash_mantine_components as dmc
from dash import dcc, html

def stat_block(label, value, color="#d77a61"):
    return html.Div(
        style={
            "padding": "12px 16px",
            "background": "#eff1f3",
            "borderRadius": 10,
            "display": "flex",
            "flexDirection": "column",
            "flex": "1 1 auto",
            "minWidth": 120,
        },
        children=[
            html.Span(label, style={
                "color": color,
                "fontSize": 12,
                "fontWeight": 700,
                "textTransform": "uppercase",
                "letterSpacing": "0.5px",
                "marginBottom": 4,
            }),
            html.Span(value, style={
                "color": "#223843",
                "fontSize": 22,
                "fontWeight": 800,
            })
        ]
    )

def material_layout(market_title, stats, events, time_ranges, default_range):
    """Material Design layout for Dash app using dash_mantine_components."""

    select_data = [{"label": k, "value": k} for k in time_ranges.keys()] if time_ranges else []

    return dmc.MantineProvider(
        theme={
            "fontFamily": "Inter, Roboto, 'Helvetica Neue', Arial, sans-serif",
            "headings": {"fontFamily": "Roboto"},
            "colors": {
                "custom": ["#eff1f3", "#dbd3d8", "#d8b4a0", "#d77a61", "#223843", "#1b2d35", "#142228", "#0d161a", "#060b0d", "#000000"],
            },
            "primaryColor": "custom",
            "primaryShade": 4, # #223843 (Jet Black)
        },
        children=html.Div(
            style={
                "background": "#eff1f3", # Platinum
                "minHeight": "100vh",
                "padding": "0 0 12px 0",
            },
            children=dmc.Container(
                fluid=True,
                style={"width": "100%", "maxWidth": "100%", "padding": "16px 24px 20px"},
                children=[
                    dmc.Stack(
                        gap="xs",
                        children=[
                            dmc.Text(market_title, fz="md", fw=700, c="#d77a61"), # Burnt Peach
                            dmc.Title(
                                children="Timeline of Contract Activity",
                                order=1,
                                c="#223843", # Jet Black
                                style={"fontFamily": "Roboto", "fontWeight": 800, "letterSpacing": "-0.5px"},
                            ),
                        ],
                    ),

                    dmc.Paper(
                        radius="md",
                        shadow="sm",
                        p="lg",
                        style={"marginTop": 16, "background": "#ffffff"},
                        children=[
                            dmc.Group(
                                justify="space-between",
                                gap=16,
                                style={"flexWrap": "wrap"},
                                children=[
                                    dmc.Group(
                                        gap=14,
                                        children=[
                                            dmc.Text("View:", fw=700, c="#223843"), # Jet Black
                                            dmc.SegmentedControl(
                                                id="tabs",
                                                value="whales",
                                                data=[
                                                    {"label": "Whale Timing", "value": "whales"},
                                                    {"label": "Crowd Volume", "value": "crowd"},
                                                    {"label": "Comparison", "value": "cmp"},
                                                ],
                                                color="custom", # uses Jet black from theme
                                                radius="md",
                                                size="sm",
                                                fw=600,
                                            ),
                                        ],
                                    ),
                                    dmc.Group(
                                        gap=20,
                                        children=[
                                            dmc.Switch(
                                                id="show-odds-toggle",
                                                label="Show odds axis",
                                                checked=True,
                                                color="custom", # Jet Black
                                                size="sm",
                                                fw=600,
                                            ),
                                            dmc.Select(
                                                id="time-range-selector",
                                                data=select_data,
                                                value=default_range,
                                                placeholder="Time range",
                                                style={"minWidth": 240},
                                                clearable=False,
                                                radius="md",
                                                fw=600,
                                            ),
                                        ],
                                    ),
                                ],
                            ),

                            dmc.Divider(my="md", color="#dbd3d8"), # Dust Grey divider


                            html.Div(
                                id="chart-content",
                                style={
                                    "borderRadius": 14,
                                    "boxShadow": "0 6px 20px rgba(0,0,0,0.05)",
                                    "overflow": "hidden",
                                    "background": "#ffffff",
                                },
                            ),

                            dmc.Divider(my="lg", color="#dbd3d8"),

                            dmc.Stack(
                                gap="md",
                                children=[
                                    dmc.Group(
                                        children=stats[:len(stats)//2] if stats else [],
                                        gap=12,
                                        grow=True,
                                        wrap="nowrap",
                                    ),
                                    dmc.Group(
                                        children=stats[len(stats)//2:] if stats else [],
                                        gap=12,
                                        grow=True,
                                        wrap="nowrap",
                                    )
                                ]
                            ),
                        ],
                    ),
                ],
            ),
        ),
    )
