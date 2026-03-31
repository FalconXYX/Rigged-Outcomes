import json
from dash import Dash, dcc, html, Input, Output, _dash_renderer
import VisualizeBetTiming
import VisualizeInsiders
_dash_renderer._set_react_version("18.2.0")
# 1. Initialize the Master App
# suppress_callback_exceptions is required because we are dynamically swapping layouts
app = Dash(__name__, suppress_callback_exceptions=True)
server = app.server  # This is the WSGI server needed for web deployment

# 2. Load Markets for the Dropdown
with open("markets.json") as f:
    ALL_MARKETS = json.load(f)

market_options = [{"label": m["name"], "value": m["slug"]} for m in ALL_MARKETS]
default_market = ALL_MARKETS[0]["slug"]

# 3. Master Layout
app.layout = html.Div([
    # Navigation Bar
    html.Div([
        html.H2("Market Analysis Portal", style={"display": "inline-block", "marginRight": "20px"}),
        
        # Market Selector
        dcc.Dropdown(
            id="market-selector",
            options=market_options,
            value=default_market,
            clearable=False,
            style={"width": "300px", "display": "inline-block", "marginRight": "20px"}
        ),
        
        # View Selector
        dcc.RadioItems(
            id="view-selector",
            options=[
                {"label": " Bet Timing ", "value": "timing"},
                {"label": " Insider Radar ", "value": "insiders"}
            ],
            value="timing",
            inline=True,
            style={"display": "inline-block"}
        )
    ], style={"padding": "20px", "background": "#eff1f3", "borderBottom": "2px solid #dbd3d8"}),
    
    # Dynamic Content Container
    html.Div(id="page-content", style={"padding": "20px"})
])

# 4. Master Callback to Swap Layouts
@app.callback(
    Output("page-content", "children"),
    Input("market-selector", "value"),
    Input("view-selector", "value")
)
def update_page(market_slug, view_type):
    # Find the dictionary for the selected market
    market_dict = next(m for m in ALL_MARKETS if m["slug"] == market_slug)
    
    if view_type == "timing":
        # We will create this function in Step 2
        return VisualizeBetTiming.get_layout(market_dict)
    else:
        # We will create this function in Step 2
        return VisualizeInsiders.get_layout(market_dict)

# 5. Register Sub-Callbacks
# We pass the master app to the modules so they can attach their specific interactions
VisualizeBetTiming.register_callbacks(app, ALL_MARKETS)
# VisualizeInsiders doesn't seem to have interactive callbacks in the provided code, 
# but if it does, you would register them here similarly.

if __name__ == "__main__":
    app.run(debug=True, port=8050)