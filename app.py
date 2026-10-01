import streamlit as st
import requests
import json

st.set_page_config(
    page_title="Arbitrage Calculator (Kalshi & Polymarket)",
    page_icon="⚖️",
    layout="wide"
)

# ------------------------------------------------------------------
# Live Market API Fetchers (Cached for 60s)
# ------------------------------------------------------------------
@st.cache_data(ttl=60)
def fetch_kalshi_markets():
    """Fetch open markets from Kalshi public REST API."""
    url = "https://api.elections.kalshi.com/trade-api/v2/markets"
    params = {"limit": 100, "status": "open"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("markets", [])
            parsed = []
            for m in data:
                title = m.get("title") or m.get("ticker", "Unknown")
                yes_ask = m.get("yes_ask", 0)
                no_ask = m.get("no_ask", 0)
                
                # Derive ask from opposite bid if ask is unlisted
                if yes_ask == 0 and m.get("no_bid", 0) > 0:
                    yes_ask = 100 - m.get("no_bid")
                if no_ask == 0 and m.get("yes_bid", 0) > 0:
                    no_ask = 100 - m.get("yes_bid")

                if yes_ask > 0 and no_ask > 0:
                    parsed.append({
                        "id": m.get("ticker"),
                        "title": title,
                        "yes_odds": round(100.0 / yes_ask, 2),
                        "no_odds": round(100.0 / no_ask, 2),
                        "source": "Kalshi"
                    })
            return parsed
    except Exception as e:
        st.sidebar.error(f"Kalshi API connection issue: {e}")
    return []

@st.cache_data(ttl=60)
def fetch_polymarket_markets():
    """Fetch active markets from Polymarket Gamma REST API."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {"closed": "false", "limit": 100, "active": "true"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            parsed = []
            for m in data:
                question = m.get("question") or "Unknown"
                raw_prices = m.get("outcomePrices")
                if raw_prices:
                    try:
                        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                        if len(prices) >= 2:
                            p_yes = float(prices[0])
                            p_no = float(prices[1])
                            if p_yes > 0 and p_no > 0:
                                parsed.append({
                                    "id": str(m.get("id")),
                                    "title": question,
                                    "yes_odds": round(1.0 / p_yes, 2),
                                    "no_odds": round(1.0 / p_no, 2),
                                    "source": "Polymarket"
                                })
                    except (ValueError, TypeError):
                        continue
            return parsed
    except Exception as e:
        st.sidebar.error(f"Polymarket API connection issue: {e}")
    return []

# ------------------------------------------------------------------
# UI & Navigation
# ------------------------------------------------------------------
st.title("⚖️ Prediction Market Arbitrage Scanner")
st.caption("Live cross-exchange market integration for Kalshi and Polymarket.")

# Sidebar Configuration
st.sidebar.header("⚙️ Settings")
mode = st.sidebar.radio("Data Mode", ["📡 Live Scanner (APIs)", "✏️ Manual Input"])
budget = st.sidebar.number_input("Total Investment ($)", min_value=1.0, value=100.0, step=10.0)
fee_pct = st.sidebar.number_input("Exchange Fee on Profit (%)", min_value=0.0, max_value=20.0, value=0.0, step=0.5)

if st.sidebar.button("🔄 Refresh API Data"):
    st.cache_data.clear()
    st.rerun()

odds_yes = 7.10
odds_no = 1.15
yes_source = "Manual"
no_source = "Manual"

if mode == "📡 Live Scanner (APIs)":
    kalshi_list = fetch_kalshi_markets()
    poly_list = fetch_polymarket_markets()

    col_k, col_p = st.columns(2)

    with col_k:
        st.subheader("🏛️ Kalshi Markets")
        if kalshi_list:
            k_titles = {f"{m['title']} (YES: {m['yes_odds']} | NO: {m['no_odds']})": m for m in kalshi_list}
            selected_k_label = st.selectbox("Select Kalshi Market", list(k_titles.keys()))
            selected_k = k_titles[selected_k_label]
        else:
            st.warning("No live markets loaded from Kalshi.")
            selected_k = None

    with col_p:
        st.subheader("🟣 Polymarket Markets")
        if poly_list:
            p_titles = {f"{m['title']} (YES: {m['yes_odds']} | NO: {m['no_odds']})": m for m in poly_list}
            selected_p_label = st.selectbox("Select Polymarket Event", list(p_titles.keys()))
            selected_p = p_titles[selected_p_label]
        else:
            st.warning("No live markets loaded from Polymarket.")
            selected_p = None

    # Compare selected odds to pick the highest available YES and NO
    if selected_k and selected_p:
        if selected_k["yes_odds"] >= selected_p["yes_odds"]:
            odds_yes = selected_k["yes_odds"]
            yes_source = f"Kalshi ({selected_k['title'][:30]}...)"
        else:
            odds_yes = selected_p["yes_odds"]
            yes_source = f"Polymarket ({selected_p['title'][:30]}...)"

        if selected_k["no_odds"] >= selected_p["no_odds"]:
            odds_no = selected_k["no_odds"]
            no_source = f"Kalshi ({selected_k['title'][:30]}...)"
        else:
            odds_no = selected_p["no_odds"]
            no_source = f"Polymarket ({selected_p['title'][:30]}...)"

else:
    st.sidebar.subheader("Custom Odds Entry")
    odds_yes = st.sidebar.number_input("Best YES Odds", min_value=1.01, value=7.10, step=0.05)
    odds_no = st.sidebar.number_input("Best NO Odds", min_value=1.01, value=1.15, step=0.01)

# ------------------------------------------------------------------
# Arbitrage Calculation Engine
# ------------------------------------------------------------------
p_yes = 1 / odds_yes
p_no = 1 / odds_no
implied_sum = p_yes + p_no

stake_yes = budget * (odds_no / (odds_yes + odds_no))
stake_no = budget - stake_yes

gross_payout_yes = stake_yes * odds_yes
gross_payout_no = stake_no * odds_no

profit_yes_gross = max(0.0, gross_payout_yes - budget)
profit_no_gross = max(0.0, gross_payout_no - budget)

net_payout_yes = gross_payout_yes - (profit_yes_gross * (fee_pct / 100))
net_payout_no = gross_payout_no - (profit_no_gross * (fee_pct / 100))

guaranteed_net_payout = min(net_payout_yes, net_payout_no)
net_profit = guaranteed_net_payout - budget
roi = (net_profit / budget) * 100

st.divider()

# Key Performance Indicators
kpi1, kpi2, kpi3 = st.columns(3)
kpi1.metric("Implied Prob. Sum", f"{implied_sum * 100:.2f}%")
kpi2.metric("Net Profit / Loss", f"${net_profit:+.2f}")
kpi3.metric("ROI", f"{roi:+.2f}%")

# Status Banner
if implied_sum < 1.0 and net_profit > 0:
    st.success("🎯 **ARBITRAGE OPPORTUNITY DETECTED:** Guaranteed profit locked across selections.")
elif implied_sum < 1.0 and net_profit <= 0:
    st.warning("⚠️ **ARBITRAGE BEFORE FEES:** Positive raw spread, but exchange fees negate profit.")
else:
    st.error("❌ **NO ARBITRAGE:** Combined market structure results in a net loss.")

# Detailed Allocation Table
st.subheader("📊 Execution Plan")
data = {
    "Outcome": ["YES", "NO"],
    "Best Odds": [f"{odds_yes:.2f}", f"{odds_no:.2f}"],
    "Selected Venue": [yes_source, no_source],
    "Implied Prob.": [f"{p_yes * 100:.2f}%", f"{p_no * 100:.2f}%"],
    "Optimal Stake": [f"${stake_yes:.2f}", f"${stake_no:.2f}"],
    "Net Payout": [f"${net_payout_yes:.2f}", f"${net_payout_no:.2f}"]
}
st.dataframe(data, hide_index=True, use_container_width=True)
