import streamlit as st
import requests
import json
import difflib
import re
from collections import defaultdict
from datetime import datetime, timezone

# API Endpoints
KALSHI_MARKETS_URL = "https://external-api.kalshi.com/trade-api/v2/markets"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"
HEADERS = {"Accept": "application/json"}

# --- HELPER FUNCTIONS ---

def parse_kalshi_market_prices(m):
    # Kalshi returns prices in cents. Example: yes_ask = 53 means $0.53
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    
    p_yes = yes_ask / 100.0 if yes_ask else 0.0
    p_no = no_ask / 100.0 if no_ask else 0.0
    
    return p_yes, p_no

def clean_text_for_match(text):
    text = re.sub(r'[^\w\s\.\-]', '', text.lower())
    return text

def tokenize_title(title):
    words = clean_text_for_match(title).split()
    stop_words = {'over', 'under', 'yes', 'no', 'will', 'the', 'a', 'an', 'to', 'in', 'of', 'for', 'be', 'by'}
    return set(w for w in words if w not in stop_words and len(w) > 2)

def extract_numbers(title):
    lines = set(re.findall(r'\b\d+\.\d+\b', title))
    years = set(re.findall(r'\b202[4-9]\b', title))
    return lines, years

def has_entity_conflict(tokens_a, tokens_b):
    # Place holder if you need specific logic, keeping simple for matching algorithm
    return False

# --- FETCH FUNCTIONS ---

@st.cache_data(ttl=120)
def fetch_kalshi_markets(category=None, pages_to_fetch=10, ignore_live=True, min_liquidity=0.0):
    parsed = []
    cursor = ""
    
    try:
        for page in range(pages_to_fetch):
            url = f"{KALSHI_MARKETS_URL}?limit=1000&status=open"
            if cursor: 
                url += f"&cursor={cursor}"
                
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                raw = resp.json()
                data = raw.get("markets") or raw.get("data") or []

                for m in data:
                    title = m.get("title") or m.get("subtitle") or m.get("ticker") or "Unknown"

                    if ignore_live:
                        # Strict boolean checking, removed all string keyword checks
                        if m.get("in_play") is True or m.get("is_in_play") is True:
                            continue

                    p_yes, p_no = parse_kalshi_market_prices(m)

                    try:
                        vol_24h = float(m.get("volume_24h_fp", 0))
                        vol_total = float(m.get("volume_fp", 0))
                        open_int = float(m.get("open_interest_fp", 0))
                        usd_liquidity = max(vol_24h, vol_total, open_int)
                    except (ValueError, TypeError):
                        usd_liquidity = 0.0

                    if min_liquidity > 0 and usd_liquidity < min_liquidity:
                        continue

                    yes_name = m.get("yes_sub_title", "Yes")
                    no_name = m.get("no_sub_title", "No")

                    if p_yes > 0 and p_no > 0:
                        parsed.append({
                            "id": m.get("ticker", "N/A"),
                            "title": title,
                            "category": m.get("category", "General"),
                            "ticker": m.get("ticker", ""),
                            "p_yes": p_yes,
                            "p_no": p_no,
                            "yes_odds": 1.0 / p_yes,
                            "no_odds": 1.0 / p_no,
                            "yes_name": yes_name,
                            "no_name": no_name,
                            "usd_liquidity": usd_liquidity,
                            "source": "Kalshi"
                        })
                cursor = raw.get("cursor")
                if not cursor: break
            else: break
                
        return parsed, f"✅ Kalshi: {len(parsed)} liquid markets loaded"
    except Exception as e:
        return [], f"❌ Connection error to Kalshi API: {e}"


@st.cache_data(ttl=120)
def fetch_polymarket_markets(tag_id=None, pages_to_fetch=15, ignore_live=True, min_liquidity=0.0):
    parsed = []
    seen_ids = set()
    
    # Format current time to ISO 8601 for start_date_min filter
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            
            if tag_id is not None:
                url += f"&tag_id={tag_id}"
                
            if ignore_live:
                # Appending start_date_min strictly asks Polymarket for markets starting in the future
                url += f"&start_date_min={now_iso}"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if not events: break
                
                for ev in events:
                    event_title = ev.get("title", "")

                    if ignore_live:
                        # Fallback API boolean check
                        if ev.get("live") is True or ev.get("isLive") is True:
                            continue

                    markets = ev.get("markets", [])
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids: continue
                        seen_ids.add(m_id)
                        
                        question = m.get("question", "")

                        if ignore_live:
                            # Fallback API boolean check
                            if m.get("live") is True or m.get("isLive") is True:
                                continue

                        usd_liquidity = float(m.get("liquidity") or 0)
                        
                        if min_liquidity > 0 and usd_liquidity < min_liquidity:
                            continue

                        raw_prices = m.get("outcomePrices")
                        raw_outcomes = m.get("outcomes")
                        
                        if raw_prices:
                            try:
                                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                                outcomes_list = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                                
                                if not outcomes_list or len(outcomes_list) < 2:
                                    outcomes_list = ["Option A", "Option B"]
                                    
                                if len(prices) >= 2:
                                    p_yes, p_no = float(prices[0]), float(prices[1])
                                    if p_yes > 0 and p_no > 0:
                                        parsed.append({
                                            "id": m_id,
                                            "title": question or event_title or "Unknown",
                                            "p_yes": p_yes,
                                            "p_no": p_no,
                                            "yes_odds": 1.0 / p_yes,
                                            "no_odds": 1.0 / p_no,
                                            "yes_name": str(outcomes_list[0]),
                                            "no_name": str(outcomes_list[1]),
                                            "usd_liquidity": usd_liquidity,
                                            "source": "Polymarket"
                                        })
                            except:
                                continue
            else: break
        return parsed, f"✅ Polymarket: {len(parsed)} liquid markets loaded"
    except Exception as e:
        return [], f"❌ Polymarket API Error: {e}"

# --- MATCHING LOGIC ---

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

            k_p_yes = k.get('p_yes', 1.0 / k['yes_odds'])
            k_p_no  = k.get('p_no', 1.0 / k['no_odds'])
            p_p_yes = best_match.get('p_yes', 1.0 / best_match['yes_odds'])
            p_p_no  = best_match.get('p_no', 1.0 / best_match['no_odds'])

            implied_sum_A = k_p_yes + p_p_no
            implied_sum_B = p_p_yes + k_p_no

            if implied_sum_A < implied_sum_B:
                best_pairs.append({
                    "implied_sum": implied_sum_A,
                    "best_odds_yes": round(1.0 / k_p_yes, 2),
                    "best_odds_no": round(1.0 / p_p_no, 2),
                    "yes_source": f"Kalshi: {k['title']} ➡️ [{k.get('yes_name', 'Yes')}]",
                    "no_source": f"Polymarket: {best_match['title']} ➡️ [{best_match.get('no_name', 'No')}]",
                    "similarity": best_score
                })
            else:
                best_pairs.append({
                    "implied_sum": implied_sum_B,
                    "best_odds_yes": round(1.0 / p_p_yes, 2),
                    "best_odds_no": round(1.0 / k_p_no, 2),
                    "yes_source": f"Polymarket: {best_match['title']} ➡️ [{best_match.get('yes_name', 'Yes')}]",
                    "no_source": f"Kalshi: {k['title']} ➡️ [{k.get('no_name', 'No')}]",
                    "similarity": best_score
                })

    best_pairs.sort(key=lambda x: x['implied_sum'])
    return best_pairs

# --- MAIN APP LOGIC ---

def main():
    st.set_page_config(page_title="Arbitrage Scanner", layout="wide")
    st.title("Prediction Market Arbitrage Scanner")

    with st.sidebar:
        st.header("Settings")
        ignore_live = st.checkbox("Ignore Live Markets", value=True)
        min_liquidity = st.number_input("Minimum Liquidity ($)", value=100.0)
        match_strictness = st.slider("Match Strictness", 0.5, 0.9, 0.65)
        
        if st.button("🗑️ Clear Cache & Reset Data"):
            st.cache_data.clear()
            st.rerun()

    if st.button("Scan for Arbitrage", type="primary"):
        with st.spinner("Fetching markets..."):
            k_markets, k_msg = fetch_kalshi_markets(ignore_live=ignore_live, min_liquidity=min_liquidity)
            p_markets, p_msg = fetch_polymarket_markets(ignore_live=ignore_live, min_liquidity=min_liquidity)
            
            st.success(k_msg)
            st.success(p_msg)
            
            if k_markets and p_markets:
                pairs = find_best_arbitrage(k_markets, p_markets, min_similarity=match_strictness)
                
                if pairs:
                    st.subheader(f"Found {len(pairs)} Potential Matches")
                    for idx, pair in enumerate(pairs, 1):
                        profit_margin = (1.0 - pair['implied_sum']) * 100
                        is_arb = pair['implied_sum'] < 1.0
                        
                        color = "🟢" if is_arb else "🔴"
                        st.markdown(f"### {idx}. Implied Sum: {pair['implied_sum'] * 100:.2f}% | {color} Margin: {profit_margin:.2f}%")
                        st.write(f"**BUY YES:** {pair['yes_source']} (Odds: {pair['best_odds_yes']})")
                        st.write(f"**BUY NO:** {pair['no_source']} (Odds: {pair['best_odds_no']})")
                        st.divider()
                else:
                    st.info("No viable cross-market matches found based on your strictness settings.")

if __name__ == "__main__":
    main()
