import streamlit as st
import requests
import json
import difflib
import re
from datetime import datetime
from collections import defaultdict
import pandas as pd

st.set_page_config(
    page_title="Prediction Market Arbitrage Scanner",
    page_icon="⚖️",
    layout="wide"
)

# ------------------------------------------------------------------
# Endpoints & Headers
# ------------------------------------------------------------------
KALSHI_PROXY_BASE = "https://kalshi-proxy.soahum-golhar.workers.dev"
KALSHI_MARKETS_URL = f"{KALSHI_PROXY_BASE}/markets"
KALSHI_STATUS_URL = f"{KALSHI_PROXY_BASE}/exchange/status"

POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

# ------------------------------------------------------------------
# Status & Price Parsing Logic
# ------------------------------------------------------------------
@st.cache_data(ttl=60)
def check_kalshi_status():
    """Ping Kalshi status endpoint through worker proxy."""
    try:
        resp = requests.get(KALSHI_STATUS_URL, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("exchange_active", False), data.get("trading_active", False)
        return True, True
    except Exception:
        return True, True

def parse_kalshi_market_prices(m):
    """Fallback price parser: uses asks first, then last_price / midpoint if asks are empty."""
    if m.get("strike_type") == "custom" or "mve_selected_legs" in m or "mve_collection_ticker" in m:
        return 0.0, 0.0

    def to_float(val):
        if val is None: return 0.0
        try:
            v = float(str(val).strip())
            return v / 100.0 if v > 1.0 else v
        except (ValueError, TypeError):
            return 0.0

    yes_ask = to_float(m.get("yes_ask_dollars") or m.get("yes_ask"))
    no_ask  = to_float(m.get("no_ask_dollars") or m.get("no_ask"))
    last_p  = to_float(m.get("last_price_dollars") or m.get("last_price"))
    yes_bid = to_float(m.get("yes_bid_dollars") or m.get("yes_bid"))

    # Try live ask prices first
    p_yes = yes_ask if 0.01 <= yes_ask <= 0.99 else 0.0
    p_no  = no_ask  if 0.01 <= no_ask <= 0.99  else 0.0

    # Fallback to last price or bid complement if orderbook ask is missing
    if p_yes == 0.0 and 0.01 <= last_p <= 0.99:
        p_yes = last_p
    if p_no == 0.0 and p_yes > 0.0:
        p_no = round(1.0 - p_yes, 4)
    if p_yes == 0.0 and p_no > 0.0:
        p_yes = round(1.0 - p_no, 4)

    if 0.01 <= p_yes <= 0.99 and 0.01 <= p_no <= 0.99:
        return round(p_yes, 4), round(p_no, 4)
    return 0.0, 0.0

# ------------------------------------------------------------------
# Live API Fetchers (Native Category Integration)
# ------------------------------------------------------------------
@st.cache_data(ttl=120)
def fetch_kalshi_markets(category=None, pages_to_fetch=5, ignore_live=True, min_volume=0.0):
    parsed = []
    cursor = ""
    
    live_keywords = [
        "(live)", "[live]", " live ", "in-play", "in play", " live:", 
        "1st half", "2nd half", "first half", "second half", "halftime",
        "1st quarter", "2nd quarter", "3rd quarter", "4th quarter"
    ]

    try:
        for page in range(pages_to_fetch):
            # API-level category filtering drastically reduces fetch times
            url = f"{KALSHI_MARKETS_URL}?limit=1000&status=open"
            if category: 
                url += f"&category={category}"
            if cursor: 
                url += f"&cursor={cursor}"
                
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                raw = resp.json()
                data = raw.get("markets") or raw.get("data") or []

                for m in data:
                    title = m.get("title") or m.get("subtitle") or m.get("ticker") or "Unknown"
                    event_ticker = str(m.get("event_ticker", "")).lower()

                    # Live Game Filter
                    if ignore_live:
                        if m.get("in_play") is True or m.get("is_in_play") is True:
                            continue
                        title_lower = title.lower()
                        if any(kw in title_lower for kw in live_keywords):
                            continue
                        if any(kw in event_ticker for kw in ["-live", "live-", "inplay", "q1", "q2", "q3", "q4", "h1", "h2"]):
                            continue

                    p_yes, p_no = parse_kalshi_market_prices(m)

                    # Normalize Kalshi Volume to USD
                    raw_dollar_vol = m.get("dollar_volume")
                    if raw_dollar_vol is not None:
                        usd_volume = float(raw_dollar_vol)
                    else:
                        contracts = float(m.get("volume") or 0)
                        usd_volume = contracts * (p_yes if p_yes > 0 else 0.5)

                    if min_volume > 0 and usd_volume < min_volume:
                        continue

                    if p_yes > 0 and p_no > 0:
                        parsed.append({
                            "id": m.get("ticker", "N/A"),
                            "title": title,
                            "category": m.get("category", "General"),
                            "ticker": m.get("ticker", ""),
                            "yes_odds": round(1.0 / p_yes, 2),
                            "no_odds": round(1.0 / p_no, 2),
                            "usd_volume": usd_volume,
                            "source": "Kalshi"
                        })
                cursor = raw.get("cursor")
                if not cursor: break
            else: break
                
        return parsed, f"✅ Kalshi: {len(parsed)} markets loaded natively for [{category or 'All'}]"
    except Exception as e:
        return [], f"❌ Connection error to Kalshi Worker: {e}"

@st.cache_data(ttl=120)
def fetch_polymarket_markets(tag_id=None, pages_to_fetch=5, ignore_live=True, min_volume=0.0):
    parsed = []
    seen_ids = set()
    
    live_keywords = [
        "(live)", "[live]", " live ", "in-play", "live prop", 
        "1st half", "2nd half", "first half", "second half", "halftime",
        "1st quarter", "2nd quarter", "3rd quarter", "4th quarter"
    ]

    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            # API-level category (tag) filtering
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            if tag_id is not None:
                url += f"&tag_id={tag_id}"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if not events: break
                
                for ev in events:
                    event_title = ev.get("title", "")
                    event_slug = str(ev.get("slug", "")).lower()

                    if ignore_live:
                        if ev.get("live") is True or ev.get("isLive") is True:
                            continue
                        if any(kw in event_title.lower() for kw in live_keywords):
                            continue
                        if any(kw in event_slug for kw in ["-live-", "live-", "-live", "-q1-", "-q2-", "-q3-", "-q4-", "-h1-", "-h2-", "-liveprop"]):
                            continue

                    markets = ev.get("markets", [])
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids: continue
                        seen_ids.add(m_id)
                        
                        question = m.get("question", "")
                        market_slug = str(m.get("slug", "")).lower()

                        if ignore_live:
                            if m.get("live") is True or m.get("isLive") is True:
                                continue
                            if any(kw in question.lower() for kw in live_keywords):
                                continue
                            if any(kw in market_slug for kw in ["-live-", "live-", "-live", "-q1-", "-q2-", "-q3-", "-q4-", "-h1-", "-h2-"]):
                                continue

                        usd_volume = float(m.get("volume") or ev.get("volume") or 0)
                        liq = float(m.get("liquidity") or ev.get("liquidity") or 0)
                        if min_volume > 0 and max(usd_volume, liq) < min_volume:
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
                                            "title": question or event_title or "Unknown",
                                            "yes_odds": round(1.0 / p_yes, 2),
                                            "no_odds": round(1.0 / p_no, 2),
                                            "usd_volume": usd_volume,
                                            "source": "Polymarket"
                                        })
                            except:
                                continue
            else: break
        return parsed, f"✅ Polymarket: {len(parsed)} markets loaded natively for [{tag_id or 'All'}]"
    except Exception as e:
        return [], f"❌ Polymarket API Error: {e}"

# ------------------------------------------------------------------
# Auto-Matching Arbitrage Engine
# ------------------------------------------------------------------
STOP_WORDS = {
    "will", "happen", "the", "and", "for", "that", "this", "with", "from",
    "have", "more", "than", "before", "after", "does", "what", "when", 
    "where", "who", "which", "yes", "no", "market", "a", "an", "is", "be", 
    "to", "in", "on", "of", "by", "at", "or", "over", "under", "total", "ou",
    "regular", "season", "game", "games", "per", "leader", "leaders", "most", 
    "least", "first", "second", "quarter", "half", "nfl", "nba", "mlb", "nhl", 
    "wta", "atp", "player", "team", "stats", "award", "winner", "champion"
}

CONFLICT_GROUPS = [
    {"trump", "harris", "biden", "desantis", "haley", "newsom", "kennedy", "rfk", "walz", "vance"},
    {"democrat", "republican", "gop", "dems", "democrats", "republicans"},
    {"men", "women", "mens", "womens"}
]

def clean_text_for_match(text):
    text = re.sub(r'(\d+),(\d+)', r'\1\2', text.lower())
    text = re.sub(r'[^a-z0-9\s\.]', ' ', text).strip()
    return re.sub(r'\s+', ' ', text)

def extract_numbers(text):
    clean_text = re.sub(r'(\d+),(\d+)', r'\1\2', text)
    all_nums = set(re.findall(r'\b\d+(?:\.\d+)?\b', clean_text))
    years = {n for n in all_nums if n in {"2024", "2025", "2026", "2027", "2028", "24", "25", "26", "27", "28"}}
    lines = all_nums - years
    return lines, years

def tokenize_title(text):
    clean = clean_text_for_match(text)
    words = clean.split()
    return set(
        w for w in words 
        if len(w) >= 3 
        and not w.isdigit() 
        and w not in STOP_WORDS
    )

def has_entity_conflict(tokens_a, tokens_b):
    for group in CONFLICT_GROUPS:
        a_matches = tokens_a & group
        b_matches = tokens_b & group
        if a_matches and b_matches and not (a_matches & b_matches):
            return True
    return False

def find_best_arbitrage(kalshi_markets, poly_markets, min_similarity=0.65):
    if not kalshi_markets or not poly_markets: return []

    poly_index = defaultdict(list)
    for p in poly_markets:
        p_clean = clean_text_for_match(p['title'])
        p['raw_tokens'] = set(p_clean.split())
        p['tokens'] = tokenize_title(p['title'])
        p['lines'], p['years'] = extract_numbers(p['title'])
        
        for token in p['tokens']:
            poly_index[token].append(p)

    best_pairs = []
    seen_pair_keys = set()
    matched_poly_ids = set()

    for k in kalshi_markets:
        k_clean = clean_text_for_match(k['title'])
        k_raw_tokens = set(k_clean.split())
        k_tokens = tokenize_title(k['title'])
        
        if not k_tokens: continue
        k_lines, k_years = extract_numbers(k['title'])

        candidate_counts = defaultdict(int)
        candidate_objs = {}

        for token in k_tokens:
            for p in poly_index.get(token, []):
                pid = p['id']
                if pid in matched_poly_ids: continue
                candidate_counts[pid] += 1
                candidate_objs[pid] = p

        if not candidate_counts: continue

        best_match = None
        best_score = 0.0

        for pid, count in candidate_counts.items():
            if pid in matched_poly_ids: continue
            p = candidate_objs[pid]

            shared_entities = k_tokens & p['tokens']
            if not shared_entities: continue

            if k_lines and p['lines']:
                k_floats = {float(x) for x in k_lines}
                p_floats = {float(x) for x in p['lines']}
                direct_match = bool(k_floats & p_floats)
                half_point_match = any(abs(kf - pf) <= 0.5 for kf in k_floats for pf in p_floats)
                if not (direct_match or half_point_match): continue

            if k_years and p['years'] and not (k_years & p['years']): continue
            if has_entity_conflict(k_raw_tokens, p['raw_tokens']): continue

            intersection = len(k_tokens & p['tokens'])
            union = len(k_tokens | p['tokens'])
            jaccard_score = intersection / union if union > 0 else 0.0

            p_clean = clean_text_for_match(p['title'])
            seq_score = difflib.SequenceMatcher(None, k_clean, p_clean).ratio()
            
            combined_score = (jaccard_score * 0.60) + (seq_score * 0.40)

            if combined_score > best_score and combined_score >= min_similarity:
                best_score = combined_score
                best_match = p
                if combined_score >= 0.95: break

        if best_match:
            pair_key = f"{k['id']}_{best_match['id']}"
            if pair_key in seen_pair_keys: continue
            
            seen_pair_keys.add(pair_key)
            matched_poly_ids.add(best_match['id'])

            implied_sum_A = (1.0 / k['yes_odds']) + (1.0 / best_match['no_odds'])
            implied_sum_B = (1.0 / best_match['yes_odds']) + (1.0 / k['no_odds'])

            if implied_sum_A < implied_sum_B:
                best_pairs.append({
                    "implied_sum": implied_sum_A,
                    "best_odds_yes": k['yes_odds'],
                    "best_odds_no": best_match['no_odds'],
                    "yes_source": f"Kalshi: {k['title']}",
                    "no_source": f"Polymarket: {best_match['title']}",
                    "similarity": best_score
                })
            else:
                best_pairs.append({
                    "implied_sum": implied_sum_B,
                    "best_odds_yes": best_match['yes_odds'],
                    "best_odds_no": k['no_odds'],
                    "yes_source": f"Polymarket: {best_match['title']}",
                    "no_source": f"Kalshi: {k['title']}",
                    "similarity": best_score
                })

    best_pairs.sort(key=lambda x: x['implied_sum'])
    return best_pairs

def calculate_arbitrage_metrics(odds_yes, odds_no, budget, fee_pct):
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
    return stake_yes, stake_no, net_profit

# ------------------------------------------------------------------
# Streamlit Interface & Layout
# ------------------------------------------------------------------
st.title("⚖ Prediction Market Arbitrage Scanner")
st.caption("Auto-matches cross-platform markets to guarantee mathematically optimal spread setups.")

# Check Exchange Status
exchange_active, trading_active = check_kalshi_status()
if not exchange_active:
    st.error("🚨 **Kalshi Exchange Status: OFFLINE.** Core exchange maintenance in progress.")
elif not trading_active:
    st.warning("⚠️ **Kalshi Exchange Status: TRADING PAUSED.** Exchange active, but trading paused.")
else:
    st.success("🟢 **Kalshi Exchange Status: ACTIVE & TRADING ENABLED.**")

st.sidebar.header("⚙ Controls")
mode = st.sidebar.radio("Data Mode", ["📡 Live Scanner (Auto-Match)", "✏️ Manual Custom Odds"])

st.sidebar.subheader("🎯 Native Market Categories")
st.sidebar.caption("API-level filtering drastically reduces loading times.")

# Unified vs Split Native Category Engine
category_mode = st.sidebar.radio("Category Routing", ["Auto-Mapped (Both Platforms)", "Split Custom (Independent)"])

if category_mode == "Auto-Mapped (Both Platforms)":
    category_maps = {
        "All Markets": {"kalshi": None, "poly_tag": None},
        "🎾 Tennis": {"kalshi": "sports", "poly_tag": 864}, 
        "⚽ Sports (General)": {"kalshi": "sports", "poly_tag": 100639},
        "🏛️ Politics": {"kalshi": "politics", "poly_tag": 2},
        "📈 Crypto & Finance": {"kalshi": "crypto", "poly_tag": 21},
        "🍿 Pop Culture": {"kalshi": "culture", "poly_tag": 596}
    }
    selected_cat_label = st.sidebar.selectbox("Category Filter", list(category_maps.keys()))
    kalshi_cat = category_maps[selected_cat_label]["kalshi"]
    poly_tag = category_maps[selected_cat_label]["poly_tag"]
else:
    kalshi_native_cats = {"All": None, "Politics": "politics", "Crypto": "crypto", "Economics": "economics", "Sports": "sports", "Culture": "culture", "Science": "science"}
    poly_native_tags = {"All": None, "Politics": 2, "Crypto": 21, "Sports": 100639, "Tennis": 864, "Pop Culture": 596, "Science": 133}
    
    k_label = st.sidebar.selectbox("Kalshi API Category", list(kalshi_native_cats.keys()))
    p_label = st.sidebar.selectbox("Polymarket API Tag", list(poly_native_tags.keys()))
    
    kalshi_cat = kalshi_native_cats[k_label]
    poly_tag = poly_native_tags[p_label]

st.sidebar.divider()
arb_only = st.sidebar.checkbox("Only Show Guaranteed Arbitrage (S < 100%)", value=False)
ignore_live = st.sidebar.checkbox("Ignore Live/In-Play Games", value=True)

min_volume = st.sidebar.number_input("Min Volume / Liquidity (USD $)", min_value=0.0, value=100.0, step=100.0)
match_strictness = st.sidebar.slider("Match Strictness (Similarity %)", min_value=50, max_value=100, value=75, step=1) / 100.0

kalshi_pages = st.sidebar.slider("Kalshi Fetch Depth (Pages x 1,000)", min_value=1, max_value=5, value=2)
poly_pages = st.sidebar.slider("Polymarket Fetch Depth (Pages x 100)", min_value=1, max_value=10, value=5)

budget = st.sidebar.number_input("Total Investment ($)", min_value=1.0, value=100.0, step=10.0)
fee_pct = st.sidebar.number_input("Platform Fee on Profit (%)", min_value=0.0, max_value=20.0, value=0.0, step=0.5)

if st.sidebar.button("🗑️ Clear Cache & Reset Data"):
    st.cache_data.clear()
    st.session_state['run_scan'] = False
    st.rerun()

odds_yes = 7.10
odds_no = 1.15
yes_source = "Manual Entry"
no_source = "Manual Entry"

if mode == "📡 Live Scanner (Auto-Match)":
    btn_disabled = not exchange_active or not trading_active
    
    if st.button("🚀 Run Arbitrage Scan", type="primary", use_container_width=True, disabled=btn_disabled):
        st.session_state['run_scan'] = True

    if st.session_state.get('run_scan', False):
        with st.spinner(f"🔄 Fetching markets directly from API categories..."):
            
            # Categories are now passed directly to the fetchers to eliminate downloading unrelated bulk data
            kalshi_list, k_status = fetch_kalshi_markets(category=kalshi_cat, pages_to_fetch=kalshi_pages, ignore_live=ignore_live, min_volume=min_volume)
            poly_list, p_status = fetch_polymarket_markets(tag_id=poly_tag, pages_to_fetch=poly_pages, ignore_live=ignore_live, min_volume=min_volume)

            st.caption(f"**Diagnostic Status:** {k_status} | {p_status}")

            if kalshi_list and poly_list:
                top_opportunities = find_best_arbitrage(kalshi_list, poly_list, min_similarity=match_strictness)
                
                if arb_only:
                    top_opportunities = [op for op in top_opportunities if op['implied_sum'] < 1.0]
                
                if top_opportunities:
                    best_arb = top_opportunities[0]
                    odds_yes = best_arb["best_odds_yes"]
                    odds_no = best_arb["best_odds_no"]
                    yes_source = best_arb["yes_source"]
                    no_source = best_arb["no_source"]
                    
                    with st.expander("✅ View Top Matches & Execution Plans", expanded=True):
                        for i, op in enumerate(top_opportunities[:10], 1): 
                            s_yes, s_no, n_prof = calculate_arbitrage_metrics(op['best_odds_yes'], op['best_odds_no'], budget, fee_pct)
                            
                            if n_prof > 0:
                                profit_badge = f"🟢 **Guaranteed Profit: +${n_prof:.2f}**"
                            else:
                                profit_badge = f"🔴 **Net Loss: ${n_prof:.2f}**"

                            st.markdown(f"#### {i}. Implied Sum: {op['implied_sum']*100:.2f}% | {profit_badge}")
                            st.write(f"- **BUY YES:** {op['yes_source']}")
                            st.write(f"  - **Odds:** {op['best_odds_yes']} | **Stake:** ${s_yes:.2f}")
                            st.write(f"- **BUY NO:** {op['no_source']}")
                            st.write(f"  - **Odds:** {op['best_odds_no']} | **Stake:** ${s_no:.2f}")
                            st.divider()
                else:
                    st.warning("No matching overlapping markets found at the current strictness/volume settings.")
            else:
                st.error("Missing data from one of the platforms. Cannot run cross-matching.")
    else:
        st.info("👆 Click **'Run Arbitrage Scan'** to fetch data and find market mismatches.")
else:
    st.sidebar.subheader("Manual Odds Configuration")
    odds_yes = st.sidebar.number_input("Best YES Odds", min_value=1.01, value=7.10, step=0.05)
    odds_no = st.sidebar.number_input("Best NO Odds", min_value=1.01, value=1.15, step=0.01)

# ------------------------------------------------------------------
# Global Breakdown Calculator
# ------------------------------------------------------------------
st.header("📊 Deep Dive Breakdown (Top Match or Manual Entry)")
stake_yes, stake_no, net_profit = calculate_arbitrage_metrics(odds_yes, odds_no, budget, fee_pct)

p_yes = 1.0 / odds_yes
p_no = 1.0 / odds_no
implied_sum = p_yes + p_no
roi = (net_profit / budget) * 100

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

data = {
    "Outcome": ["YES", "NO"],
    "Best Odds": [f"{odds_yes:.2f}", f"{odds_no:.2f}"],
    "Selected Venue": [yes_source, no_source],
    "Implied Prob.": [f"{p_yes * 100:.2f}%", f"{p_no * 100:.2f}%"],
    "Optimal Stake": [f"${stake_yes:.2f}", f"${stake_no:.2f}"],
    "Gross Payout": [f"${(stake_yes * odds_yes):.2f}", f"${(stake_no * odds_no):.2f}"]
}
st.dataframe(data, hide_index=True, use_container_width=True)
