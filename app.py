import streamlit as st
import requests
import json
import pandas as pd
import difflib

# --- Constants & Endpoints ---
KALSHI_PROXY_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"
HEADERS = {"Accept": "application/json"}

# --- Helper Functions ---
def parse_kalshi_market_prices(m):
    """Safely extracts Kalshi odds. Adjust keys depending on your exact proxy/API tier."""
    # Assuming standard cents format (1 to 99)
    yes_price = m.get("yes_ask", 0) / 100.0
    no_price = m.get("no_ask", 0) / 100.0
    return yes_price, no_price

def matches_kalshi_category(m, kalshi_cat):
    """Filters Kalshi markets post-fetch using its category slug."""
    if kalshi_cat == "all":
        return True
    return str(m.get("category", "")).lower() == kalshi_cat.lower()

def similarity(s1, s2):
    """Calculates string similarity for cross-market matching."""
    if not s1 or not s2: return 0.0
    return difflib.SequenceMatcher(None, s1.lower(), s2.lower()).ratio()

# --- Streamlined API Fetchers ---
@st.cache_data(ttl=120)
def fetch_kalshi_markets(pages_to_fetch=1, ignore_live=True, min_volume=0.0):
    parsed = []
    cursor = ""
    
    try:
        for page in range(pages_to_fetch):
            url = f"{KALSHI_PROXY_URL}?limit=100&status=open&mve_filter=exclude"
            if cursor: url += f"&cursor={cursor}"
                
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                raw = resp.json()
                data = raw.get("markets") or raw.get("data") or []

                for m in data:
                    # Rely purely on API's native in-play flag
                    if ignore_live and (m.get("in_play") is True or m.get("is_in_play") is True):
                        continue

                    # Enforce volume/liquidity floor
                    vol = float(m.get("volume", 0) or 0)
                    liq = float(m.get("liquidity", 0) or 0)
                    if max(vol, liq) < min_volume:
                        continue

                    p_yes, p_no = parse_kalshi_market_prices(m)

                    if p_yes > 0 and p_no > 0:
                        parsed.append({
                            "id": m.get("ticker", "N/A"),
                            "title": m.get("title") or m.get("subtitle") or m.get("ticker") or "Unknown",
                            "category": m.get("category", "General"),
                            "ticker": m.get("ticker", ""),
                            "yes_price": p_yes,
                            "no_price": p_no,
                            "source": "Kalshi"
                        })
                cursor = raw.get("cursor")
                if not cursor: break
            else: break
                
        return parsed, f"✅ Connected to Kalshi ({len(parsed)} active markets loaded)"
    except Exception as e:
        return [], f"❌ Connection error to Kalshi API: {e}"

@st.cache_data(ttl=120)
def fetch_polymarket_markets(tag_id=None, pages_to_fetch=1, ignore_live=True, min_volume=0.0):
    parsed = []
    seen_ids = set()
    
    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            # Order by volume24hr natively sorts highest liquidity to the top
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            
            # Map tag_id instead of string slug
            if tag_id is not None:
                url += f"&tag_id={tag_id}"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if not events: break
                
                for ev in events:
                    # Native event-level live filter
                    if ignore_live and (ev.get("live") is True or ev.get("isLive") is True):
                        continue
                        
                    markets = ev.get("markets", [])
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids: continue
                        seen_ids.add(m_id)
                        
                        # Native market-level live filter
                        if ignore_live and (m.get("live") is True or m.get("isLive") is True):
                            continue

                        # Enforce volume/liquidity floor
                        vol = float(m.get("volume", 0) or 0)
                        liq = float(m.get("liquidity", 0) or 0)
                        if max(vol, liq) < min_volume:
                            continue

                        raw_prices = m.get("outcomePrices")
                        if raw_prices:
                            try:
                                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                                if len(prices) >= 2:
                                    p_yes, p_no = float(prices[0]), float(prices[1])
                                    if p_yes > 0 and p_no > 0:
                                        parsed.append({
                                            "id": m_id,
                                            "title": m.get("question", "") or ev.get("title", "") or "Unknown",
                                            "yes_price": p_yes,
                                            "no_price": p_no,
                                            "source": "Polymarket"
                                        })
                            except:
                                continue
            else: break
        return parsed, f"✅ Connected to Polymarket ({len(parsed)} markets loaded)"
    except Exception as e:
        return [], f"❌ Polymarket API Error: {e}"

# --- Main Streamlit App ---
def main():
    st.set_page_config(page_title="Arbitrage Scanner", layout="wide")
    st.title("📈 Prediction Market Arbitrage Scanner")
    
    st.sidebar.subheader("🎯 Market Configuration")

    # Independent mappings for Kalshi slugs and Polymarket tag IDs
    category_maps = {
        "All Markets": {"kalshi": "all", "poly_tag": None},
        "🎾 Tennis": {"kalshi": "tennis", "poly_tag": 864},
        "⚽ Sports (General)": {"kalshi": "sports", "poly_tag": 100639},
        "🏛️ Politics": {"kalshi": "politics", "poly_tag": 2},
        "📈 Crypto & Finance": {"kalshi": "crypto", "poly_tag": 21},
        "🍿 Pop Culture": {"kalshi": "pop-culture", "poly_tag": 596}
    }

    selected_cat_label = st.sidebar.selectbox("Category Filter", list(category_maps.keys()))
    kalshi_cat = category_maps[selected_cat_label]["kalshi"]
    poly_tag = category_maps[selected_cat_label]["poly_tag"]

    arb_only = st.sidebar.checkbox("Only Show Guaranteed Arbitrage (S < 100%)", value=False)
    ignore_live = st.sidebar.checkbox("Ignore Live/In-Play Games", value=True)

    # New Volume/Liquidity Filter
    min_volume = st.sidebar.number_input(
        "Min Volume / Liquidity ($)", 
        min_value=0.0, 
        value=500.0, 
        step=100.0,
        help="Filters out dead markets that lack the liquidity to actually fill your bets."
    )

    match_strictness = st.sidebar.slider("Match Strictness (Similarity %)", min_value=50, max_value=100, value=75, step=1) / 100.0
    kalshi_pages = st.sidebar.number_input("Kalshi Pagination Depth", 1, 10, 2)
    poly_pages = st.sidebar.number_input("Polymarket Pagination Depth", 1, 10, 2)

    if st.sidebar.button("Run Scanner"):
        st.session_state['run_scan'] = True

    if st.session_state.get('run_scan', False):
        with st.spinner(f"🔄 Fetching and scanning [{selected_cat_label}] markets. Please wait..."):
            
            # Pass min_volume and specific platform categories
            raw_kalshi_list, k_status = fetch_kalshi_markets(
                pages_to_fetch=kalshi_pages, ignore_live=ignore_live, min_volume=min_volume
            )
            poly_list, p_status = fetch_polymarket_markets(
                tag_id=poly_tag, pages_to_fetch=poly_pages, ignore_live=ignore_live, min_volume=min_volume
            )

            # Filter Kalshi strictly using its designated slug
            kalshi_list = [m for m in raw_kalshi_list if matches_kalshi_category(m, kalshi_cat)]
            st.caption(f"**Diagnostic Status:** {k_status} | {p_status}")

            st.divider()

            # Arbitrage Matching Logic
            matches = []
            for k in kalshi_list:
                for p in poly_list:
                    sim_score = similarity(k["title"], p["title"])
                    if sim_score >= match_strictness:
                        
                        # Scenario 1: Buy Yes on Kalshi, No on Polymarket
                        cost_1 = k["yes_price"] + p["no_price"]
                        
                        # Scenario 2: Buy No on Kalshi, Yes on Polymarket
                        cost_2 = k["no_price"] + p["yes_price"]
                        
                        best_cost = min(cost_1, cost_2)
                        
                        # Apply arb filter if enabled
                        if arb_only and best_cost >= 1.0:
                            continue
                            
                        matches.append({
                            "Market Title (Kalshi)": k["title"],
                            "Market Title (Poly)": p["title"],
                            "Similarity": f"{sim_score:.0%}",
                            "K_Yes + P_No": f"${cost_1:.3f}",
                            "K_No + P_Yes": f"${cost_2:.3f}",
                            "Best Arbitrage Cost": f"${best_cost:.3f}",
                            "Guaranteed Profit": f"${1.00 - best_cost:.3f}" if best_cost < 1.0 else "None",
                            "Is Arb?": "✅ YES" if best_cost < 1.0 else "❌ NO"
                        })
            
            if matches:
                df = pd.DataFrame(matches).sort_values(by="Best Arbitrage Cost")
                st.success(f"Found {len(df)} potential overlapping markets.")
                st.dataframe(df, use_container_width=True)
            else:
                st.warning("No matching overlapping markets found at the current strictness/volume settings.")

if __name__ == "__main__":
    main()
