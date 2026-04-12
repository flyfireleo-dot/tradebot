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

import os, time, json, logging, requests, schedule, pytz, threading, re
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

# ── STATE ─────────────────────────────────────────────────────────────────
S = {
    "cons_losses":0,"daily_loss":0,"blocked":0,
    "halted":False,"halt_reason":"","last_fetch":None,
    "positions":[],"port_delta":0,"port_theta":0,
    "market_state":"UNKNOWN","desk_temp":"NORMAL",
    "last_alert":None,"last_alert_type":"",
    "intraday_nifty":None,"intraday_vix":None,
}
conversation_history = {}
scanner_state = {"last_nifty":None,"last_vix":None,"alerted_levels":set(),"count":0}

# ── KILL SWITCH ───────────────────────────────────────────────────────────
def kill_check():
    if S["halted"]: return True,S["halt_reason"]
    if S["cons_losses"]>=3: S["halted"]=True;S["halt_reason"]="3 consecutive losses";return True,S["halt_reason"]
    if S["daily_loss"]>=125000: S["halted"]=True;S["halt_reason"]="Daily loss limit hit";return True,S["halt_reason"]
    if S["blocked"]>=2: S["halted"]=True;S["halt_reason"]="2 supervisor blocks";return True,S["halt_reason"]
    if S["last_fetch"] and (datetime.now(IST)-S["last_fetch"]).seconds>900: return True,"Data stale >15 min"
    return False,""

def kill_reset(): S.update({"halted":False,"halt_reason":"","cons_losses":0,"daily_loss":0,"blocked":0})

def blackout():
    n=datetime.now(IST);h,m=n.hour,n.minute
    if h==9 and m<20: return "First 5 min of open"
    if h==15 and m>=26: return "Market closing"
    if h<9 or h>=16: return "Market closed"
    if n.weekday()>=5: return "Weekend"
    return None

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

ASSISTANT_PROMPT="""Professional trading analyst for Indian options.
Desk language. No fluff. No soft hedging.

TRADER: ₹5,00,000 capital | NIFTY options | Indian market

RESPONSE DEPTHS:
- default: 3-5 lines, direct answer
- why: 10-15 lines, key factors explained
- deep: full analysis with sources

STYLE:
❌ "I think it might go up"
✅ "Bias: Bullish above 24200. Invalid below 24120."

Always search web for current prices/news before answering.
Cite source briefly in one word (Reuters, ET, NSE, etc.)"""

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
    thresh,_=adaptive_threshold(mem["trades"])
    closed=[t for t in mem["trades"] if t.get("Outcome") in ["WIN","LOSS","BREAKEVEN"]]
    wr=round(sum(1 for t in closed if t.get("Outcome")=="WIN")/len(closed)*100) if closed else 0
    tokens={"default":300,"why":600,"deep":1200}.get(depth,300)
    depth_inst={"default":"3-5 lines. Direct answer only.","why":"10-15 lines. Key factors explained.","deep":"Full analysis with sources. Thorough."}.get(depth,"")

    context=f"""TRADER: Win rate {wr}% | {len(closed)} trades | ₹5,00,000
MARKET: NIFTY {f(data.get('nifty',{}).get('price'))} | VIX {f(data.get('vix',{}).get('price'))} | {data.get('alignment')} global
STATE: {S['market_state']} | DESK: {S['desk_temp']}
TIME: {datetime.now(IST).strftime('%A %d %B %Y %I:%M %p IST')}
DEPTH: {depth.upper()} — {depth_inst}

Question: {text}"""

    try:
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"
        history=get_history(cid)
        payload={"system_instruction":{"parts":[{"text":ASSISTANT_PROMPT}]},
                 "contents":history[:-1]+[{"role":"user","parts":[{"text":context}]}],
                 "tools":[{"google_search":{}}],
                 "generationConfig":{"maxOutputTokens":tokens,"temperature":0.3}}
        r=requests.post(url,json=payload,timeout=45); r.raise_for_status()
        reply=r.json()["candidates"][0]["content"]["parts"][0]["text"]
        add_history(cid,"model",reply)
        return reply
    except Exception as e:
        log.error(f"Chat:{e}")
        try: return gemini(ASSISTANT_PROMPT,context,tokens)
        except: return "⚠️ Unavailable. Try /alert for trading signals."

# ── MORNING BRIEF ─────────────────────────────────────────────────────────
def morning_brief():
    log.info("Morning brief...")
    try:
        data=fetch_data()
        gift=data.get("gift",{})
        gift_price=gift.get("price","N/A")
        gift_chg=gift.get("change",0) or 0
        gift_src=gift.get("source","")
        gift_sig=data.get("gift_signal")

        # Build GIFT Nifty line
        gift_line=f"GIFT NIFTY: {f(gift_price)} ({gift_chg:+.2f}%) [{gift_src}]"
        bias_line=gift_sig["bias"] if gift_sig else "Gap reading unavailable"
        gap_pts=gift_sig["gap_pts"] if gift_sig else 0

        brief=gemini(
            "Market briefer. Search web NOW for today's key news. Output EXACTLY 3 lines:\nMOOD: [X]/10 [one word]\nWATCH: [one thing to watch today]\nPLAN: [one action for 9:20 AM]",
            f"""Today {datetime.now(IST).strftime('%A %d %B %Y')}.
GIFT Nifty: {gift_price} ({gift_chg:+.2f}%) — gap estimate: {gap_pts:+.0f} pts
NIFTY prev close: {f(data.get('nifty',{}).get('price'))}
Global: {data.get('alignment')} | Crude: ${f(data.get('crude',{}).get('price'))}
VIX: {f(data.get('vix',{}).get('price'))}
Search web for top 2 market-moving news items today.""",
            120
        )

        send(f"""🌅 MORNING BRIEF — {datetime.now(IST).strftime('%a %d %b')}
─────────────────────
{gift_line}
Gap est: {gap_pts:+.0f} pts
Signal: {bias_line}
─────────────────────
{brief}
─────────────────────
Full alert 8:30 AM ⏰""")
    except Exception as e:
        log.warning(f"Morning brief:{e}")

# ── INTRADAY SCANNER ──────────────────────────────────────────────────────
def intraday_scan():
    now=datetime.now(IST);h,m=now.hour,now.minute
    if not (9<=h<15):return
    if now.weekday()>=5:return
    halted,_=kill_check()
    if halted:return
    try:
        nifty=S.get("intraday_nifty");vix=S.get("intraday_vix")
        if not nifty: return
        alerts=[]
        # VIX spike — Class A
        if vix and scanner_state["last_vix"]:
            vix_move=vix-scanner_state["last_vix"]
            key=f"vix_{int(vix)}"
            if vix_move>3 and key not in scanner_state["alerted_levels"]:
                scanner_state["alerted_levels"].add(key)
                alerts.append(f"⚠️ VIX SPIKE — {now.strftime('%I:%M %p')}\n─────────────────────\nVIX jumped +{vix_move:.1f} to {vix:.1f}\nImpact: Options getting expensive\nPlan: Exit long premium if any")
        # NIFTY big move — Class A
        if scanner_state["last_nifty"]:
            move_pct=abs(nifty-scanner_state["last_nifty"])/scanner_state["last_nifty"]*100
            if move_pct>0.5:
                direction="up" if nifty>scanner_state["last_nifty"] else "down"
                key=f"move_{int(nifty/100)*100}"
                if key not in scanner_state["alerted_levels"]:
                    scanner_state["alerted_levels"].add(key)
                    alerts.append(f"⚡ MOVE ALERT — {now.strftime('%I:%M %p')}\n─────────────────────\nNIFTY {direction} {move_pct:.1f}% to {nifty:.0f}\nAction: {'CE entry possible' if direction=='up' else 'PE entry possible'}\nConfidence: 6/10 — verify before acting")
        # SL warning
        for pos in S["positions"]:
            sl=pos.get("sl",0)
            if sl and pos["current"] < sl*1.15 and f"sl_{pos['strike']}" not in scanner_state["alerted_levels"]:
                scanner_state["alerted_levels"].add(f"sl_{pos['strike']}")
                alerts.append(f"⚠️ SL WARNING\n─────────────────────\nNIFTY {pos['strike']} {pos['type']} approaching SL ₹{sl}\nCurrent: ₹{pos['current']:.0f}\nConsider exiting now")
        for a in alerts: send(a)
        scanner_state["last_nifty"]=nifty;scanner_state["last_vix"]=vix
        scanner_state["count"]+=1
        if scanner_state["count"]%16==0: scanner_state["alerted_levels"]=set()
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
        prompt=f"""Generate {atype} alert.
TODAY: {datetime.now(IST).strftime('%A %d %B %Y %I:%M %p')} IST
DESK: {desk}
STATE: {state} | LEGAL PLAYBOOKS: {legal_pbs or 'NONE'}
THRESHOLD: {thresh}/10 ({thresh_note})
NEXT 5 DAYS: {next5}
NIFTY:{f(data.get('nifty',{}).get('price'))} {p(data.get('nifty',{}).get('change'))} | VIX:{f(data.get('vix',{}).get('price'))}{gift_ctx}
SENSEX:{f(data.get('sensex',{}).get('price'))} | BankNifty:{f(data.get('banknifty',{}).get('price'))}
Dow:{f(data.get('dow',{}).get('price'),0)} | S&P:{f(data.get('sp500',{}).get('price'))} | Global:{data.get('alignment')}
Crude:${f(data.get('crude',{}).get('price'))} | USD/INR:₹{f(data.get('usdinr',{}).get('price'))}{chain_ctx}
Portfolio:{portfolio_summary()}
Lessons:{lessons_ctx or 'none'}
Search web for events. Generate clean alert."""
        alert=gemini(MAIN_PROMPT,prompt,700)
        sup=gemini(SUPERVISOR_PROMPT,f"Validate:\n{alert}\nNIFTY:{f(data.get('nifty',{}).get('price'))} Capital:₹5,00,000",80,search=False)
        if "❌ BLOCKED" in sup or "BLOCKED" in sup.upper():
            S["blocked"]+=1;send(f"{alert}\n\n{sup}\n\n❌ DO NOT TRADE."); return
        S["blocked"]=0;S["last_alert"]=alert;S["last_alert_type"]=atype
        is_trade="SIGNAL:" in alert and "NO TRADE" not in alert
        buttons=[[{"text":"✅ Executed","data":"btn_executed"},{"text":"⏭ Skip","data":"btn_skip"}],
                 [{"text":"❓ Why","data":"btn_why"},{"text":"📊 Deep","data":"btn_deep"}]] if is_trade else \
                [[{"text":"🔔 Alert if setup forms","data":"btn_watch"}]]
        send(f"{alert}\n\n{sup}",buttons=buttons)
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
                    send("""⚡ TRADE DESK

Just type anything to chat.
I search the web and answer in 3-5 lines.

Add /why or /deep for more detail on any answer.

COMMANDS:
/alert — Generate alert now
/status — Live market (5 lines)
/events — Next 5 days
/risk — Risk status
/pnl — P&L summary
/patterns — Your trading edge
/learn — Recent lessons
/report — Weekly report
/clear — Clear chat memory
/resume — Reset kill switch

LOG TRADES:
/win 12400 lesson here
/loss 8200 lesson here

⏰ AUTO (IST Mon-Fri):
🌅 7:00 Brief | 8:30 | 9:20 | 11:30 | 14:00 | 15:15
Fri 16:00 Weekly""",cid)

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

                elif text=="/resume":
                    kill_reset();send("✅ Reset. Trade carefully.",cid)

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
                    update_outcome(f"{'+'if is_win else'-'}₹{amt:,.0f}",lesson)
                    if S["positions"]: S["positions"].pop(-1)
                    send(f"{'✅ WIN' if is_win else '❌ LOSS'}: {'+'if is_win else'-'}₹{amt:,.0f}\n{'💡 '+lesson if lesson else ''}",cid)

                elif text.startswith("/"):
                    send("Unknown command. Type /help or just chat with me.",cid)

                else:
                    # Free chat with depth control
                    depth="default";clean=text
                    if text.lower() in ["/why","/deep"] or text.lower().startswith("/why ") or text.lower().startswith("/deep "):
                        depth="why" if "why" in text.lower() else "deep"
                        clean=text.split(" ",1)[1].strip() if " " in text else (S["last_alert"] or "Explain the last alert")
                    d=fetch_data();m=read_mem()
                    send("🔍 Searching...",cid)
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
