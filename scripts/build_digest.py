#!/usr/bin/env python3
"""Build the Morning Market Digest.

Reads public RSS feeds, BSE corporate announcements and free price data,
classifies items (Groq LLM if GROQ_API_KEY is set, keyword rules otherwise)
and writes data/<run-id>.json plus data/index.json for the static site.

Usage:
  python scripts/build_digest.py                     # full digest
  python scripts/build_digest.py --mode movers --threshold 10
  python scripts/build_digest.py --mode custom --query "order wins above 500 Cr this week"
"""
import argparse, json, os, re, sys, time, hashlib, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IST = ZoneInfo("Asia/Kolkata")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*", "Accept-Language": "en-IN,en;q=0.9"}

NEWS_FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Economic Times Stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/latestnews.xml",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Business Standard Companies": "https://www.business-standard.com/rss/companies-101.rss",
    "Mint Markets": "https://www.livemint.com/rss/markets",
    "Mint Companies": "https://www.livemint.com/rss/companies",
    "Reuters India (via Google News)": "https://news.google.com/rss/search?q=site:reuters.com+India+stocks&hl=en-IN&gl=IN&ceid=IN:en",
}
GNEWS = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-IN&gl=IN&ceid=IN:en"
GNEWS_FEEDS = {
    "Google News · order wins": GNEWS.format(q="%22order%22+OR+%22contract%22+OR+%22LoI%22+bags+crore+shares"),
    "Google News · results": GNEWS.format(q="Q2+results+net+profit+crore"),
    "Google News · M&A": GNEWS.format(q="acquisition+OR+stake+OR+merger+crore+company+India"),
    "Google News · policy": GNEWS.format(q="RBI+OR+SEBI+OR+government+markets+policy+India"),
    "Google News · markets": GNEWS.format(q="Sensex+Nifty+today"),
    "Google News · stocks in focus": GNEWS.format(q="%22stocks+to+watch%22+OR+%22stocks+in+focus%22"),
}
POLICY_FEEDS = {
    "RBI": "https://www.rbi.org.in/pressreleases_rss.xml",
    "SEBI": "https://www.sebi.gov.in/sebirss.xml",
    "PIB": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
}
BSE_ANN = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_FORTH = "https://api.bseindia.com/BseIndiaAPI/api/Corpforthresults/w"
BSE_BOARD = "https://api.bseindia.com/BseIndiaAPI/api/BoardMeetings/w"
NIFTY500_CSV = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
NSE_EQUITY_CSV = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
SEGMENT_LISTS = {
    "Large": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "Mid": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "Small": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    "Micro": "https://archives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv",
}

def segment_map():
    seg = {}
    for name, url in SEGMENT_LISTS.items():
        try:
            r = get(url, headers={"Referer": "https://www.nseindia.com/"})
            for line in r.text.splitlines()[1:]:
                parts = [x.strip().strip('"') for x in line.split(",")]
                if len(parts) >= 3 and parts[2] and parts[2] not in seg:
                    seg[parts[2]] = name
        except Exception as ex:
            log(f"segment list {name} FAILED: {ex}")
    log(f"segment map: {len(seg)} symbols")
    return seg

CATS = ["calendar", "results", "orders", "mna", "policy", "movers", "global", "other"]
STATS = {}
NAMES = {}

def stat(name, ok, n=0, err=""):
    STATS[name] = {"ok": ok, "n": n, "err": str(err)[:160]}

# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, file=sys.stderr, flush=True)

def now_ist():
    return dt.datetime.now(IST)

def iso_utc(d=None):
    d = d or dt.datetime.now(dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def get(url, params=None, timeout=25, headers=None):
    h = dict(UA); h.update(headers or {})
    r = requests.get(url, params=params, headers=h, timeout=timeout)
    r.raise_for_status()
    return r

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ---------------------------------------------------------------- sources
def fetch_feeds(feeds, since):
    import feedparser
    out = []
    for name, url in feeds.items():
        try:
            r = get(url)
            parsed = feedparser.parse(r.content)
            n = 0
            for e in parsed.entries:
                ts = e.get("published_parsed") or e.get("updated_parsed")
                when = dt.datetime(*ts[:6], tzinfo=dt.timezone.utc) if ts else None
                if when and when < since:
                    continue
                title = clean(e.get("title"))
                src = name
                if name.startswith("Google News") and " - " in title:
                    title, src = title.rsplit(" - ", 1)
                out.append({
                    "source": src,
                    "title": title,
                    "summary": clean(e.get("summary", ""))[:400],
                    "link": e.get("link", ""),
                    "when": iso_utc(when) if when else None,
                })
                n += 1
            log(f"feed {name}: {n} items"); stat(name, True, n)
        except Exception as ex:
            log(f"feed {name} FAILED: {ex}"); stat(name, False, 0, ex)
    return out

def fetch_bse(since):
    """BSE corporate announcements for the window. Returns raw headline rows."""
    out = []
    d0 = since.astimezone(IST).strftime("%Y%m%d")
    d1 = now_ist().strftime("%Y%m%d")
    h = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
    for page in range(1, 6):
        params = {"pageno": page, "strCat": "-1", "strPrevDate": d0, "strScrip": "", "strSearch": "P",
                  "strToDate": d1, "strType": "C", "subcategory": "-1"}
        try:
            r = get(BSE_ANN, params=params, headers=h)
            rows = r.json().get("Table", [])
        except Exception as ex:
            log(f"bse page {page} FAILED: {ex}"); stat("BSE announcements", False, len(out), ex)
            break
        if not rows:
            break
        for row in rows:
            head = clean(row.get("HEADLINE") or row.get("NEWSSUB") or "")
            cat = clean(row.get("CATEGORYNAME", ""))
            if not head:
                continue
            # keep the categories that matter; skip routine compliance filings
            if re.search(r"(Trading Window|Certificate|Compliance|Reg\.? ?74|Investor Presentation|Newspaper|Analyst|Clarification|Record Date|Outcome of Board Meeting)$", head, re.I) and not re.search(r"(order|contract|acqui|merger|result|dividend|buyback|rating|guidance)", head, re.I):
                continue
            out.append({
                "source": "BSE filing",
                "title": f"{clean(row.get('SLONGNAME',''))}: {head}",
                "summary": cat,
                "link": "https://www.bseindia.com/corporates/ann.html",
                "when": None,
            })
        log(f"bse page {page}: {len(rows)} rows")
        time.sleep(0.8)
    stat("BSE announcements", True, len(out))
    return out

def fetch_results_calendar(days=7):
    """Companies with results board meetings in the next `days` days (BSE forthcoming results)."""
    h = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
    d0 = now_ist(); d1 = d0 + dt.timedelta(days=days)
    rows = []
    for url, params in (
        (BSE_FORTH, {"scripcode": "", "Fdate": d0.strftime("%Y%m%d"), "TDate": d1.strftime("%Y%m%d"), "Purpose": "P.1", "strSearch": "P"}),
        (BSE_BOARD, {"scripcode": "", "Fdate": d0.strftime("%Y%m%d"), "TDate": d1.strftime("%Y%m%d"), "Purpose": "", "strSearch": "P", "ddlPurpose": "P.1"}),
    ):
        try:
            r = get(url, params=params, headers=h)
            data = r.json()
            rows = data if isinstance(data, list) else (data.get("Table") or data.get("data") or [])
            if rows:
                break
        except Exception as ex:
            log(f"results calendar {url.split('/')[-2]} FAILED: {ex}")
    out = []
    for row in rows:
        name = clean(row.get("short_name") or row.get("Long_Name") or row.get("SLONGNAME") or row.get("sLongName") or "")
        date = clean(str(row.get("meeting_date") or row.get("BM_DATE") or row.get("Meeting_Date") or row.get("DT_TM") or ""))[:10]
        purpose = clean(row.get("PURPOSE") or row.get("Purpose") or "Results")
        if not name or not date:
            continue
        if purpose and not re.search(r"result", purpose, re.I):
            continue
        out.append({"name": name, "date": date, "purpose": purpose[:80]})
    log(f"results calendar: {len(out)} companies"); stat("Results calendar (BSE)", len(out) > 0, len(out))
    return out

def nse_universe():
    try:
        r = get(NSE_EQUITY_CSV, headers={"Referer": "https://www.nseindia.com/"})
        syms = []
        for line in r.text.splitlines()[1:]:
            parts = [x.strip().strip('"') for x in line.split(",")]
            if len(parts) >= 3 and parts[2] in ("EQ", "BE") and parts[0]:
                syms.append(parts[0]); NAMES[parts[0]] = parts[1]
        if len(syms) > 500:
            log(f"nse full universe: {len(syms)} symbols"); return syms
    except Exception as ex:
        log(f"nse equity list FAILED: {ex}; trying nifty500")
    try:
        r = get(NIFTY500_CSV, headers={"Referer": "https://www.nseindia.com/"})
        syms = []
        for line in r.text.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 3 and parts[2].strip():
                syms.append(parts[2].strip())
        log(f"nifty500 universe: {len(syms)} symbols")
        return syms
    except Exception as ex:
        log(f"nifty500 list FAILED: {ex}; using fallback 60-stock universe")
        return ["RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY","SBIN","BHARTIARTL","ITC","LT","KOTAKBANK","AXISBANK","HINDUNILVR","BAJFINANCE","MARUTI","SUNPHARMA","TITAN","ULTRACEMCO","NTPC","ONGC","POWERGRID","TATAMOTORS","TATASTEEL","JSWSTEEL","ADANIENT","ADANIPORTS","COALINDIA","BPCL","WIPRO","HCLTECH","TECHM","DRREDDY","CIPLA","DIVISLAB","NESTLEIND","BRITANNIA","ASIANPAINT","GRASIM","HINDALCO","INDUSINDBK","EICHERMOT","HEROMOTOCO","BAJAJ-AUTO","M&M","SHRIRAMFIN","BAJAJFINSV","SBILIFE","HDFCLIFE","TATACONSUM","APOLLOHOSP","LTIM","TRENT","BEL","HAL","ZOMATO","PAYTM","IRFC","RVNL","MAZDOCK","COFORGE","PERSISTENT"]

def fetch_movers(threshold, intraday):
    """Movers via yfinance. intraday=True compares last price to today's open; else previous close to last close."""
    try:
        import yfinance as yf
    except Exception as ex:
        log(f"yfinance missing: {ex}"); return []
    syms = nse_universe()
    tickers = [s + ".NS" for s in syms]
    out = []
    frames = {}
    for i in range(0, len(tickers), 400):
        part = tickers[i:i + 400]
        try:
            df = yf.download(part, period="5d", interval="1d", group_by="ticker", threads=True, progress=False, auto_adjust=False)
            for t in part:
                try:
                    frames[t] = df[t] if len(part) > 1 else df
                except Exception:
                    pass
        except Exception as ex:
            log(f"yfinance chunk {i} FAILED: {ex}")
    if not frames:
        stat("NSE prices (Yahoo)", False, 0, "all chunks failed"); return []
    for s, t in zip(syms, tickers):
        try:
            if t not in frames:
                continue
            sub = frames[t].dropna(how="all")
            if len(sub) < 2:
                continue
            last, prev = sub.iloc[-1], sub.iloc[-2]
            if intraday:
                base, px = float(last["Open"]), float(last["Close"])
            else:
                base, px = float(prev["Close"]), float(last["Close"])
            if not base or base != base:
                continue
            pct = (px / base - 1) * 100
            vol = float(last["Volume"]); avgv = float(sub["Volume"].iloc[:-1].mean() or 0)
            if px * vol < 2e6 or px < 5:   # skip illiquid names: < ₹20 lakh traded or penny prices
                continue
            if abs(pct) >= threshold:
                out.append({"company": s, "pct": round(pct, 2), "price": round(px, 2),
                            "volx": round(vol / avgv, 1) if avgv else None})
        except Exception:
            continue
    seg = segment_map()
    for m in out:
        m["cap"] = seg.get(m["company"], "Micro")
    out.sort(key=lambda x: -abs(x["pct"]))
    log(f"movers >= {threshold}%: {len(out)} of {len(syms)} scanned"); stat("NSE prices (Yahoo)", True, len(out), f"scanned {len(syms)}")
    return out[:60]

def fetch_global():
    try:
        import yfinance as yf
    except Exception:
        return {}
    idx = {"sensex": "^BSESN", "nifty": "^NSEI", "midcap": "^NSEMDCP50", "smallcap": "^CNXSC", "sp500": "^GSPC", "nasdaq": "^IXIC", "nikkei": "^N225",
           "hangseng": "^HSI", "brent": "BZ=F", "usdinr": "INR=X", "gold": "GC=F"}
    out = {}
    try:
        df = yf.download(list(idx.values()), period="5d", interval="1d", group_by="ticker", progress=False, auto_adjust=False)
        for k, t in idx.items():
            sub = df[t].dropna(how="all")
            if len(sub) >= 2:
                last, prev = float(sub.iloc[-1]["Close"]), float(sub.iloc[-2]["Close"])
                pct = (last / prev - 1) * 100
                out[k] = f"{last:,.{2 if last < 1000 else 0}f} ({pct:+.2f}%)"
    except Exception as ex:
        log(f"global FAILED: {ex}")
    return out

# ---------------------------------------------------------------- mover reasons
STOP = {"ltd", "limited", "india", "industries", "the", "co", "company", "corporation", "corp", "and", "of", "&", "international", "enterprises", "technologies", "technology", "services", "solutions", "products", "global", "group", "holdings", "finance", "financial", "bank", "power", "energy", "infra", "infrastructure", "pharma", "pharmaceuticals", "chemicals", "steel", "motors", "textiles", "mills"}

def name_tokens(sym):
    name = NAMES.get(sym, sym)
    toks = [t for t in re.findall(r"[A-Za-z]{3,}", name.lower()) if t not in STOP]
    return toks[:2] or [name.lower()[:6]]

def find_reason(sym, items, raw):
    toks = name_tokens(sym)
    def hit(text):
        t = text.lower()
        return all(tok in t for tok in toks)
    for it in items:
        if it.get("category") != "movers" and (hit(it.get("company", "")) or hit(it.get("fact", ""))):
            return f"Likely reason: {it['fact']}", it.get("source", "")
    for r in raw:
        if hit(r["title"]):
            return f"Likely reason: {r['title'][:140]}", r["source"]
    return None, None

def news_lookup(sym):
    """Targeted Google News search for a mover nothing local explains."""
    import feedparser
    name = NAMES.get(sym, sym)
    q = requests.utils.quote(f'"{name}" shares')
    try:
        r = get(GNEWS.format(q=q + "+when:2d"))
        for e in feedparser.parse(r.content).entries[:5]:
            title = clean(e.get("title"))
            if " - " in title:
                title, src = title.rsplit(" - ", 1)
            else:
                src = "news"
            if re.search(r"(surge|rall|jump|soar|zoom|plunge|crash|tank|fall|slump|circuit|order|result|profit|acqui|stake|deal|approv|launch|block|promoter|rating|contract|win|loss)", title, re.I):
                return f"Likely reason: {title[:140]}", src
    except Exception as ex:
        log(f"news lookup {sym} failed: {ex}")
    return None, None

# ---------------------------------------------------------------- classify
KEYWORDS = [
    ("orders", r"\b(order|orders|contract|LOI|letter of (intent|award)|bags|wins|awarded|work order|purchase order|EPC)\b"),
    ("mna", r"\b(acqui|merger|amalgamat|stake|takeover|open offer|buyout|divest|JV|joint venture|demerger)\b"),
    ("results", r"\b(Q[1-4]|quarter|results?|net profit|PAT|revenue|EBITDA|guidance|dividend|buyback|margin)\b"),
    ("policy", r"\b(RBI|SEBI|government|ministry|policy|circular|regulat|GST|budget|tariff|duty|PLI|cabinet|repo rate|yield)\b"),
    ("global", r"\b(Wall Street|Dow|Nasdaq|S&P|Nikkei|Hang Seng|Fed|crude|Brent|rupee|dollar|gold|GIFT Nifty|Asian markets|US markets)\b"),
]

def rule_classify(raw):
    items = []
    for r in raw:
        text = f"{r['title']} {r['summary']}"
        cat = "other"
        for c, pat in KEYWORDS:
            if re.search(pat, text, re.I):
                cat = c; break
        if cat == "other" and r["source"] != "BSE filing":
            continue  # drop generic news in rule mode
        t = r["title"]
        if r["source"] == "BSE filing":
            company = t.split(":")[0]
        else:
            m = re.match(r"^((?:[A-Z][\w&'.-]*\s?){1,4})", t)
            company = (m.group(1).strip() if m else "Market")
            if company.lower() in ("the", "why", "how", "what", "stock", "stocks", "sensex", "nifty", "gold", "rupee", "oil", "asian", "global", "indian"):
                company = {"sensex": "Markets", "nifty": "Markets", "gold": "Gold", "rupee": "Rupee", "oil": "Crude", "asian": "Asian markets", "global": "Global markets", "indian": "Markets"}.get(company.lower(), "Markets")
        items.append({"category": cat, "company": company[:60], "fact": r["title"][:180],
                      "source": r["source"], "materiality": 2 if r["source"] == "BSE filing" else 1, "link": r["link"]})
    return items

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PROMPT = """You turn Indian stock-market headlines into a structured digest. Rules: facts only, in your own words, each fact at most 25 words with the numbers; never add buy/sell/target language; drop items that are not material market news (listicles, opinion, lifestyle, ads, routine compliance filings, IPO GMP chatter); merge duplicates of the same story; company = the listed company or body (e.g. "RBI", "Bond market"); materiality 1-3 (3 = index name, or deal/number large relative to the company). Categories: results (quarterly numbers, guidance, dividends, buybacks), orders (order wins, contracts, LOIs), mna (acquisitions, stake sales, mergers, open offers), policy (RBI/SEBI/government/regulation/macro-India), global (overseas markets, crude, currency), other (management changes, ratings, fund-raises, bulk deals, legal). MODE: {mode}. {query}
Return ONLY a JSON object: {{"items":[{{"category":"...","company":"...","fact":"...","value":"<₹ amount for orders/deals or omit>","source":"<source name as given>","materiality":1}}]}}
HEADLINES (source | title | summary):
{rows}"""

PREFILTER = re.compile(r"\b(order|orders|contract|LOI|letter of|bags|wins|awarded|acqui|merger|amalgamat|stake|takeover|open offer|buyback|divest|JV|joint venture|demerger|Q[1-4]|quarter|results?|net profit|PAT|revenue|EBITDA|guidance|dividend|margin|RBI|SEBI|government|ministry|policy|circular|regulat|GST|budget|tariff|duty|PLI|cabinet|repo|yield|bond|Sensex|Nifty|GIFT|Wall Street|Dow|Nasdaq|S&P|Nikkei|Hang Seng|Fed|crude|Brent|rupee|dollar|gold|FII|DII|IPO|listing|rating|upgrade|downgrade|block deal|bulk deal|promoter|pledge|resign|appoint|CEO|MD|fund ?rais|QIP|rights issue|NCLT|SAT|penalty|fine|probe|surge|rall|jump|plunge|crash|tank|upper circuit|lower circuit|52-week)\b", re.I)
NOISE = re.compile(r"(horoscope|astrolog|recipe|celebrit|bollywood|cricket|IPL|weather|quiz|crossword|how to|explained|top \d+|best .* to buy|multibagger|should you|opinion|editorial|podcast|live updates|gmp|grey market)", re.I)

def prefilter(raw):
    out, seen = [], set()
    for r in raw:
        t = r["title"]
        k = re.sub(r"\W+", "", t.lower())[:60]
        if k in seen or len(t) < 25:
            continue
        if NOISE.search(t) and r["source"] != "BSE filing":
            continue
        if r["source"] == "BSE filing" or PREFILTER.search(t + " " + r["summary"]):
            seen.add(k); out.append(r)
    log(f"prefilter: {len(raw)} -> {len(out)}")
    return out[:400]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PROMPT = """You turn Indian stock-market headlines into a structured digest. Rules: facts only, in your own words, each fact at most 25 words with the numbers; never add buy/sell/target language; drop items that are not material market news (listicles, opinion, lifestyle, ads, routine compliance filings, IPO GMP chatter); merge duplicates of the same story; company = the listed company or body (e.g. "RBI", "Bond market"); materiality 1-3 (3 = index name, or deal/number large relative to the company). Categories: results (quarterly numbers, guidance, dividends, buybacks), orders (order wins, contracts, LOIs), mna (acquisitions, stake sales, mergers, open offers), policy (RBI/SEBI/government/regulation/macro-India), global (overseas markets, crude, currency), other (management changes, ratings, fund-raises, bulk deals, legal). MODE: {mode}. {query}
Return ONLY a JSON object: {{"items":[{{"category":"...","company":"...","fact":"...","value":"<rupee amount for orders/deals or omit>","source":"<source name as given>","materiality":1}}]}}
HEADLINES (source | title | summary):
{rows}"""

def pick_groq_models(key):
    """Ask Groq which models this key can use and order them by preference."""
    env = os.environ.get("GROQ_MODEL", "").strip()
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    except Exception as ex:
        log(f"groq model list FAILED: {ex}"); ids = []
    log(f"groq models available: {ids}")
    prefs = ["llama-4-maverick", "llama-4-scout", "llama-3.3-70b", "gpt-oss-120b", "qwen", "gpt-oss-20b", "llama-3.1-8b", "llama", "mixtral", "gemma"]
    skip = ("whisper", "tts", "guard", "embed", "vision", "safeguard", "compound", "orpheus", "playai")
    cands = [i for i in ids if not any(k in i.lower() for k in skip)]
    ordered = []
    for p in prefs:
        for i in cands:
            if p in i.lower() and i not in ordered:
                ordered.append(i)
    for i in cands:
        if i not in ordered:
            ordered.append(i)
    if env:
        ordered = [env] + [i for i in ordered if i != env]
    return ordered[:3]

def groq_classify(raw, mode, query, key):
    items, errors, calls = [], [], 0
    models = pick_groq_models(key)
    if not models:
        stat("Groq classifier", False, 0, "no usable model found on this key"); return []
    model = models[0]
    batch = 25
    for i in range(0, len(raw), batch):
        chunk = raw[i:i + batch]
        rows = "\n".join(f"{r['source']} | {r['title']} | {r['summary'][:160]}" for r in chunk)
        done = False
        for attempt in range(4):
            body = {"model": model, "temperature": 0.1, "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": PROMPT.format(mode=mode, query=("Specific ask: " + query) if query else "", rows=rows)}]}
            try:
                calls += 1
                r = requests.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=120)
                if r.status_code == 429:
                    wait = float(r.headers.get("retry-after", 0) or 0) or 10 * (attempt + 1)
                    log(f"groq 429, waiting {wait}s"); time.sleep(min(wait, 60)); continue
                if r.status_code in (400, 404) and "model" in r.text.lower():
                    nxt = models[models.index(model) + 1] if model in models and models.index(model) + 1 < len(models) else None
                    if not nxt:
                        errors.append(f"model {model} rejected: {r.text[:100]}"); break
                    log(f"groq model {model} rejected; switching to {nxt}"); model = nxt; continue
                if r.status_code >= 400:
                    errors.append(f"{r.status_code}: {r.text[:140]}"); time.sleep(3); continue
                content = r.json()["choices"][0]["message"]["content"]
                got = json.loads(content).get("items", [])
                for it in got:
                    if it.get("category") in CATS and it.get("company") and it.get("fact"):
                        it["materiality"] = int(it.get("materiality") or 1)
                        items.append(it)
                done = True
                break
            except Exception as ex:
                errors.append(str(ex)[:140]); time.sleep(3)
        if not done:
            log(f"groq batch {i} gave up")
        time.sleep(1.5)
    log(f"groq classified: {len(items)} items from {len(raw)} headlines in {calls} calls; errors={len(errors)}")
    stat("Groq classifier", len(items) > 0, len(items), ("; ".join(errors[:2]) if errors else ("model=" + model)))
    return items

def dedupe(items):
    """One item per (company, category) for news categories; keep the most material / most detailed."""
    def norm(c):
        c = re.sub(r"\b(ltd|limited|india|corp|corporation|the|co|company|inc)\b", "", (c or "").lower())
        return re.sub(r"[^a-z0-9]", "", c)[:18]
    best = {}
    order = []
    for it in items:
        k = (norm(it.get("company")), it.get("category"))
        if it.get("category") == "calendar":
            k = (norm(it.get("company")), "calendar")
        elif it.get("category") in ("movers", "global") and it.get("fact"):
            k = (norm(it.get("company")), it.get("category"), re.sub(r"\W+", "", it["fact"].lower())[:30])
        if k not in best:
            best[k] = it; order.append(k)
        else:
            cur = best[k]
            score = lambda x: (int(x.get("materiality") or 1), len(str(x.get("fact") or "")), 1 if x.get("value") else 0)
            if score(it) > score(cur):
                best[k] = it
    return [best[k] for k in order]

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="full", choices=["full", "movers", "orders", "results", "policy", "custom"])
    ap.add_argument("--query", default="")
    ap.add_argument("--threshold", type=float, default=10)
    ap.add_argument("--hours", type=float, default=0, help="lookback window in hours (default: since 15:00 IST previous trading day)")
    ap.add_argument("--offline-sample", action="store_true", help="skip network; build from data/sample_raw.json")
    args = ap.parse_args()

    t0 = now_ist()
    if args.hours:
        since = t0 - dt.timedelta(hours=args.hours)
    else:
        prev = t0 - dt.timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= dt.timedelta(days=1)
        since = prev.replace(hour=15, minute=0, second=0, microsecond=0)
    since = since.astimezone(dt.timezone.utc)
    log(f"mode={args.mode} since={since.isoformat()} threshold={args.threshold}")

    raw, movers, glob = [], [], {}
    if args.offline_sample:
        raw = json.loads((DATA / "sample_raw.json").read_text())
    else:
        if args.mode in ("full", "orders", "results", "policy", "custom"):
            raw += fetch_feeds(NEWS_FEEDS, since)
            raw += fetch_feeds(GNEWS_FEEDS, since)
            raw += fetch_bse(since)
        if args.mode in ("full", "policy", "custom"):
            raw += fetch_feeds(POLICY_FEEDS, since)
        if args.mode in ("full", "movers", "custom"):
            intraday = t0.hour >= 9 and (t0.hour, t0.minute) >= (9, 30) and t0.hour < 16
            movers = fetch_movers(args.threshold, intraday)
        if args.mode in ("full", "custom"):
            glob = fetch_global()
    log(f"raw headlines: {len(raw)}")

    key = os.environ.get("GROQ_API_KEY", "").strip()
    raw = prefilter(raw)
    items = groq_classify(raw, args.mode, args.query, key) if (key and raw) else []
    if not items:
        log("falling back to keyword rules"); items = rule_classify(raw)
    if args.mode in ("orders", "results", "policy"):
        items = [i for i in items if i["category"] == args.mode]
    lookups = 0
    for m in movers:
        reason, rsrc = find_reason(m["company"], items, raw)
        if not reason and lookups < 25:
            lookups += 1; reason, rsrc = news_lookup(m["company"]); time.sleep(0.5)
        base = f"₹{m['price']:,} · volume {m['volx']}× average" if m.get("volx") else f"₹{m['price']:,}"
        fact = f"{base}. {reason}" if reason else f"{base}. Reason: none found in today's filings or news"
        items.append({"category": "movers", "company": NAMES.get(m["company"], m["company"]), "ticker": m["company"], "pct": m["pct"], "cap": m.get("cap", ""),
                      "fact": fact, "reasonSource": rsrc or "",
                      "source": "NSE prices", "materiality": 3 if m.get("cap") in ("Large", "Mid") else (2 if abs(m["pct"]) >= 15 else 1)})
    if args.mode in ("full", "results", "custom"):
        cal = fetch_results_calendar()
        seg = segment_map() if cal else {}
        by_name = {}
        for sym, nm in NAMES.items():
            by_name[re.sub(r"[^a-z0-9]", "", nm.lower())[:14]] = sym
        for c in cal:
            key = re.sub(r"[^a-z0-9]", "", c["name"].lower())[:14]
            sym = by_name.get(key)
            cap = seg.get(sym, "") if sym else ""
            try:
                d = dt.datetime.strptime(c["date"][:10].replace("/", "-"), "%Y-%m-%d") if "-" in c["date"] else dt.datetime.strptime(c["date"][:10], "%d %b %Y")
                when = d.strftime("%a %d %b")
            except Exception:
                when = c["date"]
            items.append({"category": "calendar", "company": c["name"], "ticker": sym or "", "cap": cap,
                          "fact": f"Results board meeting · {when}", "when": when, "sortDate": c["date"],
                          "source": "BSE", "materiality": 3 if cap in ("Large", "Mid") else (2 if cap == "Small" else 1)})
    items = dedupe(items)
    items.sort(key=lambda i: (-i.get("materiality", 1), i["category"]))

    label_mode = {"full": "Full digest", "movers": "Movers", "orders": "Orders", "results": "Results", "policy": "Policy", "custom": "Custom: " + args.query[:40]}[args.mode]
    run_id = f"{t0.strftime('%Y-%m-%d')}-{args.mode}"
    n = 2
    while (DATA / f"{run_id}.json").exists():
        run_id = f"{t0.strftime('%Y-%m-%d')}-{args.mode}-{n}"; n += 1

    market = {}
    if glob:
        market = {"sensex": glob.get("sensex", ""), "nifty": glob.get("nifty", ""), "midcap": glob.get("midcap", ""), "smallcap": glob.get("smallcap", ""), "brent": glob.get("brent", ""),
                  "usdinr": glob.get("usdinr", ""), "sp500": glob.get("sp500", ""), "nikkei": glob.get("nikkei", ""),
                  "note": "Index and commodity figures are the latest daily closes from Yahoo Finance."}
    window = f"news since {since.astimezone(IST).strftime('%a %d %b %H:%M')} IST"
    doc = {"id": run_id, "label": f"{t0.strftime('%a %d %b')} {t0.strftime('%H:%M')} IST · {label_mode}", "window": window, "date": t0.strftime("%Y-%m-%d"),
           "generatedAt": iso_utc(), "mode": args.mode, "query": args.query, "threshold": args.threshold,
           "market": market, "items": items[:200],
           "sources": sorted({r.get("link", "") for r in raw if r.get("link")})[:80],
           "classifier": "groq" if key else "rules", "stats": STATS, "rawCount": len(raw)}
    DATA.mkdir(exist_ok=True)
    (DATA / f"{run_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))

    idx_path = DATA / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else {"runs": []}
    index["runs"] = [r for r in index["runs"] if r["id"] != run_id]
    counts = {}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    index["runs"].insert(0, {"id": run_id, "label": doc["label"], "date": doc["date"], "generatedAt": doc["generatedAt"],
                             "mode": args.mode, "count": len(items), "counts": counts})
    index["runs"] = sorted(index["runs"], key=lambda r: r["generatedAt"], reverse=True)[:120]
    index["updatedAt"] = iso_utc()
    idx_path.write_text(json.dumps(index, ensure_ascii=False, indent=1))
    log(f"wrote data/{run_id}.json with {len(items)} items; classifier={doc['classifier']}")

if __name__ == "__main__":
    main()
