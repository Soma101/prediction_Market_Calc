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

    p_yes = yes_ask if 0.01 <= yes_ask <= 0.99 else 0.0
    p_no  = no_ask  if 0.01 <= no_ask <= 0.99  else 0.0

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
# Live API Fetchers
# ------------------------------------------------------------------
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
        return [], f"❌ Connection error to Kalshi Worker: {e}"

@st.cache_data(ttl=120)
def fetch_polymarket_markets(tag_id=None, pages_to_fetch=15, ignore_live=True, min_liquidity=0.0):
    parsed = []
    seen_ids = set()

    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            if tag_id is not None:
                url += f"&tag_id={tag_id}"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if not events: break
                
                for ev in events:
                    event_title = ev.get("title", "")

                    if ignore_live:
                        if ev.get("live") is True or ev.get("isLive") is True:
                            continue

                    markets = ev.get("markets", [])
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids: continue
                        seen_ids.add(m_id)
                        
                        question = m.get("question", "")

                        if ignore_live:
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
                                
                                # Safe parsing for outcomes with null check
                                outcomes_list = ["Option A", "Option B"]
                                if raw_outcomes:
                                    parsed_outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                                    if parsed_outcomes and len(parsed_outcomes) >= 2:
                                        outcomes_list = parsed_outcomes
                                
                                if prices and len(prices) >= 2:
                                    p_yes, p_no = 0.0, 0.0
                                    
                                    # Dynamic Outcome-to-Price Mapping
                                    for outcome_label, price_val in zip(outcomes_list, prices):
                                        label_clean = str(outcome_label).strip().lower()
                                        p_float = float(price_val)
                                        
                                        if any(w in label_clean for w in ["yes", "over"]):
                                            p_yes = p_float
                                        elif any(w in label_clean for w in ["no", "under"]):
                                            p_no = p_float
                                            
                                    # Fallback if outcome names are non-standard
                                    if p_yes == 0.0 and p_no == 0.0:
                                        p_yes, p_no = float(prices[0]), float(prices[1])

                                    if p_yes > 0 and p_no > 0:
                                        parsed.append```python
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
KALSHI_PROXY_BASE = "[https://kalshi-proxy.soahum-golhar.workers.dev](https://kalshi-proxy.soahum-golhar.workers.dev)"
KALSHI_MARKETS_URL = f"{KALSHI_PROXY_BASE}/markets"
KALSHI_STATUS_URL = f"{KALSHI_PROXY_BASE}/exchange/status"

POLYMARKET_BASE_URL = "[https://gamma-api.polymarket.com](https://gamma-api.polymarket.com)"

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

    p_yes = yes_ask if 0.01 <= yes_ask <= 0.99 else 0.0
    p_no  = no_ask  if 0.01 <= no_ask <= 0.99  else 0.0

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
# Live API Fetchers
# ------------------------------------------------------------------
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
        return [], f"❌ Connection error to Kalshi Worker: {e}"

@st.cache_data(ttl=120)
def fetch_polymarket_markets(tag_id=None, pages_to_fetch=15, ignore_live=True, min_liquidity=0.0):
    parsed = []
    seen_ids = set()

    try:
        for page in range(pages_to_fetch):
            offset = page * 100
            url = f"{POLYMARKET_BASE_URL}/events?closed=false&active=true&limit=100&offset={offset}&order=volume24hr&ascending=false"
            if tag_id is not None:
                url += f"&tag_id={tag_id}"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if not events: break
                
                for ev in events:
                    event_title = ev.get("title", "")

                    if ignore_live:
                        if ev.get("live") is True or ev.get("isLive") is True:
                            continue

                    markets = ev.get("markets", [])
                    for m in markets:
                        m_id = str(m.get("id"))
                        if m_id in seen_ids: continue
                        seen_ids.add(m_id)
                        
                        question = m.get("question", "")

                        if ignore_live:
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
                                
                                # Safe parsing for outcomes with null check
                                outcomes_list = ["Option A", "Option B"]
                                if raw_outcomes:
                                    parsed_outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
                                    if parsed_outcomes and len(parsed_outcomes) >= 2:
                                        outcomes_list = parsed_outcomes
                                    
                                if prices and len(prices) >= 2:
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
                            except Exception:
                                continue
            else: break
        return parsed, f"✅ Polymarket: {len(parsed)} liquid markets loaded natively for [{tag_id or 'All'}]"
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
                    "best_odds_yes": round(1.0 / p_
