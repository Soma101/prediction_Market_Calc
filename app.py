import streamlit as st
import requests
import json
import difflib
import re
from collections import defaultdict

st.set_page_config(
    page_title="Prediction Market Arbitrage Scanner",
    page_icon="⚖️",
    layout="wide"
)

# ------------------------------------------------------------------
# Endpoints & Headers
# ------------------------------------------------------------------
KALSHI_PROXY_URL = "https://kalshi-proxy.soahum-golhar.workers.dev/markets"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json"
}

# ------------------------------------------------------------------
# Price Parsing & Category Matching
# ------------------------------------------------------------------
def parse_kalshi_market_prices(m):
    if m.get("strike_type") == "custom" or "mve_selected_legs" in m or "mve_collection_ticker" in m:
        return 0.0, 0.0

    def to_float(val):
        if val is None: return 0.0
        try:
            v = float(str(val).strip())
            return v / 100.0 if v > 1.0 else v
        except (ValueError, TypeError):
            return 0.0

    yes_bid = to_float(m.get("yes_bid_dollars") or m.get("yes_bid"))
    no_bid  = to_float(m.get("no_bid_dollars") or m.get("no_bid"))
    yes_ask = to_float(m.get("yes_ask_dollars") or m.get("yes_ask"))
    no_ask  = to_float(m.get("no_ask_dollars") or m.get("no_ask"))
    last_p  = to_float(m.get("last_price_dollars") or m.get("last_price"))

    if 0 < yes_ask < 1.0: p_yes = yes_ask
    elif 0 < no_bid < 1.0: p_yes = 1.0 - no_bid
    elif 0 < yes_bid < 1.0: p_yes = yes_bid
    elif 0 < last_p < 1.0: p_yes = last_p
    else: p_yes = 0.0

    if 0 < no_ask < 1.0: p_no = no_ask
    elif 0 < yes_bid < 1.0: p_no = 1.0 - yes_bid
    elif 0 < no_bid < 1.0: p_no = no_bid
    elif 0 < last_p < 1.0: p_no = last_p
    else: p_no = 0.0

    if p_yes > 0 and p_no == 0: p_no = round(1.0 - p_yes, 4)
    elif p_no > 0 and p_yes == 0: p_yes = round(1.0 - p_no, 4)

    if 0.01 <= p_yes <= 0.99 and 0.01 <= p_no <= 0.99:
        return round(p_yes, 4), round(p_no, 4)
    return 0.0, 0.0

def matches_kalshi_category(market, cat_slug):
    if cat_slug == "all": return True
    title, category = str(market.get("title", "")).lower(), str(market.get("category", "")).lower()
    ticker, event_ticker = str(market.get("ticker", "")).lower(), str(market.get("event_ticker", "")).lower()

    if cat_slug == "tennis":
        return any(kw in title or kw in ticker for kw in ["tennis", "wta", "atp", "open", "slam", "djokovic", "alcaraz", "swiatek", "sinner"])
    elif cat_slug == "sports":
        return "sport" in category or any(kw in title or kw in ticker or kw in event_ticker for kw in ["sport", "nba", "nfl", "mlb", "nhl", "soccer", "basketball", "ufc", "f1"])
    elif cat_slug == "politics":
        return "politic" in category or any(kw in title or kw in ticker for kw in ["politic", "election", "president", "trump", "biden", "senate", "congress"])
    elif cat_slug == "crypto":
        return any(c in category for c in ["crypto", "economic", "financial"]) or any(kw in title or kw in ticker for kw in ["crypto", "bitcoin", "btc", "ethereum", "fed", "rate", "cpi"])
    elif cat_slug == "pop-culture":
        return any(c in category for c in ["culture", "entertainment"]) or any(kw in title or kw in ticker for kw in ["movie", "oscar", "grammy", "box office", "emmy"])
    return True

# ------------------------------------------------------------------
# Live API Fetchers (Strict In-Play Detection)
# ------------------------------------------------------------------
@st.cache_data(ttl=120)
def fetch_kalshi_markets(pages_to_fetch=1, ignore_live=True):
    parsed = []
    cursor = ""
    page = 0
    try:
        for page in range(pages_to_fetch):
            # Ensure status=open is used here
            url = f"{KALSHI_PROXY_URL}?limit=1000&status=open&mve_filter=exclude"
            if cursor: url += f"&cursor={cursor}"
                
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                raw = resp.json()
                data = raw.get("markets") or raw.get("data") or []

                for m in data:
                    title = m.get("title") or m.get("subtitle") or m.get("ticker") or "Unknown"
                    event_ticker = str(m.get("event_ticker", "")).lower()
                    
                    # Corrected Live Game Filter for Kalshi
                    if ignore_live:
                        # Rely strictly on Kalshi's actual in_play boolean and naming conventions
                        if m.get("in_play") is True or m.get("is_in_play") is True:
                            continue
                        title_lower = title.lower()
                        if any(kw in title_lower for kw in ["(live)", "[live]", " live ", "in-play", "in play", " live:"]):
                            continue
                        if any(kw in event_ticker for kw in ["-live", "live-", "inplay"]):
                            continue

                    p_yes, p_no = parse_kalshi_market_prices(m)

                    if p_yes > 0 and p_no > 0:
                        parsed.append({
                            "id": m.get("ticker", "N/A"),
                            "title": title,
                            "category": m.get("category", "General"),
                            "ticker": m.get("ticker", ""),
                            "yes_odds": round(1.0 / p_yes, 2),
                            "no_odds": round(1.0 / p_no, 2),
                            "source": "Kalshi"
                        })
                cursor = raw.get("cursor")
                if not cursor: break
            else: break
                
        return parsed, f"✅ Connected to Kalshi ({len(parsed)} active markets loaded across {page+1} pages)"
    except Exception as e:
        return [], f"❌ Connection error to Kalshi Worker: {e}"

@st.cache_data(ttl=120)
def fetch_polymarket_markets(category_slug="all", pages_to_fetch=1, ignore_live=True):
    parsed = []
    seen_ids = set()
    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            if category_slug != "all":
                url += f"&tag_slug={category_slug}"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if not events: break
                
                for ev in events:
                    event_title = ev.get("title", "")
                    event_slug = str(ev.get("slug", "")).lower()
                    
                    # Strict Live Game Filter for Polymarket Events
                    if ignore_live:
                        if ev.get("live") is True or ev.get("isLive") is True:
                            continue
                        ev_title_lower = event_title.lower()
                        if any(kw in ev_title_lower for kw in ["(live)", "[live]", " live ", "in-play", "live prop"]):
                            continue
                        # Polymarket live props always use quarter/period/live slug tags
                        if any(kw in event_slug for kw in ["-live-", "live-", "-live", "-q1-", "-q2-", "-q3-", "-q4-", "-h1-", "-h2-", "-liveprop"]):
                            continue

                    markets = ev.get("markets", [])
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids: continue
                        seen_ids.add(m_id)
                        
                        question = m.get("question", "")
                        market_slug = str(m.get("slug", "")).lower()

                        # Strict Live Game Filter for Polymarket Individual Props
                        if ignore_live:
                            if m.get("live") is True or m.get("isLive") is True:
                                continue
                            q_lower = question.lower()
                            if any(kw in q_lower for kw in ["(live)", "[live]", " live ", "in-play"]):
                                continue
                            if any(kw in market_slug for kw in ["-live-", "live-", "-live", "-q1-", "-q2-", "-q3-", "-q4-", "-h1-", "-h2-"]):
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
                                            "source": "Polymarket"
                                        })
                            except:
                                continue
            else: break
        return parsed, f"✅ Connected to Polymarket ({len(parsed)} markets loaded)"
    except Exception as e:
        return [], f"❌ Polymarket API Error: {e}"

# ------------------------------------------------------------------
# Auto-Matching Arbitrage Engine (STRICT SUBJECT & ENTITY MATCHING)
# ------------------------------------------------------------------
STOP_WORDS = {
    "will", "happen", "the", "and", "for", "that", "this", "with", "from",
    "have", "more", "than", "before", "after", "does", "what", "when", 
    "where", "who", "which", "yes", "no", "market", "a", "an", "is", "be", 
    "to", "in", "on", "of", "by", "at", "or", "over", "under", "total", "ou",
    # Generic sports & market structure boilerplate
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
                if pid in matched_poly_ids: 
                    continue
                candidate_counts[pid] += 1
                candidate_objs[pid] = p

        if not candidate_counts: continue

        best_match = None
        best_score = 0.0

        for pid, count in candidate_counts.items():
            if pid in matched_poly_ids: continue
            
            p = candidate_objs[pid]

            shared_entities = k_tokens & p['tokens']
            if not shared_entities:
                continue

            if k_lines and p['lines']:
                k_floats = {float(x) for x in k_lines}
                p_floats = {float(x) for x in p['lines']}
                
                direct_match = bool(k_floats & p_floats)
                half_point_match = any(abs(kf - pf) <= 0.5 for kf in k_floats for pf in p_floats)
                
                if not (direct_match or half_point_match):
                    continue

            if k_years and p['years'] and not (k_years & p['years']):
                continue

            if has_entity_conflict(k_raw_tokens, p['raw_tokens']):
                continue

            intersection = len(k_tokens & p['tokens'])
            union = len(k_tokens | p['tokens'])
            jaccard_score = intersection / union if union > 0 else 0.0

            p_clean = clean_text_for_match(p['title'])
            seq_score = difflib.SequenceMatcher(None, k_clean, p_clean).ratio()
            
            combined_score = (jaccard_score * 0.60) + (seq_score * 0.40)

            if combined_score > best_score and combined_score >= min_similarity:
                best_score = combined_score
                best_match = p
                
                if combined_score >= 0.95:
                    break

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

# ------------------------------------------------------------------
# Math Helper for UI
# ------------------------------------------------------------------
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
# UI & Layout Controls
# ------------------------------------------------------------------
st.title("⚖️ Prediction Market Arbitrage Scanner")
st.caption("Auto-matches cross-platform markets to guarantee mathematically optimal spread setups.")

st.sidebar.header("⚙ Controls")
mode = st.sidebar.radio("Data Mode", ["📡 Live Scanner (Auto-Match)", "✏️ Manual Custom Odds"])

st.sidebar.subheader("🎯 Market Configuration")
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

arb_only = st.sidebar.checkbox("Only Show Guaranteed Arbitrage (S < 100%)", value=False)
ignore_live = st.sidebar.checkbox("Ignore Live/In-Play Games", value=True)

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
    
    if st.button("🚀 Run Arbitrage Scan", type="primary", use_container_width=True):
        st.session_state['run_scan'] = True

    if st.session_state.get('run_scan', False):
        with st.spinner(f"🔄 Fetching and scanning [{selected_cat_label}] markets. Please wait..."):
            raw_kalshi_list, k_status = fetch_kalshi_markets(pages_to_fetch=kalshi_pages, ignore_live=ignore_live)
            poly_list, p_status = fetch_polymarket_markets(category_slug=category_slug, pages_to_fetch=poly_pages, ignore_live=ignore_live)

            kalshi_list = [m for m in raw_kalshi_list if matches_kalshi_category(m, category_slug)]
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
                    if arb_only:
                        st.warning("No pure arbitrage opportunities found at current market prices. Uncheck 'Only Show Guaranteed Arbitrage' to view limit-order candidates, or lower Match Strictness.")
                    else:
                        st.warning("Could not find any overlapping markets. Try lowering the Match Strictness slider or expanding category/depth.")
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
