import streamlit as st
import requests
import json
from difflib import SequenceMatcher

st.set_page_config(
    page_title="Prediction Market Arbitrage Scanner",
    page_icon="⚖️",
    layout="wide"
)

# ------------------------------------------------------------------
# Live Market API Fetchers
# ------------------------------------------------------------------
# Pass query parameters directly to your worker so Kalshi returns active open markets
KALSHI_PROXY_URL = "https://kalshi-proxy.soahum-golhar.workers.dev/markets?limit=1000&status=open"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

@st.cache_data(ttl=180)
def fetch_kalshi_markets():
    """Fetch active markets from Kalshi via Cloudflare Worker Proxy."""
    parsed = []
    try:
        resp = requests.get(KALSHI_PROXY_URL, timeout=12)
        if resp.status_code == 200:
            raw = resp.json()
            data = raw.get("markets", []) if isinstance(raw, dict) else raw
            
            for m in data:
                title = m.get("title") or m.get("ticker") or "Unknown"
                
                # Kalshi prices are in cents (1 to 99)
                yes_ask = m.get("yes_ask", 0) or m.get("last_price", 0) or 0
                no_ask = m.get("no_ask", 0) or (100 - yes_ask if 0 < yes_ask < 100 else 0)

                if 0 < yes_ask < 100 and 0 < no_ask < 100:
                    parsed.append({
                        "id": m.get("ticker", "N/A"),
                        "title": title,
                        "yes_price": yes_ask / 100.0,
                        "no_price": no_ask / 100.0,
                        "yes_odds": round(100.0 / yes_ask, 2),
                        "no_odds": round(100.0 / no_ask, 2),
                        "source": "Kalshi"
                    })

            if parsed:
                return parsed, f"✅ Connected to Kalshi ({len(parsed)} active markets)"
            else:
                return [], "⚠️ Kalshi Worker returned empty market list."
        else:
            return [], f"❌ Kalshi Worker returned HTTP {resp.status_code}"
    except Exception as e:
        return [], f"❌ Connection error to Kalshi Worker: {e}"

@st.cache_data(ttl=180)
def fetch_polymarket_markets(category_slug="all", pages_to_fetch=3):
    """
    Fetch active markets from Polymarket Gamma API with pagination and category filtering.
    """
    parsed = []
    seen_ids = set()
    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            
            # Apply Polymarket Tag Slug
            if category_slug != "all":
                url += f"&tag_slug={category_slug}"

            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code == 200:
                events = resp.json()
                if not events:
                    break
                
                for ev in events:
                    markets = ev.get("markets", [])
                    event_title = ev.get("title", "")
                    
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids:
                            continue
                        seen_ids.add(m_id)

                        question = m.get("question") or event_title or "Unknown"
                        raw_prices = m.get("outcomePrices")
                        
                        if raw_prices:
                            try:
                                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                                if len(prices) >= 2:
                                    p_yes = float(prices[0])
                                    p_no = float(prices[1])
                                    
                                    if p_yes > 0 and p_no > 0:
                                        parsed.append({
                                            "id": m_id,
                                            "title": question,
                                            "yes_price": p_yes,
                                            "no_price": p_no,
                                            "yes_odds": round(1.0 / p_yes, 2),
                                            "no_odds": round(1.0 / p_no, 2),
                                            "source": "Polymarket"
                                        })
                            except (ValueError, TypeError):
                                continue
            else:
                break

        if parsed:
            return parsed, f"✅ Connected to Polymarket ({len(parsed)} markets loaded)"
        else:
            return [], f"⚠️ No markets found for category '{category_slug}'."
    except Exception as e:
        return [], f"❌ Polymarket API Error: {e}"

# ------------------------------------------------------------------
# UI & Controls
# ------------------------------------------------------------------
st.title("⚖️ Prediction Market Arbitrage Scanner")
st.caption("Real-time cross-exchange market scanner for Kalshi and Polymarket.")

# Sidebar Controls
st.sidebar.header("⚙️ Controls")
mode = st.sidebar.radio("Data Mode", ["📡 Live Scanner", "✏️ Manual Custom Odds"])

# Category & Depth Filters
st.sidebar.subheader("🎯 Market Category & Search")
category_map = {
    "All Markets": "all",
    "🎾 Tennis": "tennis",
    "⚽ Sports (General)": "sports",
    "🏛️ Politics": "politics",
    "📈 Crypto & Finance": "crypto",
    "🍿 Pop Culture": "pop-culture"
}
selected_cat_label = st.sidebar.selectbox("Category Filter", list(category_map.keys()))
category_slug = category_map[selected_cat_label]

search_query = st.sidebar.text_input("Keyword Search (e.g., Open, NBA, Fed)", "").strip().lower()
fetch_depth = st.sidebar.slider("Polymarket Fetch Depth (Pages x 100)", min_value=1, max_value=10, value=4)

budget = st.sidebar.number_input("Total Investment ($)", min_value=1.0, value=100.0, step=10.0)
fee_pct = st.sidebar.number_input("Platform Fee on Profit (%)", min_value=0.0, max_value=20.0, value=0.0, step=0.5)

if st.sidebar.button("🔄 Force Refresh API Data"):
    st.cache_data.clear()
    st.rerun()

odds_yes = 7.10
odds_no = 1.15
yes_source = "Manual Entry"
no_source = "Manual Entry"

if mode == "📡 Live Scanner":
    kalshi_list, k_status = fetch_kalshi_markets()
    poly_list, p_status = fetch_polymarket_markets(category_slug=category_slug, pages_to_fetch=fetch_depth)

    # Apply Client-Side Search Keyword Filter
    if search_query:
        kalshi_list = [m for m in kalshi_list if search_query in m['title'].lower()]
        poly_list = [m for m in poly_list if search_query in m['title'].lower()]

    with st.expander("🔍 Connection Diagnostics & Status", expanded=False):
        st.write(f"**Kalshi Status:** {k_status}")
        st.write(f"**Polymarket Status:** {p_status}")

    col_k, col_p = st.columns(2)

    with col_k:
        st.subheader(f"🏛️ Kalshi ({len(kalshi_list)})")
        if kalshi_list:
            k_titles = {f"{m['title'][:60]}... (YES: {m['yes_odds']} | NO: {m['no_odds']})": m for m in kalshi_list}
            selected_k_label = st.selectbox("Select Kalshi Event", list(k_titles.keys()), key="k_select")
            selected_k = k_titles[selected_k_label]
        else:
            st.info("No matching Kalshi markets.")
            selected_k = None

    with col_p:
        st.subheader(f"🟣 Polymarket ({len(poly_list)})")
        if poly_list:
            p_titles = {f"{m['title'][:60]}... (YES: {m['yes_odds']} | NO: {m['no_odds']})": m for m in poly_list}
            selected_p_label = st.selectbox("Select Polymarket Event", list(p_titles.keys()), key="p_select")
            selected_p = p_titles[selected_p_label]
        else:
            st.info("No matching Polymarket markets.")
            selected_p = None

    # Determine Best Odds for Arbitrage Pair
    if selected_k and selected_p:
        if selected_k["yes_odds"] >= selected_p["yes_odds"]:
            odds_yes = selected_k["yes_odds"]
            yes_source = f"Kalshi: {selected_k['title'][:25]}..."
        else:
            odds_yes = selected_p["yes_odds"]
            yes_source = f"Polymarket: {selected_p['title'][:25]}..."

        if selected_k["no_odds"] >= selected_p["no_odds"]:
            odds_no = selected_k["no_odds"]
            no_source = f"Kalshi: {selected_k['title'][:25]}..."
        else:
            odds_no = selected_p["no_odds"]
            no_source = f"Polymarket: {selected_p['title'][:25]}..."
    elif selected_k:
        odds_yes, odds_no = selected_k["yes_odds"], selected_k["no_odds"]
        yes_source = no_source = "Kalshi"
    elif selected_p:
        odds_yes, odds_no = selected_p["yes_odds"], selected_p["no_odds"]
        yes_source = no_source = "Polymarket"

else:
    st.sidebar.subheader("Manual Odds Configuration")
    odds_yes = st.sidebar.number_input("Best YES Odds", min_value=1.01, value=7.10, step=0.05)
    odds_no = st.sidebar.number_input("Best NO Odds", min_value=1.01, value=1.15, step=0.01)

# ------------------------------------------------------------------
# Arbitrage Calculation Engine
# ------------------------------------------------------------------
p_yes = 1.0 / odds_yes
p_no = 1.0 / odds_no
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

# Metric Cards
kpi1, kpi2, kpi3 = st.columns(3)
kpi1.metric("Implied Prob. Sum", f"{implied_sum * 100:.2f}%")
kpi2.metric("Net Profit / Loss", f"${net_profit:+.2f}")
kpi3.metric("ROI", f"{roi:+.2f}%")

# Outcome Banner
if implied_sum < 1.0 and net_profit > 0:
    st.success("🎯 **ARBITRAGE OPPORTUNITY DETECTED:** Guaranteed profit locked across selections.")
elif implied_sum < 1.0 and net_profit <= 0:
    st.warning("⚠️ **ARBITRAGE BEFORE FEES:** Positive raw spread, but exchange fees negate profit.")
else:
    st.error("❌ **NO ARBITRAGE:** Combined market structure results in a net loss.")

# Breakdown Table
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
