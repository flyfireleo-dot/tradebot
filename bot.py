"""
⚡ TRADE DESK BOT — FINAL VERSION
Clean. Precise. Professional.
One message = one clear action. 3 seconds to understand.

REQUIRED: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID + GEMINI_API_KEY
OPTIONAL: DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN (Phase 2 option chain)
OPTIONAL: UPSTOX_API_KEY + UPSTOX_ACCESS_TOKEN + AUTO_EXECUTE=true (Phase 5)
OPTIONAL: GOOGLE_SHEET_URL + GOOGLE_CREDS_JSON (memory)
OPTIONAL: GEMINI_MODEL (default: gemini-1.5-flash, swap anytime)
"""

import os, time, json, logging, requests, schedule, pytz, threading, re, math
from datetime import datetime, timedelta
from collections import defaultdict

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID        = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_KEY     = os.environ["GEMINI_API_KEY"]
SHEET_URL      = os.environ.get("GOOGLE_SHEET_URL","")
GOOGLE_CREDS   = os.environ.get("GOOGLE_CREDS_JSON","")
DHAN_CLIENT    = os.environ.get("DHAN_CLIENT_ID","")
DHAN_TOKEN     = os.environ.get("DHAN_ACCESS_TOKEN","")
UPSTOX_KEY     = os.environ.get("UPSTOX_API_KEY","")
UPSTOX_TOKEN   = os.environ.get("UPSTOX_ACCESS_TOKEN","")
AUTO_EXECUTE   = os.environ.get("AUTO_EXECUTE","false").lower()=="true"
MODEL          = os.environ.get("GEMINI_MODEL","gemini-1.5-flash")

IST = pytz.timezone("Asia/Kolkata")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)
P2 = bool(DHAN_CLIENT and DHAN_TOKEN)
P5 = bool(UPSTOX_KEY and UPSTOX_TOKEN and AUTO_EXECUTE)

# ── TECHNICAL INDICATORS ENGINE ───────────────────────────────────────────
# Pure Python — no external libraries needed. Calculates from real price data.

def fetch_ohlcv(symbol, period="3mo", interval="1d"):
    """Fetch real OHLCV candle data from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={period}"
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
        data = r.json()
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        ohlcv = result["indicators"]["quote"][0]
        closes = ohlcv.get("close", [])
        highs = ohlcv.get("high", [])
        lows = ohlcv.get("low", [])
        volumes = ohlcv.get("volume", [])
        # Remove None values
        valid = [(c,h,l,v) for c,h,l,v in zip(closes,highs,lows,volumes) if c and h and l]
        if not valid: return None
        return {
            "closes": [x[0] for x in valid],
            "highs": [x[1] for x in valid],
            "lows": [x[2] for x in valid],
            "volumes": [x[3] for x in valid] if valid[0][3] else [],
            "symbol": symbol
        }
    except Exception as e:
        log.warning(f"OHLCV fetch {symbol}: {e}")
        return None

def calc_ema(closes, period):
    """Calculate Exponential Moving Average."""
    if len(closes) < period: return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

def calc_rsi(closes, period=14):
    """Calculate RSI (0-100)."""
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_macd(closes, fast=12, slow=26, signal=9):
    """Calculate MACD line, signal line, and histogram."""
    if len(closes) < slow + signal: return None, None, None
    ema_fast = []
    ema_slow = []
    k_fast = 2/(fast+1); k_slow = 2/(slow+1)
    ef = sum(closes[:fast])/fast; es = sum(closes[:slow])/slow
    for i, price in enumerate(closes):
        if i >= fast-1: ef = price*k_fast + ef*(1-k_fast); ema_fast.append(ef)
        if i >= slow-1: es = price*k_slow + es*(1-k_slow); ema_slow.append(es)
    macd_line = [f-s for f,s in zip(ema_fast[slow-fast:], ema_slow)]
    k_sig = 2/(signal+1)
    sig = sum(macd_line[:signal])/signal
    for m in macd_line[signal:]:
        sig = m*k_sig + sig*(1-k_sig)
    macd_val = macd_line[-1]
    hist = macd_val - sig
    return round(macd_val, 2), round(sig, 2), round(hist, 2)

def calc_bollinger(closes, period=20, std_mult=2):
    """Calculate Bollinger Bands."""
    if len(closes) < period: return None, None, None
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma)**2 for x in recent) / period
    std = math.sqrt(variance)
    return round(sma + std_mult*std, 2), round(sma, 2), round(sma - std_mult*std, 2)

def calc_atr(highs, lows, closes, period=14):
    """Calculate Average True Range (volatility)."""
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period-1) + tr) / period
    return round(atr, 2)

def find_support_resistance(closes, highs, lows, lookback=20):
    """Find key support and resistance levels."""
    recent_high = max(highs[-lookback:])
    recent_low = min(lows[-lookback:])
    # Simple pivot: 52-week high/low
    yearly_high = max(highs)
    yearly_low = min(lows)
    current = closes[-1]
    # Nearest support (below current)
    support = round(current * 0.97, 0)  # rough 3% below as default
    resistance = round(current * 1.03, 0)
    return {
        "support": recent_low,
        "resistance": recent_high,
        "52w_high": yearly_high,
        "52w_low": yearly_low,
        "near_support": support,
        "near_resistance": resistance
    }

def get_full_technicals(symbol, interval="1d"):
    """
    Calculate ALL indicators for a symbol.
    Returns a dict with every technical reading.
    This is what gets fed to AI — REAL numbers, not guesses.
    """
    data = fetch_ohlcv(symbol, period="1y", interval=interval)
    if not data:
        return None

    closes = data["closes"]
    highs = data["highs"]
    lows = data["lows"]
    volumes = data["volumes"]
    current = closes[-1]

    # Moving averages
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, 200)

    # Trend from EMAs
    trend = "STRONG UPTREND" if current > ema20 and ema20 > ema50 and ema50 > ema200 else \
            "UPTREND" if current > ema20 and ema20 > ema50 else \
            "STRONG DOWNTREND" if current < ema20 and ema20 < ema50 and ema50 < ema200 else \
            "DOWNTREND" if current < ema20 and ema20 < ema50 else "SIDEWAYS"

    # RSI
    rsi = calc_rsi(closes)
    rsi_signal = "OVERSOLD — possible bounce" if rsi and rsi < 35 else \
                 "OVERBOUGHT — be careful" if rsi and rsi > 70 else \
                 "NEUTRAL" if rsi else "N/A"

    # MACD
    macd, signal, hist = calc_macd(closes)
    macd_signal = "BULLISH CROSSOVER" if hist and hist > 0 and macd and macd > signal else \
                  "BEARISH CROSSOVER" if hist and hist < 0 else "NEUTRAL"

    # Bollinger Bands
    bb_upper, bb_mid, bb_lower = calc_bollinger(closes)
    bb_signal = "Near upper band — stretched" if bb_upper and current > bb_upper * 0.99 else \
                "Near lower band — potential bounce" if bb_lower and current < bb_lower * 1.01 else \
                "Within bands — normal"

    # ATR (volatility)
    atr = calc_atr(highs, lows, closes)
    atr_pct = round((atr / current) * 100, 1) if atr else None

    # Volume analysis
    vol_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
    vol_today = volumes[-1] if volumes else None
    vol_signal = "HIGH — institutional activity" if vol_today and vol_avg and vol_today > vol_avg * 1.5 else \
                 "LOW — weak conviction" if vol_today and vol_avg and vol_today < vol_avg * 0.5 else \
                 "NORMAL"

    # Support/Resistance
    sr = find_support_resistance(closes, highs, lows)

    # 52-week position
    wk52_high = sr["52w_high"]
    wk52_low = sr["52w_low"]
    pct_from_high = round(((current - wk52_high) / wk52_high) * 100, 1)
    pct_from_low = round(((current - wk52_low) / wk52_low) * 100, 1)

    # Breakout detection
    is_breakout = current > sr["resistance"] and vol_today and vol_avg and vol_today > vol_avg * 1.3
    is_breakdown = current < sr["support"] and vol_today and vol_avg and vol_today > vol_avg * 1.3

    # Overall signal score (0-10)
    score = 5  # neutral start
    if rsi and rsi < 45: score += 1
    if rsi and rsi < 35: score += 1
    if macd_signal == "BULLISH CROSSOVER": score += 1
    if "UPTREND" in trend: score += 1
    if vol_signal == "HIGH — institutional activity": score += 1
    if is_breakout: score += 1
    if rsi and rsi > 70: score -= 2
    if "DOWNTREND" in trend: score -= 2
    if is_breakdown: score -= 2

    return {
        "symbol": symbol,
        "current": round(current, 2),
        "trend": trend,
        "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "rsi": rsi, "rsi_signal": rsi_signal,
        "macd": macd, "macd_signal": macd_signal,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_signal": bb_signal,
        "atr": atr, "atr_pct": atr_pct,
        "volume_signal": vol_signal,
        "support": sr["support"], "resistance": sr["resistance"],
        "52w_high": wk52_high, "52w_low": wk52_low,
        "pct_from_52w_high": pct_from_high,
        "pct_from_52w_low": pct_from_low,
        "is_breakout": is_breakout, "is_breakdown": is_breakdown,
        "score": min(10, max(0, score)),
    }

def format_technicals(t):
    """Format technical data into clean readable text for AI prompt."""
    if not t: return "Technical data unavailable"
    breakout_str = " ⚡ BREAKOUT DETECTED!" if t.get("is_breakout") else ""
    breakdown_str = " 🔴 BREAKDOWN!" if t.get("is_breakdown") else ""
    return f"""REAL TECHNICAL DATA — {t['symbol']} @ Rs{t['current']}
──────────────────────────────
TREND: {t['trend']}{breakout_str}{breakdown_str}
RSI (14): {t['rsi']} — {t['rsi_signal']}
MACD: {t['macd']} — {t['macd_signal']}
EMA 20: {t['ema20']} | EMA 50: {t['ema50']} | EMA 200: {t['ema200']}
Bollinger: {t['bb_signal']}
Volume: {t['volume_signal']}
ATR: {t['atr']} ({t['atr_pct']}% daily range)
──────────────────────────────
Support: {t['support']} | Resistance: {t['resistance']}
52W High: {t['52w_high']} ({t['pct_from_52w_high']}% away)
52W Low: {t['52w_low']} (+{t['pct_from_52w_low']}% above)
──────────────────────────────
TECHNICAL SCORE: {t['score']}/10"""

def get_nifty_technicals():
    """Get technicals for all 3 indices."""
    results = {}
    for name, sym in [("NIFTY", "^NSEI"), ("SENSEX", "^BSESN"), ("BANKNIFTY", "^NSEBANK")]:
        t = get_full_technicals(sym)
        if t: results[name] = t
        time.sleep(0.5)
    return results

# ── STATE ─────────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE = 75000  # 15% of Rs5L — reduced from 25% (Grok+ChatGPT agreed)
MAX_DAILY_LOSS     = 100000 # Rs1L daily hard stop

S = {
    "cons_losses":0,"daily_loss":0,"blocked":0,
    "halted":False,"halt_reason":"","last_fetch":None,
    "positions":[],"port_delta":0,"port_theta":0,
    "market_state":"UNKNOWN","desk_temp":"NORMAL",
    "last_alert":None,"last_alert_type":"",
    "intraday_nifty":None,"intraday_vix":None,
    "intraday_sensex":None,"intraday_banknifty":None,
    "watchlist":{},
    "day_quality":None,      # 0-20 No-Trade Day Score
    "day_verdict":"",        # TRADE / FLAT / CAUTION
    "fii_cache":None,        # cached FII/DII data (refreshed each morning)
    "fii_cache_date":"",     # date of last FII fetch
    "data_quality":"OK",     # OK / STALE / INCONSISTENT
    "silent_mode":False,     # True = bot goes quiet due to bad data
}
conversation_history = {}
scanner_state = {
    "last_nifty":None,"last_vix":None,
    "last_sensex":None,"last_banknifty":None,
    "alerted_levels":set(),"count":0
}

# ── KILL SWITCH ───────────────────────────────────────────────────────────
def kill_check():
    if S["halted"]: return True,S["halt_reason"]
    if S["silent_mode"]: return True,"Silent mode — data inconsistency"
    if S["cons_losses"]>=3:
        S["halted"]=True;S["halt_reason"]="3 consecutive losses"
        return True,S["halt_reason"]
    if S["daily_loss"]>=MAX_DAILY_LOSS:
        S["halted"]=True;S["halt_reason"]="Daily loss limit Rs1L hit"
        return True,S["halt_reason"]
    if S["blocked"]>=2:
        S["halted"]=True;S["halt_reason"]="2 supervisor blocks"
        return True,S["halt_reason"]
    if S["last_fetch"] and (datetime.now(IST)-S["last_fetch"]).seconds>900:
        return True,"Data stale >15 min — not trading"
    return False,""

def kill_reset():
    S.update({"halted":False,"halt_reason":"","cons_losses":0,
              "daily_loss":0,"blocked":0,"silent_mode":False})

def blackout():
    n=datetime.now(IST);h,m=n.hour,n.minute
    if h==9 and m<20: return "First 5 min of open"
    if h==15 and m>=26: return "Market closing"
    if h<9 or h>=16: return "Market closed"
    if n.weekday()>=5: return "Weekend"
    return None

# ── DATA QUALITY CHECKER ──────────────────────────────────────────────────
def check_data_quality(data, tech):
    """
    Checks for data inconsistencies.
    If something looks wrong → Silent Mode.
    Protects from bad-data trades.
    """
    issues = []
    nifty = data.get("nifty",{}).get("price")
    vix = data.get("vix",{}).get("price")

    # Basic sanity checks
    if not nifty: issues.append("NIFTY price missing")
    if nifty and (nifty < 15000 or nifty > 35000): issues.append(f"NIFTY {nifty} looks wrong")
    if not vix: issues.append("VIX missing")
    if vix and (vix < 5 or vix > 90): issues.append(f"VIX {vix} looks wrong")

    # Technical inconsistency check
    if tech:
        rsi = tech.get("rsi")
        price = tech.get("current")
        ema20 = tech.get("ema20")
        ema200 = tech.get("ema200")
        # RSI overbought but price below all EMAs = contradiction
        if rsi and rsi > 75 and price and ema20 and price < ema20:
            issues.append("RSI/price contradiction")
        # EMA 20 above EMA 200 by more than 20% = data error
        if ema20 and ema200 and ema20 > ema200 * 1.20:
            issues.append("EMA values look miscalculated")

    # Data staleness
    if S["last_fetch"]:
        age = (datetime.now(IST) - S["last_fetch"]).seconds
        if age > 1200: issues.append(f"Data {age//60}min old")

    if len(issues) >= 2:
        S["data_quality"] = "INCONSISTENT"
        S["silent_mode"] = True
        log.warning(f"Silent mode activated: {issues}")
        send(f"⚠️ DATA ISSUE — Going silent today\nReasons: {', '.join(issues)}\nBot will not generate alerts.\nType /resume tomorrow morning.")
        return False

    S["data_quality"] = "OK"
    S["silent_mode"] = False
    return True

# ── DAILY NO-TRADE DAY SCORE ──────────────────────────────────────────────
def compute_day_quality(data, tech):
    """
    0-20 score. Below 14 = FLAT DAY, skip all signals.
    This is the DISCIPLINE LAYER — prevents over-trading.

    Components:
    - GIFT Nifty gap clarity (0-3)
    - VIX level (0-3)
    - Global alignment (0-3)
    - Technical score (0-4)
    - Event risk (0-3)
    - OI/PCR bias (0-4, if available)
    """
    score = 0
    reasons = []

    # 1. GIFT Nifty gap (0-3)
    gift_sig = data.get("gift_signal")
    if gift_sig:
        gap_pct = abs(gift_sig.get("gap_pct", 0))
        if gap_pct > 0.5:
            score += 2; reasons.append(f"Clear GIFT gap {gift_sig.get('gap_pct',0):+.1f}%")
        elif gap_pct > 0.15:
            score += 1; reasons.append("Mild GIFT signal")
        else:
            score += 0; reasons.append("Flat GIFT — no gap direction")
    else:
        score += 1  # neutral

    # 2. VIX level (0-3)
    vix = float(data.get("vix",{}).get("price") or 18)
    if vix < 14:
        score += 3; reasons.append(f"VIX very low {vix:.1f} — easy options")
    elif vix < 18:
        score += 2; reasons.append(f"VIX normal {vix:.1f}")
    elif vix < 22:
        score += 1; reasons.append(f"VIX elevated {vix:.1f}")
    else:
        score += 0; reasons.append(f"VIX high {vix:.1f} — dangerous")

    # 3. Global alignment (0-3)
    alignment = data.get("alignment","Mixed")
    if alignment == "Bullish":
        score += 3; reasons.append("Globals bullish")
    elif alignment == "Bearish":
        score += 2; reasons.append("Globals bearish — directional")
    else:
        score += 1; reasons.append("Globals mixed")

    # 4. Technical score from real indicators (0-4)
    if tech:
        tech_score = tech.get("score", 5)
        if tech_score >= 7:
            score += 4; reasons.append(f"Strong technicals {tech_score}/10")
        elif tech_score >= 5:
            score += 2; reasons.append(f"Neutral technicals {tech_score}/10")
        else:
            score += 0; reasons.append(f"Weak technicals {tech_score}/10")
    else:
        score += 2  # neutral if no data

    # 5. Event risk (0-3) — penalize for major events
    dte = (3 - datetime.now(IST).weekday()) % 7
    if dte == 0:
        score += 1; reasons.append("Expiry day — limited")
    elif dte == 1:
        score += 3; reasons.append("Far from expiry — good")
    else:
        score += 2; reasons.append("Normal day")

    # 6. OI/PCR bias (0-4, if chain available)
    chain = data.get("chain")
    if chain:
        pcr = float(chain.get("pcr", 1.0))
        if 0.7 < pcr < 1.5:
            score += 4; reasons.append(f"PCR {pcr} — healthy")
        elif pcr >= 1.5 or pcr <= 0.5:
            score += 1; reasons.append(f"PCR {pcr} — extreme")
        else:
            score += 2
    else:
        score += 2  # neutral

    S["day_quality"] = score
    if score >= 16:
        verdict = "EXCELLENT — Trade normally"
        S["day_verdict"] = "TRADE"
    elif score >= 14:
        verdict = "GOOD — Trade with normal size"
        S["day_verdict"] = "TRADE"
    elif score >= 11:
        verdict = "AVERAGE — Trade at 50% size only"
        S["day_verdict"] = "CAUTION"
    else:
        verdict = "POOR — FLAT DAY. No new entries."
        S["day_verdict"] = "FLAT"

    return score, verdict, reasons

# ── ATR-BASED STOP LOSS CALCULATOR ───────────────────────────────────────
def calc_atr_sl(tech, current_price, direction="CE", multiplier=1.5):
    """
    Calculate mathematically sound stop loss based on ATR.
    No more AI-guessed stop losses.

    For CE (bullish): SL = current_price - (ATR * multiplier)
    For PE (bearish): SL = current_price + (ATR * multiplier)
    """
    if not tech or not tech.get("atr"): return None, None
    atr = tech["atr"]
    support = tech.get("support", current_price * 0.97)
    resistance = tech.get("resistance", current_price * 1.03)

    if direction == "CE":
        # SL below recent support OR ATR-based, whichever is tighter
        atr_sl = round(current_price - (atr * multiplier), 0)
        sl = max(atr_sl, support - atr * 0.5)  # don't go too wide
    else:
        atr_sl = round(current_price + (atr * multiplier), 0)
        sl = min(atr_sl, resistance + atr * 0.5)

    sl_pct = round(abs(current_price - sl) / current_price * 100, 1)
    return round(sl, 0), sl_pct

# ── RULE-BASED SUPERVISOR (FAST — NO AI NEEDED) ───────────────────────────
def rule_supervisor(alert_text, nifty_price, capital=500000):
    """
    Rule-based math checks first — much faster than AI.
    Only unclear cases get sent to Gemini.
    Returns: (verdict, reason, needs_ai)
    """
    issues = []

    # Extract numbers from alert
    def extract(text, key):
        try:
            match = re.search(key + r"[:\s₹Rs]*([0-9,]+)", text, re.IGNORECASE)
            return float(match.group(1).replace(",","")) if match else None
        except: return None

    premium = extract(alert_text, "PREMIUM")
    lots = extract(alert_text, "LOTS")
    sl_price = extract(alert_text, "SL")
    target = extract(alert_text, "TARGET")
    quality_match = re.search(r"QUALITY[:\s]*([0-9]+)/20", alert_text)
    quality = int(quality_match.group(1)) if quality_match else None
    conf_match = re.search(r"CONFIDENCE[:\s]*([0-9]+)/10", alert_text)
    confidence = int(conf_match.group(1)) if conf_match else None

    # Check 1: Quality gate
    if quality and quality < 14:
        issues.append(f"Quality {quality}/20 below 14")

    # Check 2: Risk calculation
    if premium and lots:
        lot_size = 65  # NIFTY default
        total_risk = premium * lots * lot_size
        if total_risk > MAX_RISK_PER_TRADE:
            issues.append(f"Risk Rs{total_risk:,.0f} exceeds Rs{MAX_RISK_PER_TRADE:,.0f} limit")
        if total_risk > capital * 0.25:
            issues.append(f"Risk {round(total_risk/capital*100)}% of capital — too high")

    # Check 3: Risk-reward ratio
    if premium and sl_price and target:
        risk_per = abs(premium - sl_price)
        reward_per = abs(target - premium)
        if risk_per > 0 and reward_per / risk_per < 1.5:
            issues.append(f"R:R {round(reward_per/risk_per,1)}:1 below 1.5:1")

    # Check 4: Confidence threshold
    if confidence and confidence < 6:
        issues.append(f"Confidence {confidence}/10 too low")

    # Check 5: NO TRADE check
    if "NO TRADE" in alert_text:
        return "✅ APPROVED — No trade signal", "No trade to validate", False

    # Check 6: Day quality gate
    if S["day_quality"] and S["day_quality"] < 11:
        issues.append(f"Day quality {S['day_quality']}/20 — should be FLAT")

    # Verdict
    if not issues:
        return "✅ APPROVED — All checks passed", "", False
    elif len(issues) == 1 and "R:R" in issues[0]:
        return f"⚠️ CAUTION — {issues[0]}", issues[0], False
    else:
        return f"❌ BLOCKED — {' | '.join(issues)}", " | ".join(issues), False

# ── FII/DII DAILY CACHE ───────────────────────────────────────────────────
def fetch_fii_dii():
    """
    Fetch FII/DII data once per morning, cache for the day.
    Structured data, not just web search noise.
    """
    today = datetime.now(IST).strftime("%d %b %Y")
    if S["fii_cache"] and S["fii_cache_date"] == today:
        return S["fii_cache"]  # use cached data

    try:
        result = gemini(
            """You are a financial data extractor. Search web for today's FII and DII data.
Return ONLY this JSON format, nothing else:
{"fii_net": "+2345 Cr", "dii_net": "-890 Cr", "fii_trend": "BUYING", "dii_trend": "SELLING", "net_institutional": "+1455 Cr", "bias": "BULLISH", "source": "NSE/BSE"}
If data not available, return: {"error": "not available"}""",
            f"Search for 'FII DII data India today {today}' and extract the numbers.",
            150
        )
        # Parse JSON
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            fii_data = json.loads(match.group())
            if "error" not in fii_data:
                S["fii_cache"] = fii_data
                S["fii_cache_date"] = today
                log.info(f"FII/DII cached: {fii_data}")
                return fii_data
    except Exception as e:
        log.warning(f"FII fetch: {e}")

    return {"fii_net":"N/A","dii_net":"N/A","bias":"UNKNOWN","source":"unavailable"}

# ── MARKET STATE MACHINE ──────────────────────────────────────────────────
LEGAL_PLAYBOOKS = {
    "TREND_DAY_UP":   ["PB-01","PB-05"],
    "TREND_DAY_DOWN": ["PB-02","PB-05"],
    "MEAN_REVERSION": ["PB-06","PB-05"],
    "EXPIRY_DAY":     ["PB-04"],
    "INSIDE_DAY":     ["PB-06"],
    "VOL_EXPANSION":  [],
    "EVENT_RISK":     ["PB-03"],
    "UNKNOWN":        ["PB-01","PB-02","PB-03","PB-04","PB-05","PB-06"],
}

def classify_state(data):
    vix = float(f(data.get("vix",{}).get("price"))) if data.get("vix",{}) and data["vix"].get("price") else 18
    chg = data.get("nifty",{}).get("change",0) or 0
    dte = (3-datetime.now(IST).weekday())%7
    alignment = data.get("alignment","Mixed")
    if dte==0: state="EXPIRY_DAY"
    elif vix>25: state="VOL_EXPANSION"
    elif abs(chg)>1.0: state="TREND_DAY_UP" if chg>0 else "TREND_DAY_DOWN"
    elif abs(chg)<0.2: state="INSIDE_DAY"
    elif "Bullish" in alignment and chg>0: state="TREND_DAY_UP"
    elif "Bearish" in alignment and chg<0: state="TREND_DAY_DOWN"
    else: state="MEAN_REVERSION"
    S["market_state"]=state
    return state

def arbitrate(global_sig, options_sig, macro_sig, state):
    now=datetime.now(IST)
    dte=(3-now.weekday())%7
    if macro_sig in ["EVENT_TODAY","RBI_TODAY","FED_TODAY"]: w={"global":1.0,"options":1.2,"macro":2.0}
    elif dte==0: w={"global":1.0,"options":2.0,"macro":1.0}
    elif now.hour<10: w={"global":2.0,"options":1.0,"macro":1.0}
    else: w={"global":1.2,"options":1.5,"macro":1.0}
    scores={"BULLISH":0,"BEARISH":0,"NEUTRAL":0}
    for sig,key in [(global_sig,"global"),(options_sig,"options"),(macro_sig,"macro")]:
        wt=w.get(key,1.0)
        if sig=="BULLISH": scores["BULLISH"]+=wt
        elif sig=="BEARISH": scores["BEARISH"]+=wt
        else: scores["NEUTRAL"]+=1.0
    total=sum(scores.values())
    bp=scores["BULLISH"]/total if total else 0
    brp=scores["BEARISH"]/total if total else 0
    if bp>=0.65: bias,conv="BULLISH","HIGH"
    elif brp>=0.65: bias,conv="BEARISH","HIGH"
    elif bp>=0.45: bias,conv="BULLISH","MEDIUM"
    elif brp>=0.45: bias,conv="BEARISH","MEDIUM"
    else: bias,conv="NEUTRAL","LOW"
    structure="SINGLE_LEG" if conv=="HIGH" else "SPREAD_ONLY" if conv=="MEDIUM" else "NO_TRADE"
    return bias,structure,conv

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────
def get_sheet():
    if not GOOGLE_CREDS or not SHEET_URL: return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        c=Credentials.from_service_account_info(json.loads(GOOGLE_CREDS),
            scopes=["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"])
        return gspread.authorize(c).open_by_url(SHEET_URL)
    except Exception as e: log.warning(f"Sheet:{e}"); return None

def read_mem():
    try:
        sh=get_sheet()
        if not sh: return {"lessons":[],"trades":[],"no_trades":[],"patterns":[]}
        return {"lessons":sh.worksheet("Lessons").get_all_records()[-20:],
                "trades":sh.worksheet("Trades").get_all_records()[-50:],
                "no_trades":sh.worksheet("NoTrades").get_all_records()[-10:],
                "patterns":sh.worksheet("Patterns").get_all_records()[-5:]}
    except: return {"lessons":[],"trades":[],"no_trades":[],"patterns":[]}

def wlesson(text,src="AUTO"):
    try:
        sh=get_sheet()
        if not sh: return
        sh.worksheet("Lessons").append_row([datetime.now(IST).strftime("%d %b %Y %H:%M"),text,src])
    except: pass

def wtrade(d):
    try:
        sh=get_sheet()
        if not sh: return
        sh.worksheet("Trades").append_row([d.get("date",""),d.get("atype",""),d.get("state",""),
            d.get("pb",""),d.get("bias",""),d.get("strike",""),d.get("prem",""),
            d.get("lots",""),d.get("risk",""),d.get("conf",""),d.get("qual",""),"PENDING","",""])
    except: pass

def wnotrade(reason):
    try:
        sh=get_sheet()
        if not sh: return
        sh.worksheet("NoTrades").append_row([datetime.now(IST).strftime("%d %b %Y %H:%M"),reason,""])
    except: pass

def update_outcome(pnl_str,lesson=""):
    try:
        sh=get_sheet()
        if not sh: return
        ws=sh.worksheet("Trades");data=ws.get_all_values()
        if len(data)<=1: return
        last=len(data)
        pnl=float(str(pnl_str).replace("₹","").replace(",","").replace("+",""))
        outcome="WIN" if pnl>0 else "LOSS" if pnl<0 else "BREAKEVEN"
        ws.update_cell(last,12,outcome);ws.update_cell(last,13,str(pnl_str));ws.update_cell(last,14,lesson)
        if outcome=="LOSS": S["cons_losses"]+=1;S["daily_loss"]+=abs(pnl)
        else: S["cons_losses"]=0
        if lesson: wlesson(lesson,"TRADE")
    except Exception as e: log.warning(f"outcome:{e}")

# ── PATTERN ANALYSIS ──────────────────────────────────────────────────────
def analyze_patterns(trades):
    closed=[t for t in trades if t.get("Outcome") in ["WIN","LOSS","BREAKEVEN"]]
    if len(closed)<5: return f"Need {5-len(closed)} more trades."
    pb={};ts={}
    for t in closed:
        p=t.get("Playbook","?");won=t.get("Outcome")=="WIN"
        pnl=float(str(t.get("PnL","0")).replace("₹","").replace(",","").replace("+","") or 0)
        if not pb.get(p): pb[p]={"w":0,"t":0,"pnl":0}
        pb[p]["t"]+=1;pb[p]["pnl"]+=pnl
        if won: pb[p]["w"]+=1
        slot="Morning" if any(x in t.get("AlertType","") for x in ["8:30","9:20"]) else "Midday" if "11:30" in t.get("AlertType","") else "Afternoon"
        if not ts.get(slot): ts[slot]={"w":0,"t":0}
        ts[slot]["t"]+=1
        if won: ts[slot]["w"]+=1
    lines=["📊 YOUR EDGE\n─────────────"]
    for pn,s in sorted(pb.items(),key=lambda x:-x[1]["t"]):
        wr=round(s["w"]/s["t"]*100)
        flag="✅" if wr>=60 else "⚠️" if wr>=40 else "❌"
        lines.append(f"{flag} {pn}: {wr}% ({s['w']}/{s['t']}) avg ₹{round(s['pnl']/s['t']):,}")
    lines.append("─────────────")
    for slot,s in ts.items():
        wr=round(s["w"]/s["t"]*100) if s["t"] else 0
        lines.append(f"⏰ {slot}: {wr}%")
    if pb:
        best=max(pb.items(),key=lambda x:x[1]["w"]/max(x[1]["t"],1))[0]
        worst=min(pb.items(),key=lambda x:x[1]["w"]/max(x[1]["t"],1))[0]
        lines.append(f"\nUse more: {best}");lines.append(f"Use less: {worst}")
    return "\n".join(lines)

def adaptive_threshold(trades):
    closed=[t for t in trades if t.get("Outcome") in ["WIN","LOSS"]]
    if len(closed)<20: return 6,"Default (need 20+ trades)"
    wr=sum(1 for t in closed if t.get("Outcome")=="WIN")/len(closed)
    if wr>=0.65: return 6,f"{round(wr*100)}% win — excellent"
    if wr>=0.50: return 7,f"{round(wr*100)}% — raised to 7"
    return 8,f"{round(wr*100)}% — very selective"

# ── PHASE 2: DHAN OPTION CHAIN ────────────────────────────────────────────
def get_chain():
    if not P2: return None
    try:
        today=datetime.now(IST)
        exp=(today+timedelta(days=(3-today.weekday())%7)).strftime("%Y-%m-%d")
        h={"access-token":DHAN_TOKEN,"client-id":DHAN_CLIENT,"Content-Type":"application/json"}
        r=requests.post("https://api.dhan.co/v2/optionchain",headers=h,
                        json={"UnderlyingScrip":13,"UnderlyingSeg":"IDX_I","Expiry":exp},timeout=10)
        data=r.json()
        if not data.get("data"): return None
        spot=data.get("last_price",0);atm=round(spot/50)*50
        chain=data.get("data",{});res={"spot":spot,"atm":atm,"strikes":{}}
        total_ce,total_pe,max_ce,max_pe,ce_wall,pe_wall=0,0,0,0,0,0
        for strike in [atm-100,atm-50,atm,atm+50,atm+100]:
            s=str(int(strike))
            ce=chain.get(f"{s}_CE",{});pe=chain.get(f"{s}_PE",{})
            res["strikes"][strike]={
                "CE":{"ltp":ce.get("last_price",0),"oi":ce.get("open_interest",0),
                      "oi_chg":ce.get("oi_change_pct",0),"iv":ce.get("implied_volatility",0),
                      "bid":ce.get("best_bid_price",0),"ask":ce.get("best_ask_price",0),
                      "spread":ce.get("best_ask_price",0)-ce.get("best_bid_price",0),
                      "delta":ce.get("delta",0),"theta":ce.get("theta",0)},
                "PE":{"ltp":pe.get("last_price",0),"oi":pe.get("open_interest",0),
                      "oi_chg":pe.get("oi_change_pct",0),"iv":pe.get("implied_volatility",0),
                      "delta":pe.get("delta",0),"theta":pe.get("theta",0)},
            }
            total_ce+=ce.get("open_interest",0);total_pe+=pe.get("open_interest",0)
            if ce.get("open_interest",0)>max_ce: max_ce=ce["open_interest"];ce_wall=strike
            if pe.get("open_interest",0)>max_pe: max_pe=pe["open_interest"];pe_wall=strike
        res["pcr"]=round(total_pe/total_ce,2) if total_ce else 0
        res["ce_wall"]=ce_wall;res["pe_wall"]=pe_wall
        res["atm_iv"]=res["strikes"][atm]["CE"]["iv"]
        atm_spread=res["strikes"][atm]["CE"]["spread"]
        atm_ltp=res["strikes"][atm]["CE"]["ltp"]
        res["exec_quality"]=round((1-atm_spread/atm_ltp)*5,1) if atm_ltp else 3
        return res
    except Exception as e: log.warning(f"Chain:{e}"); return None

# ── PHASE 3: PORTFOLIO ────────────────────────────────────────────────────
def portfolio_summary():
    if not S["positions"]: return "No open positions"
    lines=[];total_pnl=0
    for p in S["positions"]:
        pnl=(p["current"]-p["entry"])*p["lots"]*65;total_pnl+=pnl
        lines.append(f"NIFTY {p['strike']} {p['type']} ×{p['lots']} | P&L: {'+' if pnl>=0 else ''}₹{pnl:,.0f}")
    lines.append(f"Total: {'+' if total_pnl>=0 else ''}₹{total_pnl:,.0f} | Δ {S['port_delta']:.2f}")
    return "\n".join(lines)

def port_gate():
    if abs(S["port_delta"])>500: return False,"Portfolio delta too high"
    if len(S["positions"])>=3: return False,"Max 3 positions open"
    return True,""

# ── MARKET DATA ───────────────────────────────────────────────────────────
def yahoo(sym):
    try:
        r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d",
                       headers={"User-Agent":"Mozilla/5.0"},timeout=10)
        meta=r.json()["chart"]["result"][0]["meta"]
        price=meta["regularMarketPrice"];prev=meta.get("chartPreviousClose") or price
        return {"price":round(price,2),"change":round(((price-prev)/prev)*100,2)}
    except: return {"price":None,"change":None}

def get_fx(b,q):
    try:
        r=requests.get(f"https://api.exchangerate-api.com/v4/latest/{b}",timeout=8)
        return round(r.json()["rates"][q],2)
    except: return None

def get_gift_nifty():
    """
    Fetch real GIFT Nifty (NSE IFSC) price.
    GIFT Nifty trades 6:30 AM - 11:30 PM IST almost 24 hours.
    Ticker on Yahoo Finance: NIFTY.NS is Indian market.
    GIFT Nifty futures: try multiple sources.
    """
    # Try Yahoo Finance GIFT Nifty futures symbol
    for sym in ["NIFTYBEES.NS","^NSEI"]:
        try:
            # Try NSE IFSC direct API
            r=requests.get(
                "https://ifsca.gov.in/api/market-data/gift-nifty",
                timeout=5,headers={"User-Agent":"Mozilla/5.0"}
            )
            if r.ok:
                data=r.json()
                price=float(data.get("lastPrice",0))
                prev=float(data.get("prevClose",price))
                if price>0:
                    return {"price":round(price,2),"change":round(((price-prev)/prev)*100,2),"source":"NSE IFSC"}
        except: pass

    # Try investing.com GIFT Nifty
    try:
        r=requests.get(
            "https://api.investing.com/api/financial/latest/future/gift-nifty",
            headers={"User-Agent":"Mozilla/5.0","X-Requested-With":"XMLHttpRequest"},
            timeout=6
        )
        if r.ok:
            d=r.json()
            price=float(d.get("last",0))
            if price>0:
                prev=float(d.get("previous_close",price))
                return {"price":round(price,2),"change":round(((price-prev)/prev)*100,2),"source":"Investing"}
    except: pass

    # Fallback: use Nikkei 225 + S&P weighted estimate
    # This is smarter than just NIFTY+20 — uses overnight global moves
    return None

def interpret_gift(gift, nifty, alignment):
    """
    Interpret GIFT Nifty signal for morning brief.
    Returns gap estimate and bias.
    """
    if not gift or not nifty: return None
    gift_price=gift.get("price",0)
    nifty_price=nifty.get("price",0)
    if not gift_price or not nifty_price: return None
    gap_pts=round(gift_price-nifty_price,2)
    gap_pct=round((gap_pts/nifty_price)*100,2)
    if gap_pct>0.5: bias="GAP UP — CE bias. Wait 9:45 to confirm."
    elif gap_pct<-0.5: bias="GAP DOWN — Watch for bounce. PE risk."
    elif gap_pct>0.15: bias="MILD GAP UP — Wait for opening range."
    elif gap_pct<-0.15: bias="MILD GAP DOWN — Cautious at open."
    else: bias="FLAT OPEN — Range bound expected."
    return {"gap_pts":gap_pts,"gap_pct":gap_pct,"bias":bias}

def fetch_data():
    log.info("Fetching data...")
    d={}
    for k,sym in {"nifty":"^NSEI","sensex":"^BSESN","vix":"^INDIAVIX","banknifty":"^NSEBANK",
                   "dow":"^DJI","sp500":"^GSPC","nasdaq":"^IXIC","nikkei":"^N225",
                   "hsi":"^HSI","crude":"BZ=F","gold":"GC=F"}.items():
        d[k]=yahoo(sym);time.sleep(0.3)
    d["usdinr"]={"price":get_fx("USD","INR"),"change":None}
    d["aedinr"]={"price":get_fx("AED","INR"),"change":None}

    # ── REAL GIFT NIFTY ──
    gift=get_gift_nifty()
    if gift:
        d["gift"]=gift
        log.info(f"GIFT Nifty: {gift['price']} ({gift['change']:+.2f}%) via {gift['source']}")
    elif d.get("nifty",{}).get("price"):
        # Use Gemini web search as fallback for GIFT Nifty
        try:
            gift_search=gemini(
                "You are a market data fetcher. Search web for current GIFT Nifty price right now. Return ONLY: PRICE: XXXXX.XX CHANGE: +/-X.XX% — nothing else.",
                f"Search 'GIFT Nifty price today {datetime.now(IST).strftime('%d %B %Y')}' and return current price and change percentage.",
                60
            )
            # Parse response
            price_match=re.search(r"PRICE:\s*([\d,.]+)",gift_search)
            change_match=re.search(r"CHANGE:\s*([+-]?[\d.]+)%",gift_search)
            if price_match:
                gift_price=float(price_match.group(1).replace(",",""))
                gift_change=float(change_match.group(1)) if change_match else 0
                d["gift"]={"price":gift_price,"change":gift_change,"source":"Gemini Search"}
                log.info(f"GIFT Nifty via Gemini: {gift_price} ({gift_change:+.2f}%)")
            else:
                # Last resort: estimate from overnight global moves
                nifty_prev=d["nifty"]["price"]
                dow_chg=d.get("dow",{}).get("change",0) or 0
                nk_chg=d.get("nikkei",{}).get("change",0) or 0
                hsi_chg=d.get("hsi",{}).get("change",0) or 0
                # Weighted estimate: 40% US + 30% Nikkei + 30% HSI impact on NIFTY
                global_impact=(dow_chg*0.4+nk_chg*0.3+hsi_chg*0.3)*0.6
                gift_est=round(nifty_prev*(1+global_impact/100),2)
                d["gift"]={"price":gift_est,"change":round(global_impact,2),"source":"Estimated"}
                log.info(f"GIFT Nifty estimated: {gift_est} ({global_impact:+.2f}%)")
        except:
            nifty_price=d["nifty"]["price"]
            d["gift"]={"price":round(nifty_price+20,2),"change":d["nifty"]["change"],"source":"Fallback"}

    # GIFT Nifty interpretation
    d["gift_signal"]=interpret_gift(d.get("gift"),d.get("nifty"),d.get("alignment"))

    chgs=[d.get(k,{}).get("change") for k in ["dow","sp500","nasdaq","nikkei","hsi"] if d.get(k,{}).get("change") is not None]
    d["alignment"]="Bullish" if sum(1 for v in chgs if v>0.3)>=3 else "Bearish" if sum(1 for v in chgs if v<-0.3)>=3 else "Mixed"
    d["chain"]=get_chain()
    if d.get("nifty",{}).get("price"): S["intraday_nifty"]=d["nifty"]["price"]
    if d.get("vix",{}).get("price"): S["intraday_vix"]=d["vix"]["price"]
    if d.get("sensex",{}).get("price"): S["intraday_sensex"]=d["sensex"]["price"]
    if d.get("banknifty",{}).get("price"): S["intraday_banknifty"]=d["banknifty"]["price"]
    S["last_fetch"]=datetime.now(IST)
    return d

def f(n,dec=2): return "N/A" if n is None else f"{float(n):,.{dec}f}"
def p(n): return "" if n is None else f"({'+'if n>=0 else''}{n:.2f}%)"

# ── AI ────────────────────────────────────────────────────────────────────
def gemini(system,user,tokens=600,search=True):
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
    payload={"system_instruction":{"parts":[{"text":system}]},
             "contents":[{"parts":[{"text":user}]}],
             "generationConfig":{"maxOutputTokens":tokens,"temperature":0.3}}
    if search: payload["tools"]=[{"google_search":{}}]
    r=requests.post(url,json=payload,timeout=60)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

# ── TELEGRAM ──────────────────────────────────────────────────────────────
def send(text,cid=None,buttons=None):
    cid=cid or CHAT_ID
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0,len(text),4000)]:
        payload={"chat_id":cid,"text":chunk}
        if buttons: payload["reply_markup"]={"inline_keyboard":[[{"text":b["text"],"callback_data":b["data"]} for b in row] for row in buttons]}
        try: requests.post(url,json=payload,timeout=10)
        except: pass

def answer_cb(cb_id,text=""):
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",json={"callback_query_id":cb_id,"text":text},timeout=5)
    except: pass

def typing(cid=None):
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction",json={"chat_id":cid or CHAT_ID,"action":"typing"},timeout=5)
    except: pass

def ext(text,start,end=""):
    try:
        s=text.index(start)+len(start)
        if end and end in text[s:]: return text[s:text.index(end,s)].strip()
        return text[s:s+200].split("\n")[0].strip()
    except: return ""

# ── PROMPTS ───────────────────────────────────────────────────────────────
DESK_PROMPT="""You are DESK CHIEF. Search web for today's market.
Output EXACTLY (no extra text):
TEMPERAMENT: [AGGRESSIVE/NORMAL/DEFENSIVE/FLAT]
REASON: [one line from web]
VIX: [LOW/NORMAL/HIGH/EXTREME]
EVENT_RISK: [NONE/LOW/MEDIUM/HIGH]
MAX_SIZE: [FULL/75%/50%/25%/ZERO]
BIAS: [BUY/SPREAD/SELL/NONE]"""

MAIN_PROMPT="""Elite AI trading desk. NIFTY 50 options.
Capital:₹5,00,000 | Lot:65 | Max risk:25%
Banking 37%|IT 13%|Oil 11%

PATTERNS: RBI cut→+1.2%,CE,78% | Gap up>1%→CE 9:45 | VIX>25→sell only | Expiry Thu→exit 1PM

PLAYBOOKS: PB-01 GAP_UP_CARRY|PB-02 GAP_DOWN_BOUNCE|PB-03 EVENT_PLAY
PB-04 EXPIRY_SCALP|PB-05 OVERNIGHT_CARRY|PB-06 RANGE_FADE

SEARCH web: NIFTY today, events next 5 days, RBI/Fed rates, VIX, FII/DII, crude, USD/INR, Nifty 50 earnings.

STRICT DECISION FLOW:
1. Desk Chief sets max size
2. State machine filters legal playbooks
3. Signal arbitration resolves conflicts
4. Quality gate: must score 14+/20
5. Portfolio risk gate
6. Supervisor validates

CONFLICT RESOLUTION RULES:
- Event day: macro > options > global
- Expiry day: OI walls > everything
- Morning: global/gift > local
- Midday: price action + OI > news
- Strong conflict + event risk = SPREAD or NO TRADE

CLEAN OUTPUT FORMAT (nothing extra):

IF TRADE:
SIGNAL: [BULLISH/BEARISH]
PLAYBOOK: [PB-0X]
STRIKE: NIFTY [XXXXX] [CE/PE] [EXPIRY DATE]
PREMIUM: ₹[XX]
LOTS: [X]
SL: ₹[XX]
TARGET: ₹[XX]
ENTRY: [time] IST
EXIT BY: [time/date]
TOTAL MONEY: ₹[XXXXX]
QUALITY: [XX]/20
CONFIDENCE: [X]/10
RATIONALE: [one line why NOW]
5-DAY RADAR:
[Day Date]: [Event or Clear]
[Day Date]: [Event or Clear]
[Day Date]: [Event or Clear]
[Day Date]: [Event or Clear]
[Day Date]: [Event or Clear]
LEARNED: [one insight]

IF NO TRADE:
NO TRADE
REASON: [one specific line]
WATCH FOR: [exact trigger]
5-DAY RADAR:
[same format]
LEARNED: [one insight]"""

SUPERVISOR_PROMPT="""Strict supervisor. One line output only.
RULES: Lot=65|Max=₹1,25,000|Strike=×50|Target:SL≥1.5|Quality≥14
CHECK math, capital, strike validity, ratio, quality.
OUTPUT one of:
✅ APPROVED — math checks out
⚠️ CAUTION — [specific issue]
❌ BLOCKED — [specific reason]"""

ASSISTANT_PROMPT="""You are Leo's personal trading assistant — smart, friendly, and straight to the point.

You talk like a knowledgeable friend who understands Indian markets deeply. Not robotic. Not emotional. Just clear, warm, and useful.

TRADER PROFILE:
- Capital: Rs5,00,000 in NIFTY options
- Based in India/UAE
- Wants clear answers he can act on

HOW TO TALK:
✅ "Crude fell $2 overnight — that's actually good news for NIFTY tomorrow. Expect a mild gap up."
✅ "Iran situation is getting worse. Markets don't like uncertainty, so be careful with fresh positions."
✅ "Honestly, with VIX at 22, buying options is expensive right now. Better to wait."
✅ "Good question! Here's what's happening..."

❌ Never say "I am unable to provide" 
❌ Never say "As an AI language model"
❌ Never give empty corporate answers
❌ Don't be overly formal

RESPONSE LENGTHS:
- Short question = 3-5 lines, conversational
- Complex question = 8-12 lines with key points
- Deep analysis = full breakdown with sources

Always search web for latest prices and news before answering.
If you find something important, mention the source briefly.
End with one clear takeaway or action if relevant."""

# ── CONVERSATION MEMORY ────────────────────────────────────────────────────
def get_history(cid): return conversation_history.get(str(cid),[])
def add_history(cid,role,text):
    cid=str(cid)
    if cid not in conversation_history: conversation_history[cid]=[]
    conversation_history[cid].append({"role":role,"parts":[{"text":text}]})
    conversation_history[cid]=conversation_history[cid][-16:]
def clear_history(cid): conversation_history[str(cid)]=[]

# ── PERSONAL ASSISTANT ────────────────────────────────────────────────────
def handle_chat(text,cid,data,mem,depth="default"):
    typing(cid)
    add_history(cid,"user",text)
    closed=[t for t in mem["trades"] if t.get("Outcome") in ["WIN","LOSS","BREAKEVEN"]]
    wr=round(sum(1 for t in closed if t.get("Outcome")=="WIN")/len(closed)*100) if closed else 0
    tokens={"default":400,"why":700,"deep":1200}.get(depth,400)

    context=f"""Current market context for Leo:
NIFTY: {f(data.get('nifty',{}).get('price'))} | VIX: {f(data.get('vix',{}).get('price'))} | Global: {data.get('alignment')}
Time: {datetime.now(IST).strftime('%A %d %B %Y %I:%M %p IST')}
Leo's stats: {wr}% win rate | {len(closed)} trades done

Leo's question: {text}

Search the web if needed to give an accurate, current answer."""

    # Try with web search first
    try:
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
        payload={
            "system_instruction":{"parts":[{"text":ASSISTANT_PROMPT}]},
            "contents":[{"role":"user","parts":[{"text":context}]}],
            "tools":[{"google_search":{}}],
            "generationConfig":{"maxOutputTokens":tokens,"temperature":0.5}
        }
        r=requests.post(url,json=payload,timeout=45)
        r.raise_for_status()
        result=r.json()
        # Extract text from response — handle multiple part types
        reply=""
        for part in result.get("candidates",[{}])[0].get("content",{}).get("parts",[]):
            if "text" in part:
                reply+=part["text"]
        if reply.strip():
            add_history(cid,"model",reply)
            return reply
        raise Exception("Empty response")
    except Exception as e:
        log.warning(f"Chat with search failed: {e}")

    # Fallback: try without web search tool
    try:
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
        payload={
            "system_instruction":{"parts":[{"text":ASSISTANT_PROMPT}]},
            "contents":[{"role":"user","parts":[{"text":context}]}],
            "generationConfig":{"maxOutputTokens":tokens,"temperature":0.5}
        }
        r=requests.post(url,json=payload,timeout=30)
        r.raise_for_status()
        result=r.json()
        reply=""
        for part in result.get("candidates",[{}])[0].get("content",{}).get("parts",[]):
            if "text" in part:
                reply+=part["text"]
        if reply.strip():
            add_history(cid,"model",reply)
            return reply
        raise Exception("Empty response")
    except Exception as e:
        log.error(f"Chat fallback failed: {e}")

    # Last resort: use gemini helper
    try:
        reply=gemini(ASSISTANT_PROMPT, context, tokens, search=False)
        add_history(cid,"model",reply)
        return reply
    except Exception as e:
        log.error(f"All chat attempts failed: {e}")
        return "Sorry, I'm having trouble connecting right now. Try again in a moment, or use /alert for a full market analysis!"

# ── MORNING BRIEF ─────────────────────────────────────────────────────────
def morning_brief():
    log.info("Morning brief + day quality score...")
    try:
        data=fetch_data()
        gift=data.get("gift",{})
        gift_price=gift.get("price","N/A")
        gift_chg=gift.get("change",0) or 0
        gift_src=gift.get("source","")
        gift_sig=data.get("gift_signal")
        gift_line=f"GIFT NIFTY: {f(gift_price)} ({gift_chg:+.2f}%) [{gift_src}]"
        bias_line=gift_sig["bias"] if gift_sig else "Gap reading unavailable"
        gap_pts=gift_sig["gap_pts"] if gift_sig else 0

        # Fetch FII/DII (cached for the day)
        fii=fetch_fii_dii()
        fii_line=f"FII: {fii.get('fii_net','N/A')} | DII: {fii.get('dii_net','N/A')} | {fii.get('bias','UNKNOWN')}"

        # Calculate real technicals for day quality
        nifty_tech=get_full_technicals("^NSEI")

        # Data quality check first
        data_ok=check_data_quality(data, nifty_tech)
        if not data_ok: return  # silent mode activated

        # Compute daily no-trade score
        day_score, day_verdict, day_reasons=compute_day_quality(data, nifty_tech)

        # Day verdict emoji
        if S["day_verdict"]=="TRADE": dv_emoji="✅"
        elif S["day_verdict"]=="CAUTION": dv_emoji="⚠️"
        else: dv_emoji="🚫"

        brief=gemini(
            "Market briefer. Search web NOW. Output EXACTLY 2 lines:\nWATCH: [one thing to watch today]\nPLAN: [one action for 9:20 AM]",
            f"Today {datetime.now(IST).strftime('%A %d %B %Y')}. GIFT:{gift_price} ({gift_chg:+.2f}%). FII:{fii.get('fii_net')}. Search key market news.",
            80
        )

        send(f"""🌅 MORNING BRIEF — {datetime.now(IST).strftime('%a %d %b')}
─────────────────────
{gift_line}
Gap: {gap_pts:+.0f} pts | {bias_line}
{fii_line}
─────────────────────
DAY QUALITY: {day_score}/20
{dv_emoji} Verdict: {day_verdict}
─────────────────────
{brief}
─────────────────────
Full alert 8:30 AM ⏰""")

        # If FLAT day, send explicit warning
        if S["day_verdict"]=="FLAT":
            send(f"""🚫 FLAT DAY — STAND ASIDE
─────────────────────
Today's score: {day_score}/20 (need 14+)
Reasons: {' | '.join(day_reasons[:3])}

No new trades today.
Protect your capital.
Back tomorrow morning.""")

    except Exception as e:
        log.warning(f"Morning brief:{e}")

# ── SWING STOCK ENGINE ────────────────────────────────────────────────────
SWING_PROMPT = """You are an elite swing trading analyst for Indian stocks (NSE/BSE).
You think like a professional fund manager. You are honest, precise, and avoid hype.

WHAT PROS USE TO FIND SWING STOCKS:
1. TECHNICAL: RSI (14), MACD, 20/50/200 EMA, Volume breakout, Bollinger Bands
2. FUNDAMENTAL: Debt/Equity < 0.5, ROE > 15%, Promoter holding > 45%, PEG ratio
3. INSTITUTIONAL: FII/DII buying patterns (3-5 consecutive days = strong signal)
4. MOMENTUM: 52-week high breakouts with volume, consolidation breakouts
5. SECTOR: Sector rotation, government policy tailwinds, sector FII flow
6. RISK: Beta > 1 for volatility, ADTV > 5 lakh shares for liquidity
7. NEWS: Earnings momentum, management guidance, sector-specific news

SEARCH THE WEB for:
- Current FII/DII data today
- Top performing sectors this week
- Any upcoming results/events for stocks
- Current news that could move stocks
- Analyst upgrades/downgrades this week

STRICT RULES TO AVOID WRONG PICKS:
- Never recommend stocks with upcoming court cases or regulatory issues
- Never recommend stocks with promoter pledging > 50%
- Never recommend near earnings if direction unclear
- Always verify news from at least 2 sources before including
- Mark confidence clearly: HIGH/MEDIUM/LOW

OUTPUT FORMAT (exactly this, for each stock):

SWING PICK #X — [STOCK NAME] ([TICKER])
─────────────────────────────
WHY NOW: [2 lines — specific reason TODAY]
TECHNICALS: RSI:[X] | Trend:[UP/DOWN] | Volume:[HIGH/NORMAL]
FUNDAMENTALS: [2 key metrics]
NEWS CHECK: [any recent news verified from web]
SECTOR: [sector + is it in favour?]
ENTRY ZONE: Rs[X] – Rs[X]
TARGET: Rs[X] ([X]% gain in [X] days)
STOP LOSS: Rs[X] ([X]% below entry)
RISK-REWARD: [X]:1
CONFIDENCE: [HIGH/MEDIUM/LOW]
RISK: [one specific risk to watch]
─────────────────────────────

After all picks:
MARKET CONTEXT: [2 lines on why now is good/bad for swing trades]
AVOID: [sectors or themes to avoid this week with reason]"""

def get_swing_recommendations(cid, data):
    """Generate swing stock recommendations using real technicals + web search."""
    typing(cid)
    send("Calculating real indicators and searching web... give me 45 seconds!", cid)
    typing(cid)

    now = datetime.now(IST)
    nifty = f(data.get("nifty",{}).get("price"))
    vix = f(data.get("vix",{}).get("price"))

    # Get real NIFTY technicals first — market context
    nifty_tech = get_full_technicals("^NSEI")
    market_tech = format_technicals(nifty_tech) if nifty_tech else "NIFTY technicals unavailable"

    prompt = f"""Find the best 4 swing trading stocks on NSE right now for 3-10 day trades.

VERIFIED MARKET DATA (calculated from real prices):
Date: {now.strftime('%A %d %B %Y')}
NIFTY: {nifty} | VIX: {vix}

{market_tech}

MANDATORY WEB SEARCHES (do all before answering):
1. "FII DII data India today {now.strftime('%B %Y')}"
2. "top NSE breakout stocks this week"
3. "India stock market news today"
4. "NSE 52 week high stocks volume"
5. "best swing trade stocks India {now.strftime('%B %Y')}"

FOR EACH STOCK YOU RECOMMEND — you must verify:
✅ Check if it's in the news for good reasons
✅ Check if sector is in favour right now
✅ Must have high volume confirmation
✅ Must have clear support level for stop loss
❌ Reject any stock with bad news, promoter issues, upcoming unclear results

ANTI-HALLUCINATION RULES:
- Only recommend stocks you can verify with web search
- If you can't find recent data on a stock — skip it
- Be honest about confidence level
- State your source for each pick

Output exactly 4 stocks in the format below, or fewer if market conditions are bad."""

    try:
        result = gemini(SWING_PROMPT, prompt, 1400)
        return result
    except Exception as e:
        log.error(f"Swing engine: {e}")
        return "Having trouble right now. Try again in a minute!"

# ── WATCHLIST SYSTEM ──────────────────────────────────────────────────────
def add_to_watchlist(symbol, entry_price, notes="", cid=None):
    """Add stock to watchlist for 24/7 monitoring."""
    symbol = symbol.upper().strip()
    S["watchlist"][symbol] = {
        "entry": entry_price,
        "current": entry_price,
        "notes": notes,
        "added": datetime.now(IST).strftime("%d %b %Y %H:%M"),
        "alerted": False,
        "high": entry_price,
        "pnl_pct": 0,
        "status": "HOLDING"
    }
    log.info(f"Added {symbol} to watchlist at Rs{entry_price}")

def remove_from_watchlist(symbol):
    symbol = symbol.upper().strip()
    if symbol in S["watchlist"]:
        del S["watchlist"][symbol]
        return True
    return False

def get_watchlist_prices():
    """Fetch current prices for all watchlist stocks."""
    if not S["watchlist"]: return
    for symbol in list(S["watchlist"].keys()):
        try:
            # Try Yahoo Finance with .NS suffix for NSE stocks
            data = yahoo(f"{symbol}.NS")
            if data.get("price"):
                S["watchlist"][symbol]["current"] = data["price"]
                entry = S["watchlist"][symbol]["entry"]
                pnl_pct = ((data["price"] - entry) / entry) * 100
                S["watchlist"][symbol]["pnl_pct"] = round(pnl_pct, 2)
                if data["price"] > S["watchlist"][symbol].get("high", entry):
                    S["watchlist"][symbol]["high"] = data["price"]
        except Exception as e:
            log.warning(f"Watchlist price {symbol}: {e}")

WATCHLIST_MONITOR_PROMPT = """You are monitoring a stock position for a trader.
Analyze the situation and give a clear, direct recommendation.
Be like a trusted friend who knows markets well — honest, not alarmist, not overconfident.

Keep response under 8 lines. End with one clear action: HOLD / ADD MORE / REDUCE / EXIT."""

def analyze_watchlist_stock(symbol, stock_data, market_data):
    """AI analysis of a watchlist stock position."""
    entry = stock_data["entry"]
    current = stock_data["current"]
    pnl_pct = stock_data["pnl_pct"]
    high = stock_data.get("high", entry)
    notes = stock_data.get("notes","")

    prompt = f"""Watchlist stock: {symbol}
Entry price: Rs{entry}
Current price: Rs{current}
P&L: {pnl_pct:+.1f}%
Highest reached: Rs{high}
Notes: {notes}
Market: NIFTY {f(market_data.get('nifty',{}).get('price'))} | VIX {f(market_data.get('vix',{}).get('price'))} | {market_data.get('alignment')}
Date: {datetime.now(IST).strftime('%A %d %B %Y %I:%M %p IST')}

Search web for:
1. Latest news on {symbol} stock
2. Any analyst updates or target changes
3. Sector news that affects {symbol}

Based on current price, news, and market conditions — what should the trader do?"""

    try:
        return gemini(WATCHLIST_MONITOR_PROMPT, prompt, 300)
    except:
        return f"{symbol}: Price Rs{current} ({pnl_pct:+.1f}%). Unable to analyze right now."

def check_watchlist_alerts(data):
    """Check all watchlist stocks for alert conditions."""
    if not S["watchlist"]: return
    get_watchlist_prices()

    for symbol, stock in S["watchlist"].items():
        entry = stock["entry"]
        current = stock["current"]
        pnl_pct = stock["pnl_pct"]
        key = f"watch_{symbol}_{int(abs(pnl_pct))}"

        # Big profit — suggest taking some off
        if pnl_pct >= 8 and key not in scanner_state["alerted_levels"]:
            scanner_state["alerted_levels"].add(key)
            send(f"""🎯 PROFIT ALERT — {symbol}
─────────────────────
Your stock is up {pnl_pct:+.1f}%!
Entry: Rs{entry} → Now: Rs{current}
Consider booking partial profits.
/analyze {symbol} for full analysis""")

        # Stop loss territory
        elif pnl_pct <= -5 and key not in scanner_state["alerted_levels"]:
            scanner_state["alerted_levels"].add(key)
            send(f"""⚠️ STOP LOSS WARNING — {symbol}
─────────────────────
Down {pnl_pct:+.1f}% from your entry.
Entry: Rs{entry} → Now: Rs{current}
Review your stop loss level!
/analyze {symbol} for full analysis""")

        # Very big loss
        elif pnl_pct <= -10 and f"exit_{symbol}" not in scanner_state["alerted_levels"]:
            scanner_state["alerted_levels"].add(f"exit_{symbol}")
            send(f"""🚨 EXIT ALERT — {symbol}
─────────────────────
Down {pnl_pct:+.1f}% — this is serious!
Entry: Rs{entry} → Now: Rs{current}
STRONGLY consider exiting to protect capital.
Type: /analyze {symbol}""")

def show_watchlist():
    """Format watchlist for display."""
    if not S["watchlist"]:
        return "Your watchlist is empty.\nAdd stocks: /watch RELIANCE 2850\nOr ask me for swing picks!"
    lines = ["📋 YOUR WATCHLIST\n─────────────────────"]
    total_pnl = 0
    for symbol, s in S["watchlist"].items():
        pnl = s["pnl_pct"]
        emoji = "🟢" if pnl > 0 else "🔴" if pnl < -3 else "🟡"
        lines.append(f"{emoji} {symbol}: Rs{s['current']} ({pnl:+.1f}%)")
        lines.append(f"   Entry Rs{s['entry']} | Added {s['added']}")
        if s.get("notes"): lines.append(f"   📝 {s['notes']}")
        total_pnl += pnl
    lines.append("─────────────────────")
    lines.append(f"Average P&L: {total_pnl/len(S['watchlist']):+.1f}%")
    lines.append("\nCommands: /analyze SYMBOL | /remove SYMBOL")
    return "\n".join(lines)

# ── MULTI-INDEX SUPPORT ────────────────────────────────────────────────────
def get_index_summary(data):
    """Clean summary of all 3 indices."""
    nifty = data.get("nifty",{})
    sensex = data.get("sensex",{})
    bn = data.get("banknifty",{})
    vix = data.get("vix",{})

    n_chg = nifty.get("change",0) or 0
    s_chg = sensex.get("change",0) or 0
    b_chg = bn.get("change",0) or 0

    # Determine overall mood
    avg_chg = (n_chg + s_chg + b_chg) / 3
    mood = "Bullish 🟢" if avg_chg > 0.3 else "Bearish 🔴" if avg_chg < -0.3 else "Neutral 🟡"

    return f"""📊 ALL INDICES
─────────────────────
NIFTY 50:    {f(nifty.get('price'))} ({n_chg:+.2f}%)
SENSEX:      {f(sensex.get('price'),0)} ({s_chg:+.2f}%)
BANK NIFTY:  {f(bn.get('price'))} ({b_chg:+.2f}%)
VIX:         {f(vix.get('price'))}
─────────────────────
Mood: {mood}"""

# ── INTRADAY SCANNER (UPDATED WITH ALL INDICES + WATCHLIST) ──────────────
def intraday_scan():
    now=datetime.now(IST);h,m=now.hour,now.minute
    if now.weekday()>=5:return
    halted,_=kill_check()
    if halted:return
    try:
        nifty=S.get("intraday_nifty")
        sensex=S.get("intraday_sensex")
        banknifty=S.get("intraday_banknifty")
        vix=S.get("intraday_vix")
        alerts=[]
        market_open=9<=h<15

        if market_open and nifty:
            # NIFTY big move
            if scanner_state["last_nifty"]:
                move_pct=abs(nifty-scanner_state["last_nifty"])/scanner_state["last_nifty"]*100
                if move_pct>0.5:
                    direction="up" if nifty>scanner_state["last_nifty"] else "down"
                    key=f"nifty_move_{int(nifty/100)*100}"
                    if key not in scanner_state["alerted_levels"]:
                        scanner_state["alerted_levels"].add(key)
                        alerts.append(f"⚡ NIFTY MOVE — {now.strftime('%I:%M %p')}\nNIFTY moved {direction} {move_pct:.1f}% to {nifty:.0f}\nAction: {'CE entry possible' if direction=='up' else 'PE bounce watch'}\nConfidence: 6/10")

            # BANK NIFTY leading signal
            if banknifty and scanner_state.get("last_banknifty") and scanner_state["last_nifty"]:
                bn_move=(banknifty-scanner_state["last_banknifty"])/scanner_state["last_banknifty"]*100
                n_move=(nifty-scanner_state["last_nifty"])/scanner_state["last_nifty"]*100
                if bn_move>1.0 and n_move<0.3:
                    key=f"bn_lead_{now.hour}"
                    if key not in scanner_state["alerted_levels"]:
                        scanner_state["alerted_levels"].add(key)
                        alerts.append(f"🏦 BANK NIFTY LEADING — {now.strftime('%I:%M %p')}\nBank NIFTY +{bn_move:.1f}% while NIFTY flat.\nBanking strength often pulls NIFTY up soon.\nWatch for NIFTY CE or Bank NIFTY CE opportunity.")

            # VIX spike
            if vix and scanner_state["last_vix"]:
                vix_move=vix-scanner_state["last_vix"]
                if vix_move>3 and f"vix_{int(vix)}" not in scanner_state["alerted_levels"]:
                    scanner_state["alerted_levels"].add(f"vix_{int(vix)}")
                    alerts.append(f"⚠️ VIX SPIKE — {now.strftime('%I:%M %p')}\nVIX jumped +{vix_move:.1f} to {vix:.1f}\nOptions getting expensive. Exit long premium if any.")

            # SL warning for options
            for pos in S["positions"]:
                sl=pos.get("sl",0)
                if sl and pos["current"] < sl*1.15 and f"sl_{pos['strike']}" not in scanner_state["alerted_levels"]:
                    scanner_state["alerted_levels"].add(f"sl_{pos['strike']}")
                    alerts.append(f"⚠️ SL WARNING — {pos['strike']} {pos['type']}\nApproaching stop loss Rs{sl}. Consider exiting.")

        # Watchlist monitoring — runs every hour, all day
        if S["watchlist"] and scanner_state["count"]%4==0:
            check_watchlist_alerts({"nifty":{"price":nifty},"sensex":{"price":sensex},"banknifty":{"price":banknifty},"vix":{"price":vix},"alignment":"Mixed"})

        for a in alerts: send(a)
        if nifty: scanner_state["last_nifty"]=nifty
        if vix: scanner_state["last_vix"]=vix
        scanner_state["last_sensex"]=sensex
        scanner_state["last_banknifty"]=banknifty
        scanner_state["count"]+=1
        if scanner_state["count"]%32==0: scanner_state["alerted_levels"]=set()
    except Exception as e: log.warning(f"Scanner:{e}")

# ── MAIN ALERT ────────────────────────────────────────────────────────────
def run_alert(atype):
    now_str=datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    log.info(f"=== {atype} ===")
    halted,reason=kill_check()
    if halted: send(f"⛔ TRADING HALTED\n{reason}\n\nDO NOTHING.\n/resume after review."); return
    bo=blackout()
    if bo: log.info(f"Blackout:{bo}"); return
    ok,pr=port_gate()
    if not ok: send(f"⚠️ {pr}\nNo new trade."); return

    # ── DAY QUALITY GATE ──────────────────────────────────────────────────
    # If we already know today is FLAT, don't even generate alerts
    if S["day_verdict"] == "FLAT":
        send(f"🚫 FLAT DAY ({S['day_quality']}/20) — Standing aside.\nNo alerts until tomorrow morning brief.")
        return
    if S["silent_mode"]:
        send("⚠️ Silent mode active — data issues detected.\nNot generating alerts today.")
        return
    try:
        typing();data=fetch_data();mem=read_mem()
        thresh,thresh_note=adaptive_threshold(mem["trades"])
        state=classify_state(data)
        legal_pbs=LEGAL_PLAYBOOKS.get(state,[])
        chain=data.get("chain")
        chain_ctx=""
        if chain:
            atm=chain["atm"];atm_ce=chain["strikes"].get(atm,{}).get("CE",{})
            chain_ctx=f"\nOPTION CHAIN (live): Spot:{chain['spot']} ATM:{atm} PCR:{chain['pcr']} IV:{chain.get('atm_iv',0):.1f}%\nCE Wall:{chain['ce_wall']} | PE Wall:{chain['pe_wall']} | Exec quality:{chain.get('exec_quality',3)}/5\nATM CE:₹{atm_ce.get('ltp',0):.0f} Δ:{atm_ce.get('delta',0):.3f} θ:₹{(atm_ce.get('theta',0)*65):.0f}/day"
        lessons_ctx="\n".join([f"- {l.get('Lesson','')}" for l in mem["lessons"][-3:]]) if mem["lessons"] else ""
        next5=[(datetime.now(IST)+timedelta(days=i)).strftime("%A %d %B %Y") for i in range(1,6)]

        # ── REAL TECHNICAL INDICATORS (calculated from live price data) ──
        log.info("Calculating real technicals...")
        tech_ctx = ""
        try:
            nifty_tech = get_full_technicals("^NSEI")
            bn_tech = get_full_technicals("^NSEBANK")
            if nifty_tech:
                tech_ctx = f"\n{format_technicals(nifty_tech)}"
            if bn_tech:
                tech_ctx += f"\n\nBANK NIFTY TECHNICALS:\nTrend:{bn_tech['trend']} | RSI:{bn_tech['rsi']} | MACD:{bn_tech['macd_signal']} | Score:{bn_tech['score']}/10"
            log.info(f"Technicals: NIFTY RSI={nifty_tech.get('rsi') if nifty_tech else 'N/A'}")
        except Exception as e:
            log.warning(f"Technicals failed: {e}")
            tech_ctx = "\nTechnicals: calculation failed this run"

        desk=gemini(DESK_PROMPT,f"Today {datetime.now(IST).strftime('%A %d %B %Y')}.\nNIFTY:{f(data.get('nifty',{}).get('price'))} VIX:{f(data.get('vix',{}).get('price'))} Global:{data.get('alignment')}\nState:{state} Legal playbooks:{legal_pbs}",200)
        S["desk_temp"]="FLAT" if "FLAT" in desk else "DEFENSIVE" if "DEFENSIVE" in desk else "AGGRESSIVE" if "AGGRESSIVE" in desk else "NORMAL"
        if "FLAT" in desk or "ZERO" in desk:
            msg=f"🚫 NO TRADE\n─────────────────────\nReason: Desk Chief: FLAT day\nWatch: Tomorrow morning brief"
            send(msg);wnotrade("Desk Chief FLAT");return
        gift=data.get("gift",{})
        gift_sig=data.get("gift_signal")
        gift_ctx=""
        if gift.get("price"):
            gift_ctx=f"\nGIFT NIFTY: {f(gift.get('price'))} ({(gift.get('change') or 0):+.2f}%) [{gift.get('source','')}]"
            if gift_sig:
                gift_ctx+=f"\nGap estimate: {gift_sig['gap_pts']:+.0f} pts ({gift_sig['gap_pct']:+.2f}%)"
                gift_ctx+=f"\nGap signal: {gift_sig['bias']}"
        fii=fetch_fii_dii()
        fii_ctx=f"\nFII: {fii.get('fii_net','N/A')} | DII: {fii.get('dii_net','N/A')} | Institutional bias: {fii.get('bias','N/A')}"
        day_ctx=f"\nDAY QUALITY: {S['day_quality']}/20 ({S['day_verdict']})" if S["day_quality"] else ""
        prompt=f"""Generate {atype} alert.
TODAY: {datetime.now(IST).strftime('%A %d %B %Y %I:%M %p')} IST
DESK: {desk}
STATE: {state} | LEGAL PLAYBOOKS: {legal_pbs or 'NONE'}
THRESHOLD: {thresh}/10 ({thresh_note})
NEXT 5 DAYS: {next5}{day_ctx}
NIFTY:{f(data.get('nifty',{}).get('price'))} {p(data.get('nifty',{}).get('change'))} | VIX:{f(data.get('vix',{}).get('price'))}{gift_ctx}
SENSEX:{f(data.get('sensex',{}).get('price'))} | BankNifty:{f(data.get('banknifty',{}).get('price'))}{fii_ctx}
Dow:{f(data.get('dow',{}).get('price'),0)} | S&P:{f(data.get('sp500',{}).get('price'))} | Global:{data.get('alignment')}
Crude:${f(data.get('crude',{}).get('price'))} | USD/INR:₹{f(data.get('usdinr',{}).get('price'))}{chain_ctx}{tech_ctx}
Portfolio:{portfolio_summary()}
Lessons:{lessons_ctx or 'none'}
RULE: Use ONLY the real data above. Do not invent indicator values.
Only recommend trade if technicals AND day quality confirm. Search web for events."""
        alert=gemini(MAIN_PROMPT,prompt,700)

        # ── RULE-BASED SUPERVISOR (fast, no AI) ──
        sup_verdict, sup_reason, needs_ai = rule_supervisor(alert, data.get("nifty",{}).get("price",24000))
        if needs_ai:
            # Only call Gemini for borderline cases
            sup_verdict = gemini(SUPERVISOR_PROMPT, f"Validate:\n{alert}\nNIFTY:{f(data.get('nifty',{}).get('price'))} Capital:₹5,00,000", 80, search=False)

        # Append ATR-based SL suggestion if trade signal
        atr_note = ""
        if "SIGNAL:" in alert and "NO TRADE" not in alert and nifty_tech:
            direction = "CE" if "CE" in alert else "PE"
            atr_sl, atr_sl_pct = calc_atr_sl(nifty_tech, nifty_tech.get("current", 24000), direction)
            if atr_sl:
                atr_note = f"\n📐 ATR-based SL: {atr_sl:.0f} ({atr_sl_pct}% from entry)"
        if "❌ BLOCKED" in sup_verdict or "BLOCKED" in sup_verdict.upper():
            S["blocked"]+=1;send(f"{alert}\n\n{sup_verdict}\n\n❌ DO NOT TRADE — Fix issues and regenerate.{atr_note}"); return
        S["blocked"]=0;S["last_alert"]=alert;S["last_alert_type"]=atype
        is_trade="SIGNAL:" in alert and "NO TRADE" not in alert
        buttons=[[{"text":"✅ Executed","data":"btn_executed"},{"text":"⏭ Skip","data":"btn_skip"}],
                 [{"text":"❓ Why","data":"btn_why"},{"text":"📊 Deep","data":"btn_deep"}]] if is_trade else \
                [[{"text":"🔔 Alert if setup forms","data":"btn_watch"}]]
        send(f"{alert}\n\n{sup_verdict}{atr_note}",buttons=buttons)
        S["last_alert"]=alert
        if is_trade:
            wtrade({"date":datetime.now(IST).strftime("%d %b %Y"),"atype":atype,"state":state,
                    "pb":ext(alert,"PLAYBOOK:","STRIKE:"),"bias":ext(alert,"SIGNAL:","PLAYBOOK:"),
                    "strike":ext(alert,"STRIKE:","PREMIUM:"),"prem":ext(alert,"PREMIUM:","LOTS:"),
                    "lots":ext(alert,"LOTS:","SL:"),"risk":ext(alert,"TOTAL MONEY:","QUALITY:"),
                    "conf":ext(alert,"CONFIDENCE:","RATIONALE:"),"qual":ext(alert,"QUALITY:","CONFIDENCE:")})
        elif "NO TRADE" in alert:
            wnotrade(ext(alert,"REASON:","WATCH FOR:"))
        if "LEARNED:" in alert:
            l=ext(alert,"LEARNED:","")
            if l: wlesson(l[:200],f"ALERT_{atype[:5]}")
        log.info(f"=== {atype} DONE ===")
    except Exception as e: log.error(f"Alert:{e}"); send(f"⚠️ Error @ {now_str}\n{e}")

# ── WEEKLY REPORT ─────────────────────────────────────────────────────────
def weekly_report():
    log.info("Weekly report...")
    try:
        mem=read_mem();trades=mem["trades"]
        closed=[t for t in trades if t.get("Outcome") in ["WIN","LOSS","BREAKEVEN"]]
        wins=[t for t in closed if t.get("Outcome")=="WIN"]
        pnl=sum(float(str(t.get("PnL","0")).replace("₹","").replace(",","").replace("+","") or 0) for t in closed)
        wr=round(len(wins)/len(closed)*100) if closed else 0
        recent_trades = [t.get("Playbook","") + " " + t.get("Outcome","") for t in closed[-5:]]
        report=gemini(
            "Trading analyst. Direct. Under 200 words.",
            "Weekly trading report. Trades:" + str(len(closed)) + " WR:" + str(wr) + "% P&L:Rs" + str(round(pnl)) + "\nRecent:" + str(recent_trades) + "\nSearch web for next week top 3 events.\nProvide: what worked, what failed, next week events, one rule. Short.",
            350
        )
        send(f"""📊 WEEKLY — {datetime.now(IST).strftime('%d %b')}
─────────────────────
{len(closed)} trades | {wr}% WR | {'+'if pnl>=0 else''}₹{abs(pnl):,.0f}
─────────────────────
{report}""")
        if len(trades)>=10: send(analyze_patterns(trades))
    except Exception as e: send(f"⚠️ Weekly error:{e}")

# ── COMMANDS ──────────────────────────────────────────────────────────────
def handle_commands():
    offset=0
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                           params={"offset":offset,"timeout":30},timeout=35)
            for upd in r.json().get("result",[]):
                offset=upd["update_id"]+1
                # Inline button callbacks
                if "callback_query" in upd:
                    cb=upd["callback_query"];cid=cb["message"]["chat"]["id"];data=cb["data"]
                    answer_cb(cb["id"])
                    if data=="btn_executed":
                        send("✅ Logged as executed.\nReply /win [amount] [lesson] when closed.",cid)
                    elif data=="btn_skip":
                        send("⏭ Skipped.\nCapital protected.",cid);wnotrade("User skipped")
                    elif data=="btn_why":
                        if S["last_alert"]:
                            typing(cid);d=fetch_data();m=read_mem()
                            reply=handle_chat(f"Why was this trade recommended? {S['last_alert'][:200]}",cid,d,m,"why")
                            send(reply,cid)
                    elif data=="btn_deep":
                        if S["last_alert"]:
                            typing(cid);send("🔍 Deep analysis...",cid)
                            d=fetch_data();m=read_mem()
                            reply=handle_chat(f"Full deep analysis: {S['last_alert'][:200]}",cid,d,m,"deep")
                            send(reply,cid)
                    elif data=="btn_watch":
                        send("🔔 I'll alert if a clear setup forms.",cid)
                    continue
                # Text messages
                msg=upd.get("message",{});text=msg.get("text","").strip();cid=msg.get("chat",{}).get("id")
                if not text or not cid: continue

                if text in ["/start","/help"]:
                    send("""⚡ TRADE DESK — YOUR PERSONAL MARKET ASSISTANT

Just type anything naturally and I'll help!
I search the web and give you real answers.

📈 TRADING ALERTS:
/alert — Generate full NIFTY/SENSEX/BankNifty alert
/status — Live market snapshot (all indices)
/indices — NIFTY + SENSEX + Bank NIFTY summary
/events — Next 5 days calendar

💹 SWING TRADING:
/swing — Get 4 best swing stock picks right now
  (searches web, checks fundamentals + technicals)
/watch RELIANCE 2850 — Add stock to watchlist
/watchlist — See all your monitored stocks
/analyze RELIANCE — Deep analysis of any stock
/remove RELIANCE — Remove from watchlist

📊 YOUR PERFORMANCE:
/risk — Risk dashboard
/pnl — P&L summary
/patterns — Your trading edge analysis
/learn — Recent lessons

⚙️ SETTINGS:
/clear — Clear chat memory
/resume — Reset kill switch
/report — Weekly performance report

LOG TRADES:
/win 12400 lesson
/loss 8200 lesson

⏰ AUTO ALERTS (IST Mon-Fri):
🌅 7:00 Morning Brief
📊 8:30 | 🔔 9:20 | 📊 11:30 | ⚡ 14:00 | 🌙 15:15
📊 Fri 16:00 Weekly Report
⚡ Scanner every 15 min (all 3 indices + your watchlist)

Just type anything to chat! Examples:
"What's happening with Iran and markets?"
"Should I buy Reliance now?"
"Is Bank Nifty looking bullish?"
"Which stocks are good for swing trade this week?" """,cid)

                elif text=="/alert":
                    send("⟳ Generating...",cid);typing(cid);run_alert("Manual")

                elif text=="/status":
                    typing(cid);d=fetch_data();halted,hr=kill_check()
                    chain=d.get("chain")
                    pcr=f"PCR:{chain['pcr']} | " if chain else ""
                    send(f"""📊 LIVE — {datetime.now(IST).strftime('%d %b %I:%M %p')}
─────────────────────
NIFTY:{f(d.get('nifty',{}).get('price'))} {p(d.get('nifty',{}).get('change'))}
VIX:{f(d.get('vix',{}).get('price'))} | {pcr}Global:{d.get('alignment')}
Crude:${f(d.get('crude',{}).get('price'))} | USD/INR:₹{f(d.get('usdinr',{}).get('price'))}
State:{S['market_state']} | Desk:{S['desk_temp']}
─────────────────────
{'⛔ HALTED — '+hr if halted else '✅ Active'}""",cid)

                elif text=="/events":
                    typing(cid)
                    now=datetime.now(IST)
                    next5=[(now+timedelta(days=i)).strftime("%A %d %B %Y") for i in range(1,6)]
                    result=gemini("Event scanner. Search web. One line per event.",
                        f"Indian market events next 5 days:\n"+"\n".join(next5)+"\nCheck: NSE holidays, NIFTY expiry, RBI, US Fed, Nifty 50 earnings. Short list.",150)
                    send(f"📅 NEXT 5 DAYS\n─────────────────────\n{result}",cid)

                elif text=="/risk":
                    mem=read_mem();streak=0
                    for t in reversed(mem["trades"]):
                        if t.get("Outcome")=="LOSS": streak+=1
                        else: break
                    mode="⛔ STOP" if streak>=3 else "⚠️ 50% size" if streak>=2 else "⚡ 75% size" if streak>=1 else "✅ Normal"
                    halted,hr=kill_check();thresh,tnote=adaptive_threshold(mem["trades"])
                    send(f"""⚡ RISK
─────────────────────
{mode} | Streak:{streak} losses
Daily loss:₹{S['daily_loss']:,.0f}
Min confidence:{thresh}/10
Positions:{len(S['positions'])} open
─────────────────────
{'⛔ '+hr if halted else '✅ Active'}
/resume to reset""",cid)

                elif text=="/pnl":
                    mem=read_mem()
                    closed=[t for t in mem["trades"] if t.get("Outcome") in ["WIN","LOSS","BREAKEVEN"]]
                    wins_c=[t for t in closed if t.get("Outcome")=="WIN"]
                    pnl=sum(float(str(t.get("PnL","0")).replace("₹","").replace(",","").replace("+","") or 0) for t in closed)
                    wr=round(len(wins_c)/len(closed)*100) if closed else 0
                    send(f"""📊 P&L
─────────────────────
{len(closed)} trades | ✅{len(wins_c)} | ❌{len(closed)-len(wins_c)}
Win rate:{wr}%
P&L: {'+'if pnl>=0 else''}₹{abs(pnl):,.0f}
Capital:₹{5_00_000+pnl:,.0f}""",cid)

                elif text=="/patterns":
                    typing(cid);mem=read_mem();send(analyze_patterns(mem["trades"]),cid)

                elif text=="/learn":
                    mem=read_mem();l=mem["lessons"][-6:]
                    if not l: send("No lessons yet.",cid)
                    else: send("🧠 LESSONS\n─────────────────────\n"+"\n".join([f"• {x.get('Lesson','')[:120]}" for x in reversed(l)]),cid)

                elif text=="/report":
                    send("⟳ Generating...",cid);weekly_report()

                elif text=="/clear":
                    clear_history(cid);send("🗑️ Chat memory cleared.",cid)

                elif text=="/swing" or text.lower().startswith("/swing"):
                    send("Searching web for best swing opportunities right now! This takes about 30 seconds...", cid)
                    typing(cid)
                    d=fetch_data()
                    result=get_swing_recommendations(cid, d)
                    send(result, cid)
                    send("If you like any of these, tell me!\nExample: 'I bought RELIANCE at 2850'\nOr: '/watch RELIANCE 2850'\nI'll monitor it 24/7 and alert you!", cid)

                elif text.lower().startswith("/watch "):
                    parts=text.split()
                    if len(parts)>=3:
                        symbol=parts[1].upper()
                        try:
                            entry=float(parts[2])
                            notes=" ".join(parts[3:]) if len(parts)>3 else ""
                            add_to_watchlist(symbol, entry, notes, cid)
                            send(f"✅ Added {symbol} to your watchlist!\nEntry: Rs{entry}\nI'll monitor this 24/7 and alert you if:\n→ Up 8%+ (take profits!)\n→ Down 5% (review stop loss)\n→ Down 10% (exit alert!)\n→ Important news breaks\n\n/watchlist to see all | /analyze {symbol} for analysis", cid)
                        except:
                            send("Format: /watch SYMBOL PRICE\nExample: /watch RELIANCE 2850", cid)
                    else:
                        send("Format: /watch SYMBOL PRICE\nExample: /watch RELIANCE 2850\nExample: /watch TCS 3500 bought for swing trade", cid)

                elif text=="/watchlist":
                    send(show_watchlist(), cid)

                elif text.lower().startswith("/remove "):
                    symbol=text.split()[1].upper() if len(text.split())>1 else ""
                    if symbol and remove_from_watchlist(symbol):
                        send(f"✅ Removed {symbol} from watchlist.", cid)
                    else:
                        send(f"Stock not found in watchlist. /watchlist to see what's there.", cid)

                elif text.lower().startswith("/analyze "):
                    symbol=text.split()[1].upper() if len(text.split())>1 else ""
                    if not symbol:
                        send("Format: /analyze RELIANCE", cid); continue
                    typing(cid)
                    send(f"Calculating real indicators for {symbol}... checking news too!", cid)
                    d=fetch_data()
                    # Get real technicals
                    tech=get_full_technicals(f"{symbol}.NS")
                    tech_str=format_technicals(tech) if tech else f"Could not fetch price data for {symbol}"
                    stock_data=S["watchlist"].get(symbol, {"entry":0,"current":tech.get("current",0) if tech else 0,"pnl_pct":0,"notes":""})
                    # Build enhanced prompt
                    entry=stock_data.get("entry",0)
                    current=tech.get("current",0) if tech else 0
                    pnl_pct=round(((current-entry)/entry)*100,1) if entry and current else 0
                    prompt=f"""Analyze {symbol} stock for swing trade decision.

{tech_str}

{"Position: Entered at Rs"+str(entry)+" | Current Rs"+str(current)+" | P&L: "+str(pnl_pct)+"%"  if entry else "Not in portfolio yet."}
Notes: {stock_data.get('notes','')}

Market: NIFTY {f(d.get('nifty',{}).get('price'))} | VIX {f(d.get('vix',{}).get('price'))}
Date: {datetime.now(IST).strftime('%A %d %B %Y %I:%M %p IST')}

Search web for:
1. Latest {symbol} news (last 7 days)
2. Any analyst upgrades or target changes
3. Sector news relevant to {symbol}
4. Any management updates or results

Based on REAL indicators above + current news — give clear recommendation."""
                    result=gemini(WATCHLIST_MONITOR_PROMPT, prompt, 400)
                    send(f"📊 ANALYSIS — {symbol}\n─────────────────────\n{tech_str}\n─────────────────────\n{result}", cid)

                elif text=="/indices" or text=="/index":
                    d=fetch_data()
                    send(get_index_summary(d), cid)



                elif text=="/test":
                    d=fetch_data()
                    send(f"""✅ SYSTEMS TEST
─────────────────────
Telegram:✅ | Model:{MODEL}
NIFTY:{f(d.get('nifty',{}).get('price'))}
Chain:{'✅' if d.get('chain') else '⚠️ add DHAN vars'}
Sheets:{'✅' if get_sheet() else '⚠️ optional'}
State:{S['market_state']}
─────────────────────
{datetime.now(IST).strftime('%d %b %I:%M %p IST')}""",cid)

                elif text.startswith("/win ") or text.startswith("/loss "):
                    parts=text.split(" ",2);is_win=text.startswith("/win")
                    try: amt=float(parts[1].replace(",",""))
                    except: amt=0
                    lesson=parts[2] if len(parts)>2 else ""
                    # Auto-tag playbook and state from last alert
                    auto_pb=""; auto_state=""
                    if S["last_alert"]:
                        pb_match=re.search(r"PLAYBOOK:\s*(PB-\d+)",S["last_alert"])
                        auto_pb=pb_match.group(1) if pb_match else ""
                        auto_state=S["market_state"]
                    update_outcome(f"{'+'if is_win else'-'}₹{amt:,.0f}",lesson)
                    if S["positions"]: S["positions"].pop(-1)
                    # Update adaptive threshold based on new win rate
                    mem=read_mem()
                    thresh,tnote=adaptive_threshold(mem["trades"])
                    tag_info=f"Playbook: {auto_pb} | State: {auto_state}" if auto_pb else ""
                    send(f"{'✅ WIN' if is_win else '❌ LOSS'}: {'+'if is_win else'-'}₹{amt:,.0f}\n{'💡 '+lesson if lesson else ''}\n{tag_info}\nNew threshold: {thresh}/10 ({tnote})",cid)

                elif text.startswith("/"):
                    send("Unknown command. Type /help or just chat with me.",cid)

                else:
                    # Free chat — personal assistant mode
                    depth="default";clean=text
                    if text.lower() in ["/why","/deep"] or text.lower().startswith("/why ") or text.lower().startswith("/deep "):
                        depth="why" if "why" in text.lower() else "deep"
                        clean=text.split(" ",1)[1].strip() if " " in text else (S["last_alert"] or "Explain the last alert")

                    # Detect natural language watchlist intent
                    text_lower=text.lower()
                    bought_keywords=["i bought","i have bought","i purchased","i bought","bought at","i'm buying","i am buying","i will buy","planning to buy"]
                    interested_keywords=["interested in","watching","tracking","monitoring","keep eye on"]

                    is_bought=any(kw in text_lower for kw in bought_keywords)
                    is_interested=any(kw in text_lower for kw in interested_keywords)

                    if is_bought or is_interested:
                        # Extract stock name and price using AI
                        extract=gemini(
                            "Extract stock symbol and price from this message. Return ONLY: SYMBOL:XXXXX PRICE:XXXXX or SYMBOL:XXXXX if no price. If no clear stock mentioned return: NONE",
                            f"Message: {text}\nExtract NSE stock ticker and price if mentioned.",
                            50, search=False
                        )
                        if "NONE" not in extract and "SYMBOL:" in extract:
                            parts_e=extract.split()
                            sym_part=[x for x in parts_e if x.startswith("SYMBOL:")]
                            price_part=[x for x in parts_e if x.startswith("PRICE:")]
                            if sym_part:
                                symbol=sym_part[0].replace("SYMBOL:","").strip()
                                price=float(price_part[0].replace("PRICE:","").strip()) if price_part else 0
                                if symbol and symbol!="NONE":
                                    add_to_watchlist(symbol, price, text[:100], cid)
                                    send(f"Got it! Added {symbol} to your watchlist.\nI'll monitor it 24/7 and alert you about profit targets, stop losses, and important news!\n/watchlist to see all your stocks", cid)

                    d=fetch_data();m=read_mem()
                    import random
                    intros=["On it! Let me check..","Looking into that..","Good question, checking now..","Let me search for the latest.."]
                    send(random.choice(intros),cid)
                    reply=handle_chat(clean,cid,d,m,depth)
                    send(reply,cid)

        except Exception as e: log.warning(f"Cmd:{e}"); time.sleep(5)

# ── MAIN ──────────────────────────────────────────────────────────────────
def to_utc(ist):
    h,m=map(int,ist.split(":"));t=h*60+m-330
    if t<0: t+=1440
    return f"{t//60:02d}:{t%60:02d}"

def main():
    import sys
    log.info(f"⚡ TRADE DESK — FINAL | Model:{MODEL}")
    if "--test" in sys.argv:
        run_alert("TEST");return
    for ist_t,atype in [("07:00","MORNING_BRIEF"),("08:30","8:30 AM Pre-Market"),
                         ("09:20","9:20 AM Opening"),("11:30","11:30 AM Mid-Session"),
                         ("14:00","2:00 PM Power Hour"),("15:15","3:15 PM Pre-Close")]:
        for day in ["monday","tuesday","wednesday","thursday","friday"]:
            if atype=="MORNING_BRIEF":
                getattr(schedule.every(),day).at(to_utc(ist_t)).do(morning_brief)
            else:
                getattr(schedule.every(),day).at(to_utc(ist_t)).do(run_alert,atype=atype)
        log.info(f"Scheduled {atype} at {ist_t}")
    schedule.every().friday.at(to_utc("16:00")).do(weekly_report)
    schedule.every().day.at("00:01").do(lambda: S.update({"daily_loss":0}))
    schedule.every(15).minutes.do(intraday_scan)
    threading.Thread(target=handle_commands,daemon=True).start()
    send("""⚡ TRADE DESK ONLINE

Just type anything to chat.
I'll search the web and answer in 3-5 lines.

Type /help for commands.

⏰ IST (Mon-Fri):
🌅 7:00 Brief | 8:30 | 9:20 | 11:30 | 14:00 | 15:15
📊 Fri 16:00 Weekly | ⚡ Scanner every 15 min""")
    log.info("Running.")
    while True: schedule.run_pending(); time.sleep(30)

if __name__=="__main__": main()
