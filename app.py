import streamlit as st
import requests
import json
from curl_cffi import requests as c_requests

st.set_page_config(
    page_title="Prediction Market Arbitrage Scanner",
    page_icon="⚖️",
    layout="wide"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9"
}

# Fallback active market data if Cloudflare blocks Streamlit Cloud datacenter IPs
FALLBACK_KALSHI = [
    {"id": "KXFEDOCT26", "title": "Fed Interest Rate Cut in Oct 2026", "yes_odds": 2.10, "no_odds": 1.91, "source": "Kalshi (Fallback)"},
    {"id": "KXPRES28", "title": "U.S. Presidential Election 2028 Winner", "yes_odds": 3.45, "no_odds": 1.41, "source": "Kalshi (Fallback)"},
    {"id": "KXCPI26", "title": "US CPI Inflation > 2.8% in Q4", "yes_odds": 1.85, "no_odds": 2.18, "source": "Kalshi (Fallback)"},
    {"id": "KXTIKTOK", "title": "TikTok Operating in US by EOY", "yes_odds": 6.29, "no_odds": 1.15, "source": "Kalshi (Fallback)"}
]

# ------------------------------------------------------------------
# Live Market API Fetchers
# ------------------------------------------------------------------
@st.cache_data(ttl=60)
def fetch_kalshi_markets():
    """Fetch active markets from Kalshi using Chrome TLS impersonation to bypass Cloudflare."""
    url = "https://external-api.kalshi.com/trade-api/v2/markets?limit=100&status=open"
    parsed = []

    try:
        # Impersonate Chrome 120 browser TLS fingerprint
        resp = c_requests.get(
            url, 
            impersonate="chrome120", 
            headers={"Accept": "application/json"}, 
            timeout=10
        )
        
        if resp.status_code == 200:
            data = resp.json().get("markets", [])
            for m in data:
                title = m.get("title") or m.get("ticker") or "Unknown"
                yes_ask = m.get("yes_ask", 0) or m.get("last_price", 0) or 0
                no_ask = m.get("no_ask", 0) or (100 - yes_ask if yes_ask > 0 else 0)

                if yes_ask > 0 and no_ask > 0:
                    parsed.append({
                        "id": m.get("ticker", "N/A"),
                        "title": title,
                        "yes_odds": round(100.0 / yes_ask, 2),
                        "no_odds": round(100.0 / no_ask, 2),
                        "source": "Kalshi (Live API)"
                    })

            if parsed:
                return parsed, f"✅ Connected to Kalshi API via TLS Impersonation ({len(parsed)} markets loaded)"
    except Exception as e:
        pass

    return FALLBACK_KALSHI, "⚠️ Kalshi API blocked. Loaded fallback dataset."

@st.cache_data(ttl=60)
def fetch_polymarket_markets():
    """Fetch active markets from Polymarket Gamma REST API."""
    url = "https://gamma-api.polymarket.com/markets?closed=false&limit=100&active=true"
    parsed = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            for m in resp.json():
                question = m.get("question") or "Unknown"
                raw_prices = m.get("outcomePrices")
                if raw_prices:
                    try:
                        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                        if len(prices) >= 2:
                            p_yes, p_no = float(prices[0]), float(prices[1])
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
            return parsed, f"✅ Connected to Polymarket API ({len(parsed)} markets loaded)"
    except Exception as e:
        return [], f"❌ Polymarket API Error: {e}"
    return [], "⚠️ Polymarket returned empty list"

# ------------------------------------------------------------------
# UI & Navigation
# ------------------------------------------------------------------
st.title("⚖️ Prediction Market Arbitrage Scanner")
st.caption("Live cross-exchange market scanner for Kalshi and Polymarket.")

# Sidebar Configuration
st.sidebar.header("⚙️ Controls")
mode = st.sidebar.radio("Mode", ["📡 Live Scanner", "✏️ Manual Custom Odds"])
budget = st.sidebar.number_input("Total Investment ($)", min_value=1.0, value=100.0, step=10.0)
fee_pct = st.sidebar.number_input("Platform Fee on Profit (%)", min_value=0.0, max_value=20.0, value=0.0, step=0.5)

if st.sidebar.button("🔄 Force API Refresh"):
    st.cache_data.clear()
    st.rerun()

odds_yes = 7.10
odds_no = 1.15
yes_source = "Manual Entry"
no_source = "Manual Entry"

if mode == "📡 Live Scanner":
    kalshi_list, k_status = fetch_kalshi_markets()
    poly_list, p_status = fetch_polymarket_markets()

    # Diagnostic Status Expander
    with st.expander("🔍 API Connection Health & Diagnostics"):
        st.write(f"**Kalshi Status:** {k_status}")
        st.write(f"**Polymarket Status:** {p_status}")

    col_k, col_p = st.columns(2)

    with col_k:
        st.subheader("🏛️ Kalshi Markets")
        if kalshi_list:
            k_titles = {f"{m['title']} (YES: {m['yes_odds']} | NO: {m['no_odds']})": m for m in kalshi_list}
            selected_k_label = st.selectbox("Select Kalshi Event", list(k_titles.keys()), key="k_select")
            selected_k = k_titles[selected_k_label]
        else:
            st.error("No Kalshi markets available.")
            selected_k = None

    with col_p:
        st.subheader("🟣 Polymarket Markets")
        if poly_list:
            p_titles = {f"{m['title']} (YES: {m['yes_odds']} | NO: {m['no_odds']})": m for m in poly_list}
            selected_p_label = st.selectbox("Select Polymarket Event", list(p_titles.keys()), key="p_select")
            selected_p = p_titles[selected_p_label]
        else:
            st.error("No Polymarket markets available.")
            selected_p = None

    # Routing logic: Pick the best available odds for YES and NO
    if selected_k and selected_p:
        if selected_k["yes_odds"] >= selected_p["yes_odds"]:
            odds_yes = selected_k["yes_odds"]
            yes_source = f"{selected_k['source']} ({selected_k['title'][:25]}...)"
        else:
            odds_yes = selected_p["yes_odds"]
            yes_source = f"{selected_p['source']} ({selected_p['title'][:25]}...)"

        if selected_k["no_odds"] >= selected_p["no_odds"]:
            odds_no = selected_k["no_odds"]
            no_source = f"{selected_k['source']} ({selected_k['title'][:25]}...)"
        else:
            odds_no = selected_p["no_odds"]
            no_source = f"{selected_p['source']} ({selected_p['title'][:25]}...)"
            
else:
    st.sidebar.subheader("Manual Odds Configuration")
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

# Dashboard Output
kpi1, kpi2, kpi3 = st.columns(3)
kpi1.metric("Implied Prob. Sum", f"{implied_sum * 100:.2f}%")
kpi2.metric("Net Profit / Loss", f"${net_profit:+.2f}")
kpi3.metric("ROI", f"{roi:+.2f}%")

if implied_sum < 1.0 and net_profit > 0:
    st.success("🎯 **ARBITRAGE OPPORTUNITY DETECTED:** Guaranteed profit locked across selections.")
elif implied_sum < 1.0 and net_profit <= 0:
    st.warning("⚠️ **ARBITRAGE BEFORE FEES:** Positive raw spread, but exchange fees negate profit.")
else:
    st.error("❌ **NO ARBITRAGE:** Combined market structure results in a net loss.")

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
