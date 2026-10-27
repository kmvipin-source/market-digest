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
    "Google News · ratings": GNEWS.format(q="(upgrade+OR+downgrade+OR+%22target+price%22+OR+%22initiates+coverage%22)+shares+brokerage"),
    "Google News · bulk deals": GNEWS.format(q="%22bulk+deal%22+OR+%22block+deal%22+shares"),
    "Google News · FII DII": GNEWS.format(q="FII+DII+net+crore+(bought+OR+sold)"),
    "Google News · IPO": GNEWS.format(q="IPO+(opens+OR+closes+OR+listing+OR+%22price+band%22+OR+subscribed+OR+allotment)+India"),
    "Google News · SME IPO": GNEWS.format(q="%22SME+IPO%22+(opens+OR+listing+OR+subscription)"),
    "Google News · IPO dates": GNEWS.format(q="IPO+%22opens+on%22+OR+%22closes+on%22+OR+%22listing+date%22+OR+%22allotment%22"),
}
POLICY_FEEDS = {
    "RBI": "https://www.rbi.org.in/pressreleases_rss.xml",
    "SEBI": "https://www.sebi.gov.in/sebirss.xml",
    "PIB": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3",
}
BSE_ANN = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
NSE_RSS = {
    "announcements": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "board": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
    "actions": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
    "ipo": "https://nsearchives.nseindia.com/content/RSS/Public_Issues.xml",
}
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

CATS = ["calendar", "results", "orders", "mna", "policy", "movers", "volume", "deals", "ratings", "actions", "ipo", "global", "other"]
STATS = {}
NAMES = {}
BREADTH = {}
VOLSPIKES = []
PRICES = {}

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

def fetch_nse_rss(kind):
    import feedparser
    try:
        r = get(NSE_RSS[kind], headers={"Referer": "https://www.nseindia.com/"})
        entries = feedparser.parse(r.content).entries
        stat(f"NSE RSS {kind}", len(entries) > 0, len(entries))
        return entries
    except Exception as ex:
        stat(f"NSE RSS {kind}", False, 0, ex); return []

def fetch_nse_announcements(since):
    out = []
    for e in fetch_nse_rss("announcements"):
        title = clean(e.get("title")); desc = clean(e.get("summary", ""))[:300]
        if not title:
            continue
        if re.search(r"(Trading Window|Compliance Certificate|Newspaper Publication|Reg\.? ?74|Investor Presentation|Analyst|Clarification|Record Date Intimation|Loss of Share Certificate|Closure of Trading)", title + " " + desc, re.I) and not re.search(r"(order|contract|acqui|merger|result|dividend|buyback|rating|guidance|bonus|split)", title + " " + desc, re.I):
            continue
        out.append({"source": "NSE filing", "title": f"{title}: {desc}" if desc and desc.lower() not in title.lower() else title, "summary": "", "link": e.get("link", ""), "when": None})
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
    """Companies with results board meetings in the next `days` days. BSE API first, Moneycontrol calendar as fallback."""
    h = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
    d0 = now_ist(); d1 = d0 + dt.timedelta(days=days)
    out, notes = [], []
    attempts = [
        (BSE_BOARD, {"fromdate": d0.strftime("%Y%m%d"), "todate": d1.strftime("%Y%m%d"), "purpose": "P.1", "scripcode": "", "strSearch": "P"}),
        (BSE_FORTH, {"scripcode": "", "Fdate": d0.strftime("%Y%m%d"), "TDate": d1.strftime("%Y%m%d"), "Purpose": "P.1", "strSearch": "P"}),
        (BSE_BOARD, {"Fdate": d0.strftime("%Y%m%d"), "TDate": d1.strftime("%Y%m%d"), "Purpose": "P.1", "scripcode": "", "strSearch": "P"}),
    ]
    for url, params in attempts:
        try:
            r = get(url, params=params, headers=h)
            data = r.json()
            rows = data if isinstance(data, list) else (data.get("Table") or data.get("data") or [])
            notes.append(f"{url.split('/')[-2]}:{len(rows)}")
            for row in rows:
                name = clean(row.get("short_name") or row.get("Long_Name") or row.get("SLONGNAME") or row.get("sLongName") or row.get("LongName") or "")
                date = clean(str(row.get("meeting_date") or row.get("BM_DATE") or row.get("Meeting_Date") or row.get("DT_TM") or row.get("BoardMeetingDate") or ""))
                purpose = clean(row.get("PURPOSE") or row.get("Purpose") or row.get("purpose") or "Results")
                if not name or not date or (purpose and not re.search(r"result", purpose, re.I)):
                    continue
                out.append({"name": name, "date": date[:10], "purpose": purpose[:80]})
            if out:
                break
        except Exception as ex:
            notes.append(f"{url.split('/')[-2]} err {str(ex)[:60]}")
    if not out:
        for e in fetch_nse_rss("board"):
            title = clean(e.get("title")); desc = clean(e.get("summary", ""))
            text = title + " " + desc
            if not re.search(r"result", text, re.I):
                continue
            m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", desc) or re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", title)
            if not m:
                continue
            ds = m.group(1); d = None
            for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    d = dt.datetime.strptime(ds, fmt); break
                except Exception:
                    continue
            if not d or not (d0.date() <= d.date() <= d1.date()):
                continue
            name = re.split(r"\s[-–:]\s|\(", title)[0].strip()
            out.append({"name": name[:60], "date": d.strftime("%Y-%m-%d"), "purpose": "Results"})
        notes.append(f"nse-rss:{len(out)}")
    if not out:
        try:
            r = get("https://www.moneycontrol.com/stocks/marketinfo/results-calendar/", headers={"Referer": "https://www.moneycontrol.com/"})
            html = r.text
            # rows look like: <td ...><a ...>Company</a></td> ... <td>27-Oct-2026</td>
            for m in re.finditer(r'<a[^>]+href="[^"]*/stockpricequote/[^"]*"[^>]*>([^<]{2,60})</a>.{0,600}?(\d{1,2}-[A-Za-z]{3}-\d{4})', html, re.S):
                name, date = clean(m.group(1)), m.group(2)
                try:
                    d = dt.datetime.strptime(date, "%d-%b-%Y").replace(tzinfo=IST)
                except Exception:
                    continue
                if d0.date() <= d.date() <= d1.date():
                    out.append({"name": name, "date": d.strftime("%Y-%m-%d"), "purpose": "Results"})
            notes.append(f"moneycontrol:{len(out)}")
        except Exception as ex:
            notes.append(f"moneycontrol err {str(ex)[:60]}")
    seen = set(); uniq = []
    for c in out:
        k = (c["name"].lower(), c["date"])
        if k not in seen:
            seen.add(k); uniq.append(c)
    log(f"results calendar: {len(uniq)} companies ({'; '.join(notes)})"); stat("Results calendar", len(uniq) > 0, len(uniq), "; ".join(notes))
    return uniq

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
    BREADTH.update({"scanned": 0, "advances": 0, "declines": 0, "up5": 0, "down5": 0, "up10": 0, "down10": 0})
    VOLSPIKES.clear()
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
            BREADTH["scanned"] += 1
            PRICES[s] = [NAMES.get(s, s)[:40], round(px, 2), round(pct, 2), round((vol / avgv), 1) if avgv else 0]
            if pct > 0: BREADTH["advances"] += 1
            elif pct < 0: BREADTH["declines"] += 1
            if pct >= 5: BREADTH["up5"] += 1
            if pct <= -5: BREADTH["down5"] += 1
            if pct >= 10: BREADTH["up10"] += 1
            if pct <= -10: BREADTH["down10"] += 1
            volx = (vol / avgv) if avgv else 0
            if volx >= 5 and abs(pct) < threshold and px * vol >= 2e7:
                VOLSPIKES.append({"company": s, "pct": round(pct, 2), "price": round(px, 2), "volx": round(volx, 1)})
            if abs(pct) >= threshold:
                out.append({"company": s, "pct": round(pct, 2), "price": round(px, 2),
                            "volx": round(vol / avgv, 1) if avgv else None})
        except Exception:
            continue
    VOLSPIKES.sort(key=lambda x: -x["volx"]); del VOLSPIKES[20:]
    seg = segment_map()
    for m in out + VOLSPIKES:
        m["cap"] = seg.get(m["company"], "Micro")
    flag52(out + VOLSPIKES)
    out.sort(key=lambda x: -abs(x["pct"]))
    log(f"movers >= {threshold}%: {len(out)} of {len(syms)} scanned"); stat("NSE prices (Yahoo)", True, len(out), f"scanned {len(syms)}")
    return out[:60]

def flag52(rows):
    """Mark 52-week high/low for a small set of symbols (one yfinance call)."""
    if not rows:
        return
    try:
        import yfinance as yf
        tickers = [r["company"] + ".NS" for r in rows][:80]
        df = yf.download(tickers, period="1y", interval="1d", group_by="ticker", threads=True, progress=False, auto_adjust=False)
        for r in rows[:80]:
            try:
                sub = (df[r["company"] + ".NS"] if len(tickers) > 1 else df).dropna(how="all")
                hi, lo, px = float(sub["High"].max()), float(sub["Low"].min()), float(sub.iloc[-1]["Close"])
                if px >= hi * 0.995: r["w52"] = "52-week high"
                elif px <= lo * 1.005: r["w52"] = "52-week low"
                r["fromHigh"] = round((px / hi - 1) * 100, 1)
            except Exception:
                pass
    except Exception as ex:
        log(f"52w flags FAILED: {ex}")

def fetch_global():
    try:
        import yfinance as yf
    except Exception:
        return {}
    idx = {"sensex": "^BSESN", "nifty": "^NSEI", "midcap": "^NSEMDCP50", "smallcap": "^CNXSC", "banknifty": "^NSEBANK", "vix": "^INDIAVIX",
           "sp500": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI", "nikkei": "^N225", "hangseng": "^HSI", "kospi": "^KS11",
           "brent": "BZ=F", "usdinr": "INR=X", "gold": "GC=F", "silver": "SI=F", "us10y": "^TNX", "dxy": "DX-Y.NYB",
           "sec_it": "^CNXIT", "sec_auto": "^CNXAUTO", "sec_pharma": "^CNXPHARMA", "sec_metal": "^CNXMETAL", "sec_realty": "^CNXREALTY",
           "sec_fmcg": "^CNXFMCG", "sec_psubank": "^CNXPSUBANK", "sec_energy": "^CNXENERGY", "sec_infra": "^CNXINFRA", "sec_media": "^CNXMEDIA"}
    out = {}
    alt = {"smallcap": ["^CNXSC", "^CNXSMCP", "NIFTYSMLCAP100.NS", "NIFTY_SMLCAP_100.NS", "^NSMIDCP", "SMALLCAP.BO", "^BSESMC"], "midcap": ["^NSEMDCP50", "NIFTYMIDCAP150.NS", "^CNXMIDCAP", "^BSEMDC"]}
    try:
        for k, cands in alt.items():
            for c in cands:
                try:
                    t = yf.download(c, period="5d", interval="1d", progress=False, auto_adjust=False)
                    if len(t.dropna(how="all")) >= 2:
                        idx[k] = c; break
                except Exception:
                    continue
        df = yf.download(list(idx.values()), period="5d", interval="1d", group_by="ticker", progress=False, auto_adjust=False)
        asof = None
        for k, t in idx.items():
            sub = df[t].dropna(how="all")
            if len(sub) >= 2:
                if k in ("nifty", "sensex"):
                    try:
                        ts = sub.index[-1].to_pydatetime()
                        asof = ts
                    except Exception:
                        pass
                last, prev = float(sub.iloc[-1]["Close"]), float(sub.iloc[-2]["Close"])
                pct = (last / prev - 1) * 100; chg = last - prev
                dp = 2 if last < 1000 else 0
                out[k] = f"{last:,.{dp}f} · {chg:+,.{dp}f} ({pct:+.2f}%)"
        # quote freshness: during market hours (09:15-15:30 IST) try a 5-minute bar for the latest Nifty/Sensex print
        t_now = now_ist()
        if t_now.weekday() < 5 and (9, 15) <= (t_now.hour, t_now.minute) <= (15, 35):
            try:
                live = yf.download(["^NSEI", "^BSESN"], period="1d", interval="5m", group_by="ticker", progress=False, auto_adjust=False)
                for k, t in (("nifty", "^NSEI"), ("sensex", "^BSESN")):
                    sub = live[t].dropna(how="all")
                    if len(sub):
                        px = float(sub.iloc[-1]["Close"]); prev = float(df[t].dropna(how="all").iloc[-2]["Close"])
                        out[k] = f"{px:,.0f} · {px - prev:+,.0f} ({(px / prev - 1) * 100:+.2f}%)"
                        asof = sub.index[-1].to_pydatetime()
                out["quoteNote"] = "live"
            except Exception as ex:
                log(f"live quote failed: {ex}")
        if asof is not None:
            try:
                if asof.tzinfo is None:
                    asof = asof.replace(tzinfo=dt.timezone.utc)
                out["asOf"] = (asof.astimezone(IST).strftime("%a %d %b %H:%M") + " IST (live)") if out.get("quoteNote") == "live" else (asof.strftime("%a %d %b") + " close")
            except Exception:
                pass
    except Exception as ex:
        log(f"global FAILED: {ex}")
    return out

# ---------------------------------------------------------------- extra NSE/BSE datasets
DELIV = {}
def last_trading_days(n=4):
    d = now_ist()
    if (d.hour, d.minute) < (18, 30):  # today's files publish in the evening
        d -= dt.timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out

def nse_csv(url_fmt):
    """Try the newest trading days until a file exists."""
    for d in last_trading_days():
        url = url_fmt.format(d=d)
        try:
            r = get(url, headers={"Referer": "https://www.nseindia.com/"})
            if r.status_code == 200 and len(r.text) > 200 and "<html" not in r.text[:200].lower():
                return r.text, d
        except Exception:
            continue
    return None, None

def fetch_delivery():
    """Delivery % per symbol from NSE's full bhavcopy."""
    text, d = nse_csv("https://archives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv")
    if not text:
        stat("Delivery % (NSE bhavcopy)", False, 0, "no file"); return
    import csv, io
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if row.get("SERIES") not in ("EQ", "BE"):
            continue
        try:
            DELIV[row["SYMBOL"]] = float(row.get("DELIV_PER") or 0); n += 1
        except Exception:
            pass
    stat("Delivery % (NSE bhavcopy)", n > 0, n, f"for {d:%d %b}" if d else "")

def fetch_deals():
    """Bulk and block deals (NSE daily files)."""
    import csv, io
    out = []
    for kind, url in (("Bulk", "https://archives.nseindia.com/content/equities/bulk.csv"), ("Block", "https://archives.nseindia.com/content/equities/block.csv")):
        try:
            r = get(url, headers={"Referer": "https://www.nseindia.com/"})
            rows = list(csv.DictReader(io.StringIO(r.text)))
            for row in rows:
                row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                sym = row.get("Symbol") or row.get("SYMBOL"); name = row.get("Security Name") or row.get("SECURITY NAME") or sym
                client = row.get("Client Name") or row.get("CLIENT NAME") or ""
                side = row.get("Buy/Sell") or row.get("BUY/SELL") or ""
                qty = (row.get("Quantity Traded") or row.get("QUANTITY TRADED") or "0").replace(",", "")
                px = (row.get("Trade Price / Wght. Avg. Price") or row.get("TRADE PRICE / WGHT. AVG. PRICE") or row.get("Trade Price") or "0").replace(",", "")
                date = row.get("Date") or row.get("DATE") or ""
                try:
                    val = float(qty) * float(px) / 1e7
                except Exception:
                    val = 0
                if not sym or val < 1:  # ignore < ₹1 Cr
                    continue
                out.append({"kind": kind, "sym": sym, "name": name, "client": client[:60], "side": side.upper()[:4], "qty": int(float(qty)), "px": float(px), "valCr": round(val, 1), "date": date})
        except Exception as ex:
            log(f"{kind} deals FAILED: {ex}")
    out.sort(key=lambda x: -x["valCr"])
    stat("Bulk/block deals (NSE)", len(out) > 0, len(out))
    return out[:40]

def fetch_52w():
    """Symbols that set a new 52-week high or low in the latest session (NSE file)."""
    import csv, io
    text, d = nse_csv("https://archives.nseindia.com/content/CM_52_wk_High_low_{d:%d%m%Y}.csv")
    if not text:
        stat("52-week lists (NSE)", False, 0, "no file"); return [], []
    highs, lows = [], []
    lines = text.splitlines()
    # file has a few header lines before the real header
    start = next((i for i, l in enumerate(lines) if l.strip().strip('"').upper().startswith("SYMBOL")), 0)
    today = d.strftime("%d-%b-%Y") if d else ""
    header = lines[start] if lines else ""
    log(f"52w header: {header[:160]}")
    sample = lines[start + 1][:160] if len(lines) > start + 1 else ""
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        row = {k.strip().upper(): (v or "").strip() for k, v in row.items() if k}
        if row.get("SERIES", "EQ") not in ("EQ", "BE", ""):
            continue
        sym = row.get("SYMBOL")
        hd = next((v for k, v in row.items() if "HIGH" in k and ("DATE" in k or "DT" in k)), "")
        ld = next((v for k, v in row.items() if "LOW" in k and ("DATE" in k or "DT" in k)), "")
        if sym and today and hd.lower().startswith(today.lower()[:6]):
            highs.append(sym)
        if sym and today and ld.lower().startswith(today.lower()[:6]):
            lows.append(sym)
    stat("52-week lists (NSE)", len(highs) + len(lows) > 0, len(highs) + len(lows), (f"for {d:%d %b}; " if d else "") + "row: " + sample[:110])
    return highs, lows

def fetch_flows(raw):
    """FII/DII cash-market flows from the day's headlines (published by exchanges after close; reported widely)."""
    best = ""
    for e in (fetch_nse_rss("fii") if "fii" in NSE_RSS else []):
        text = clean(e.get("title")) + " " + clean(e.get("summary", ""))
        f = re.search(r"FII[^\d-]{0,40}?(-?[\d,]+\.?\d*)", text); d = re.search(r"DII[^\d-]{0,40}?(-?[\d,]+\.?\d*)", text)
        if f or d:
            parts = []
            if f: parts.append(f"FII {'' if f.group(1).startswith('-') else '+'}₹{f.group(1)} Cr")
            if d: parts.append(f"DII {'' if d.group(1).startswith('-') else '+'}₹{d.group(1)} Cr")
            best = " / ".join(parts) + " (NSE)"; break
    for rr in ([] if best else raw):
        text = rr["title"] + " " + rr["summary"]
        f = re.search(r"(?:FII|FPI)s?[^.;]{0,80}?(bought|buy|buying|sold|sell|selling|offload|net (?:buy|sell)\w*)[^.;]{0,60}?(?:₹|Rs\.?|INR)?\s?([\d,]+(?:\.\d+)?)\s*(crore|cr)", text, re.I)
        d = re.search(r"DIIs?[^.;]{0,80}?(bought|buy|buying|sold|sell|selling|net (?:buy|sell)\w*)[^.;]{0,60}?(?:₹|Rs\.?|INR)?\s?([\d,]+(?:\.\d+)?)\s*(crore|cr)", text, re.I)
        if f or d:
            parts = []
            if f: parts.append(f"FII {'+' if re.search('bou|buy', f.group(1), re.I) else '-'}₹{f.group(2)} Cr")
            if d: parts.append(f"DII {'+' if re.search('bou|buy', d.group(1), re.I) else '-'}₹{d.group(2)} Cr")
            best = " / ".join(parts) + f" (per {rr['source']})"
            break
    sample = next((rr["title"][:100] for rr in raw if re.search(r"\bFII|\bFPI|\bDII", rr["title"])), "")
    stat("FII/DII flows (news)", bool(best), 1 if best else 0, "" if best else ("not parsed; e.g. " + sample if sample else "no FII headlines"))
    return best

def fetch_corp_actions(days=7):
    """Ex-dates for dividends, bonus, splits, rights in the next `days` days (BSE published corporate-actions API)."""
    d0 = now_ist(); d1 = d0 + dt.timedelta(days=days)
    out = []
    for e in fetch_nse_rss("actions"):
        title = clean(e.get("title")); desc = clean(e.get("summary", ""))
        text = title + " " + desc
        if not re.search(r"dividend|bonus|split|rights|buyback|buy back", text, re.I):
            continue
        m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", desc) or re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", title)
        d = None
        if m:
            for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    d = dt.datetime.strptime(m.group(1), fmt); break
                except Exception:
                    continue
        if d and not (d0.date() <= d.date() <= d1.date()):
            continue
        name = re.split(r"\s[-–:]\s|\(", title)[0].strip()
        purpose = re.sub(r"^\s*" + re.escape(name) + r"\s*[-–:]?\s*", "", desc or title)
        pm = re.search(r"PURPOSE\s*:\s*([^|]+)", purpose, re.I)
        purpose = (pm.group(1).strip() if pm else purpose).strip()
        purpose = purpose[:1].upper() + purpose[1:].lower() if purpose.isupper() else purpose
        purpose = purpose[:90]
        out.append({"sym": "", "name": name[:60], "purpose": purpose or "Corporate action", "exDate": d.strftime("%Y-%m-%d") if d else ""})
    if out:
        stat("Corporate actions (NSE RSS)", True, len(out)); return out[:60]
    try:
        h = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
        r = get("https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w", params={"scripcode": "", "Fdate": d0.strftime("%Y%m%d"), "TDate": d1.strftime("%Y%m%d"), "Purposecode": "", "strSearch": "S", "ddlcategorys": "E", "ddlindustrys": ""}, headers=h)
        data = r.json(); rows = data if isinstance(data, list) else (data.get("Table") or [])
        for row in rows:
            purpose = clean(row.get("Purpose") or row.get("purpose_name") or ""); ex = clean(str(row.get("Ex_date") or row.get("exdate") or ""))
            name = clean(row.get("short_name") or row.get("long_name") or "")
            if name and re.search(r"dividend|bonus|split|rights|buyback|buy back", purpose, re.I):
                out.append({"sym": "", "name": name, "purpose": purpose[:90], "exDate": ex[:10]})
    except Exception as ex:
        log(f"corp actions BSE failed: {ex}")
    stat("Corporate actions (BSE)", len(out) > 0, len(out))
    return out[:60]

def index_dma():
    """Nifty / Midcap / Smallcap vs 50- and 200-day averages."""
    try:
        import yfinance as yf
        res = {}
        for label, t in (("Nifty 50", "^NSEI"), ("Midcap", "^NSEMDCP50"), ("Smallcap", "^CNXSC")):
            df = yf.download(t, period="1y", interval="1d", progress=False, auto_adjust=False)
            if df is None or len(df) < 60:
                continue
            close = df["Close"].squeeze().dropna()
            px, d50, d200 = float(close.iloc[-1]), float(close.tail(50).mean()), float(close.tail(200).mean()) if len(close) >= 200 else None
            res[label] = {"px": round(px), "dma50": round(d50), "dma200": round(d200) if d200 else None,
                          "vs50": f"{(px / d50 - 1) * 100:+.1f}%", "vs200": f"{(px / d200 - 1) * 100:+.1f}%" if d200 else ""}
        stat("Index DMAs", len(res) > 0, len(res))
        return res
    except Exception as ex:
        log(f"dma failed: {ex}"); stat("Index DMAs", False, 0, ex); return {}

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

# ---------------------------------------------------------------- LLM providers
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"]
_GEMINI_CACHE = None
def gemini_models(key):
    """Discover available Gemini models for this key; prefer flash-class, newest first."""
    global _GEMINI_CACHE
    if _GEMINI_CACHE is not None:
        return _GEMINI_CACHE
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=100", timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if "flash" in n and "tts" not in n and "image" not in n and "audio" not in n and "live" not in n and "embedding" not in n]
        def rank(n):
            m = re.search(r"(\d+(?:\.\d+)?)", n); v = float(m.group(1)) if m else 0
            return (-v, "lite" in n, "latest" not in n, "preview" in n, len(n))
        flash.sort(key=rank)
        pro = [n for n in names if "pro" in n and "tts" not in n and "image" not in n]
        pro.sort(key=rank)
        _GEMINI_CACHE = (flash[:4] or GEMINI_MODELS) + pro[:1]
        log(f"gemini models: {_GEMINI_CACHE}")
    except Exception as ex:
        log(f"gemini model list failed: {ex}"); _GEMINI_CACHE = GEMINI_MODELS
    return _GEMINI_CACHE

def llm_json(prompt, key_groq, big=False, timeout=120):
    """Ask whichever provider is configured for a JSON object. Returns (dict|None, provider_note)."""
    gem = os.environ.get("GEMINI_API_KEY", "").strip()
    cf_acct, cf_tok = os.environ.get("CF_ACCOUNT_ID", "").strip(), os.environ.get("CF_API_TOKEN", "").strip()
    errs = []
    if gem:
        for model in gemini_models(gem):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gem}",
                                  json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}, timeout=timeout)
                if r.status_code == 404:
                    continue
                if r.status_code in (429, 503):
                    errs.append(f"gemini {model} {r.status_code}"); time.sleep(4); continue
                if r.status_code >= 400:
                    errs.append(f"gemini {model} {r.status_code}: {r.text[:80]}"); continue
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                a, b = text.find("{"), text.rfind("}")
                return json.loads(text[a:b + 1]), f"gemini:{model}"
            except Exception as ex:
                errs.append(f"gemini {model} {str(ex)[:60]}")
    if cf_acct and cf_tok:
        for model in (["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/meta/llama-3.1-8b-instruct-fast"] if big else ["@cf/meta/llama-3.1-8b-instruct-fast", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"]):
            try:
                r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{cf_acct}/ai/run/{model}", headers={"Authorization": f"Bearer {cf_tok}"},
                                  json={"messages": [{"role": "user", "content": prompt}], "max_tokens": 4000}, timeout=timeout)
                if r.status_code >= 400:
                    errs.append(f"cf {model} {r.status_code}"); continue
                text = r.json().get("result", {}).get("response", "")
                a, b = text.find("{"), text.rfind("}")
                return json.loads(text[a:b + 1]), f"cloudflare:{model}"
            except Exception as ex:
                errs.append(f"cf {str(ex)[:60]}")
    if key_groq:
        for model in pick_groq_models(key_groq, prefer="big" if big else "light"):
            try:
                body = {"model": model, "temperature": 0.1, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]}
                r = requests.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {key_groq}"}, timeout=timeout)
                if r.status_code == 400 and "json" in r.text.lower():
                    body.pop("response_format", None)
                    r = requests.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {key_groq}"}, timeout=timeout)
                if r.status_code == 429:
                    errs.append(f"groq {model} 429"); continue
                if r.status_code >= 400:
                    errs.append(f"groq {model} {r.status_code}"); continue
                text = r.json()["choices"][0]["message"]["content"]
                a, b = text.find("{"), text.rfind("}")
                return json.loads(text[a:b + 1]), f"groq:{model}"
            except Exception as ex:
                errs.append(f"groq {str(ex)[:60]}")
    return None, "; ".join(errs[:3]) or "no provider configured"

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
PROMPT = """You turn Indian stock-market headlines into a structured digest. Rules: facts only, in your own words, each fact at most 25 words with the numbers; never add buy/sell/target language; drop items that are not material market news (listicles, opinion, lifestyle, ads, routine compliance filings, IPO GMP chatter); merge duplicates of the same story; company = the listed company or body (e.g. "RBI", "Bond market"); materiality 1-3 (3 = index name, or deal/number large relative to the company). Categories: results (quarterly numbers, guidance, dividends, buybacks), orders (order wins, contracts, LOIs), mna (acquisitions, stake sales, mergers, open offers), policy (RBI/SEBI/government/regulation/macro-India), global (overseas markets, crude, currency), ratings (brokerage upgrades/downgrades, target changes, coverage initiations: state the broker, the new stance and target as reported), other (management changes, fund-raises, legal). MODE: {mode}. {query}
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
PROMPT = """You turn Indian stock-market headlines into a structured digest. Rules: facts only, in your own words, each fact at most 25 words with the numbers; never add buy/sell/target language; drop items that are not material market news (listicles, opinion, lifestyle, ads, routine compliance filings, IPO GMP chatter); merge duplicates of the same story; company = the listed company or body (e.g. "RBI", "Bond market"); materiality 1-3 (3 = index name, or deal/number large relative to the company). Categories: results (quarterly numbers, guidance, dividends, buybacks), orders (order wins, contracts, LOIs), mna (acquisitions, stake sales, mergers, open offers), policy (RBI/SEBI/government/regulation/macro-India), global (overseas markets, crude, currency), ratings (brokerage upgrades/downgrades, target changes, coverage initiations: state the broker, the new stance and target as reported), other (management changes, fund-raises, legal). MODE: {mode}. {query}
Return ONLY a JSON object: {{"items":[{{"category":"...","company":"...","fact":"...","value":"<rupee amount for orders/deals or omit>","source":"<source name as given>","materiality":1}}]}}
HEADLINES (source | title | summary):
{rows}"""

def pick_groq_models(key, prefer="light"):
    """Ask Groq which models this key can use and order them by preference."""
    env = os.environ.get("GROQ_MODEL", "").strip()
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", []) if m.get("active", True)]
    except Exception as ex:
        log(f"groq model list FAILED: {ex}"); ids = []
    log(f"groq models available: {ids}")
    prefs = (["llama-3.3-70b", "llama-4-scout", "gpt-oss-20b", "qwen", "llama-3.1-8b", "gpt-oss-120b", "llama-4-maverick", "llama", "mixtral", "gemma"] if prefer == "light"
             else ["llama-4-maverick", "llama-3.3-70b", "gpt-oss-120b", "llama-4-scout", "qwen", "gpt-oss-20b", "llama-3.1-8b", "llama", "mixtral", "gemma"])
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
    budget_end = time.time() + float(os.environ.get("GROQ_BUDGET_SEC", "420"))
    provider_used = ""
    batch = 35
    for i in range(0, len(raw), batch):
        if time.time() > budget_end:
            errors.append("time budget exhausted"); break
        chunk = raw[i:i + batch]
        rows = "\n".join(f"{r['source']} | {r['title']} | {r['summary'][:160]}" for r in chunk)
        calls += 1
        got, note = llm_json(PROMPT.format(mode=mode, query=("Specific ask: " + query) if query else "", rows=rows), key, big=False)
        if got is None:
            errors.append(note)
            if "429" in note and len(errors) >= 3:
                break
            continue
        provider_used = note
        for it in got.get("items", []):
            if isinstance(it, dict) and it.get("category") in CATS and it.get("company") and it.get("fact"):
                it["materiality"] = int(it.get("materiality") or 1)
                items.append(it)
        time.sleep(0.4)
    log(f"llm classified: {len(items)} items from {len(raw)} headlines in {calls} calls; errors={len(errors)}")
    stat("News classifier (LLM)", len(items) > 0, len(items), (provider_used + ("; " if errors else "") + "; ".join(errors[:2])).strip("; "))
    if items and len(items) < len(raw) * 0.15 and errors:
        log("coverage low; merging keyword fallback")
        seen = {re.sub(r"\W+", "", (i.get("company", "") + i.get("fact", "")).lower())[:30] for i in items}
        for it in rule_classify(raw):
            k = re.sub(r"\W+", "", (it.get("company", "") + it.get("fact", "")).lower())[:30]
            if k not in seen:
                items.append(it)
    return items

def dedupe(items):
    """One item per (company, category) for news categories; keep the most material / most detailed."""
    def norm(c):
        c = (c or "").lower()
        if re.search(r"sensex|nifty|d-street|dalal|indian (shares|stocks|markets)|^markets?$|stock market", c):
            return "indexwrap"
        c = re.sub(r"\b(ltd|limited|india|corp|corporation|the|co|company|inc)\b", "", c)
        return re.sub(r"[^a-z0-9]", "", c)[:18]
    best = {}
    order = []
    for it in items:
        k = (norm(it.get("company")), it.get("category"))
        if it.get("category") == "calendar":
            k = (norm(it.get("company")), "calendar")
        elif it.get("category") == "ipo":
            k = (norm(it.get("company")), "ipo")
        elif it.get("category") in ("deals", "actions") or it.get("subtype"):
            k = (norm(it.get("company")), it.get("category"), it.get("subtype", ""), re.sub(r"\W+", "", str(it.get("fact", "")).lower())[:40])
        elif norm(it.get("company")) == "indexwrap":
            k = ("indexwrap", "global"); it["category"] = "global"; it["company"] = "Sensex / Nifty"
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

# ---------------------------------------------------------------- IPOs
IPO_PROMPT = """From these headlines about Indian IPOs (mainboard and SME), list each distinct IPO with what is stated. Dates must be ISO (YYYY-MM-DD) and only if the headline/summary gives them (today is {today}); leave unknown fields empty. No grey-market premium, no recommendations.
Return ONLY JSON: {{"ipos":[{{"name":"...","platform":"Mainboard|SME","status":"upcoming|open|closed|listed","openDate":"","closeDate":"","listingDate":"","priceBand":"₹x-y","issueSize":"₹x Cr","subscription":"x times (day n)","listingNote":"listed at ₹x, +y% (if listed)","source":"..."}}]}}
HEADLINES (source | title | summary):
{rows}"""

def extract_ipos(raw, key):
    rows = [r for r in raw if re.search(r"\bIPO\b|public issue|listing", r["title"] + " " + r["summary"], re.I)]
    if not rows:
        stat("IPO extraction", False, 0, "no IPO headlines"); return []
    rows = rows[:80]
    ipos = []
    text = "\n".join(f"{r['source']} | {r['title']} | {r['summary'][:220]}" for r in rows)
    got, note = llm_json(IPO_PROMPT.format(today=now_ist().strftime("%Y-%m-%d"), rows=text), key, big=True)
    if got:
        ipos = got.get("ipos", []) or []
    if not ipos:
        seen = set()
        for r in rows:
            m = re.search(r"^(.*?)\s+(?:SME\s+)?IPO", r["title"], re.I)
            name = (m.group(1) if m else r["title"]).strip(" :-|")[:60]
            k = name.lower()[:20]
            if not name or k in seen:
                continue
            seen.add(k)
            st = "listed" if re.search(r"list(ed|s|ing) (at|debut)|debuts", r["title"], re.I) else ("open" if re.search(r"opens|subscri|day \d", r["title"], re.I) else "upcoming")
            ipos.append({"name": name, "platform": "SME" if re.search(r"\bSME\b", r["title"], re.I) else "Mainboard", "status": st, "listingNote": r["title"][:120], "source": r["source"]})
    out = []
    seen = set()
    for i in ipos:
        name = str(i.get("name", "")).strip()
        if not name or name.lower()[:20] in seen:
            continue
        seen.add(name.lower()[:20])
        out.append({k: (str(v).strip() if v is not None else "") for k, v in i.items()})
    stat("IPO extraction", len(out) > 0, len(out), "llm" if got else "rules")
    return out[:40]

# ---------------------------------------------------------------- brief
BRIEF_PROMPT = """You are writing the morning read for an Indian equity trader who focuses on mid and small caps. Use ONLY the data below. No buy/sell/target language and no price predictions: describe the setup and what is worth researching, and say why using the numbers given. Plain English, concise.
Return ONLY JSON:
{{"regime": "2-3 sentences on market regime: breadth, VIX, leading/lagging sectors, macro pressure (crude, rupee, yields, FII)",
 "leads": [{{"company": "...", "cap": "Large|Mid|Small|Micro|", "why": "one sentence with the concrete signals (move %, volume x, 52-week context, order/result/deal with value)", "tags": ["momentum","volume","order win","results","deal","policy","reversal","breakout"]}}],
 "week": ["3-6 bullets: the most material results and policy events ahead, with dates where given"],
 "risks": ["2-4 bullets: what could change the picture, grounded in the data"]}}
Pick 6-8 leads; prefer Mid/Small/Micro caps with more than one signal (price + volume + a filing or news). Exclude index-level items from leads.

MARKET: {market}
BREADTH: {breadth}
SECTORS: {sectors}
ITEMS (category | company | cap | pct | fact):
{rows}"""

def groq_brief(doc, key):
    m = doc.get("market", {})
    rows = []
    for it in sorted(doc["items"], key=lambda i: -int(i.get("materiality") or 1))[:120]:
        rows.append(f"{it.get('category')} | {it.get('company')} | {it.get('cap','')} | {it.get('pct','')} | {str(it.get('fact',''))[:160]}")
    prompt = BRIEF_PROMPT.format(market={k: v for k, v in m.items() if k not in ("sectors", "breadth", "note", "dma")}, breadth=m.get("breadth", {}), sectors=m.get("sectors", {}), rows="\n".join(rows))
    got, note = llm_json(prompt, key, big=True)
    if not got:
        got, note2 = llm_json(prompt, key, big=False); note = note + " | " + note2
    if got and (got.get("regime") or got.get("leads")):
        stat("Morning read (LLM)", True, len(got.get("leads", [])), note); return got
    stat("Morning read (LLM)", False, 0, note); return None

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
            raw += fetch_nse_announcements(since)
        if args.mode in ("full", "policy", "custom"):
            raw += fetch_feeds(POLICY_FEEDS, since)
        if args.mode in ("full", "movers", "custom"):
            intraday = t0.hour >= 9 and (t0.hour, t0.minute) >= (9, 30) and t0.hour < 16
            movers = fetch_movers(args.threshold, intraday)
        if args.mode in ("full", "custom"):
            glob = fetch_global()
    log(f"raw headlines: {len(raw)}")

    key = os.environ.get("GROQ_API_KEY", "").strip()
    any_llm = bool(key or os.environ.get("GEMINI_API_KEY") or os.environ.get("CF_API_TOKEN"))
    raw = prefilter(raw)
    items = groq_classify(raw, args.mode, args.query, key) if (any_llm and raw) else []
    if not items:
        log("falling back to keyword rules"); items = rule_classify(raw)
    if args.mode in ("orders", "results", "policy"):
        items = [i for i in items if i["category"] == args.mode]
    deals, highs, lows, actions, dma, flows = [], [], [], [], {}, ""
    if args.mode in ("full", "movers", "custom"):
        fetch_delivery()
        deals = fetch_deals()
        highs, lows = fetch_52w()
        dma = index_dma()
    if args.mode in ("full", "custom"):
        flows = fetch_flows(raw)
        actions = fetch_corp_actions()
    lookups = 0
    for m in movers:
        reason, rsrc = find_reason(m["company"], items, raw)
        if not reason and lookups < 15:
            lookups += 1; reason, rsrc = news_lookup(m["company"]); time.sleep(0.3)
        base = f"₹{m['price']:,} · volume {m['volx']}× average" if m.get("volx") else f"₹{m['price']:,}"
        if m.get("w52"): base += f" · {m['w52']}"
        elif m.get("fromHigh") is not None: base += f" · {m['fromHigh']:+.0f}% from 52-wk high"
        if DELIV.get(m["company"]) is not None: base += f" · delivery {DELIV[m['company']]:.0f}%"
        fact = f"{base}. {reason}" if reason else f"{base}. Reason: none found in today's filings or news"
        items.append({"category": "movers", "company": NAMES.get(m["company"], m["company"]), "ticker": m["company"], "pct": m["pct"], "cap": m.get("cap", ""),
                      "fact": fact, "reasonSource": rsrc or "",
                      "source": "NSE prices", "materiality": 3 if m.get("cap") in ("Large", "Mid") else (2 if abs(m["pct"]) >= 15 else 1)})
    for v in VOLSPIKES:
        reason, rsrc = find_reason(v["company"], items, raw)
        extra = (f" · {v['w52']}" if v.get("w52") else "") + (f" · delivery {DELIV[v['company']]:.0f}%" if DELIV.get(v["company"]) is not None else "")
        items.append({"category": "volume", "company": NAMES.get(v["company"], v["company"]), "ticker": v["company"], "pct": v["pct"], "cap": v.get("cap", ""),
                      "fact": f"Volume {v['volx']}× average at ₹{v['price']:,}{extra}. " + (reason or "Reason: none found in today's filings or news"),
                      "reasonSource": rsrc or "", "source": "NSE prices", "materiality": 2 if v.get("cap") in ("Large", "Mid") else 1})
    seg_all = segment_map() if (deals or highs or lows) else {}
    for dl in deals:
        cap = seg_all.get(dl["sym"], "")
        items.append({"category": "deals", "company": NAMES.get(dl["sym"], dl["name"]), "ticker": dl["sym"], "cap": cap, "value": f"₹{dl['valCr']:,} Cr",
                      "fact": f"{dl['kind']} deal · {dl['client'] or 'undisclosed'} {('bought' if dl['side'].startswith('B') else 'sold')} {dl['qty']:,} shares at ₹{dl['px']:,}",
                      "when": dl["date"], "source": "NSE", "materiality": 3 if dl["valCr"] >= 50 else (2 if dl["valCr"] >= 10 else 1)})
    for sym in highs[:60]:
        if any(i.get("ticker") == sym and i["category"] in ("movers", "volume") for i in items):
            continue
        p = PRICES.get(sym)
        items.append({"category": "other", "subtype": "high52", "company": NAMES.get(sym, sym), "ticker": sym, "cap": seg_all.get(sym, ""), "pct": p[2] if p else None,
                      "fact": "New 52-week high" + (f" at ₹{p[1]:,}" if p else "") + (f" · delivery {DELIV[sym]:.0f}%" if DELIV.get(sym) is not None else ""), "source": "NSE", "materiality": 2 if seg_all.get(sym) in ("Large", "Mid", "Small") else 1})
    for sym in lows[:40]:
        p = PRICES.get(sym)
        items.append({"category": "other", "subtype": "low52", "company": NAMES.get(sym, sym), "ticker": sym, "cap": seg_all.get(sym, ""), "pct": p[2] if p else None,
                      "fact": "New 52-week low" + (f" at ₹{p[1]:,}" if p else ""), "source": "NSE", "materiality": 2 if seg_all.get(sym) in ("Large", "Mid", "Small") else 1})
    for a in actions:
        items.append({"category": "actions", "company": a["name"], "ticker": a.get("sym", ""), "fact": a["purpose"], "when": a.get("exDate", ""), "sortDate": a.get("exDate", ""), "source": "NSE/BSE", "materiality": 1})
    if args.mode in ("full", "custom"):
        ipo_raw = list(raw)
        for e in fetch_nse_rss("ipo"):
            ipo_raw.append({"source": "NSE public issues", "title": clean(e.get("title")), "summary": clean(e.get("summary", ""))[:300], "link": e.get("link", ""), "when": None})
        for i in extract_ipos(ipo_raw, key):
            bits = []
            if i.get("priceBand"): bits.append(f"band {i['priceBand']}")
            if i.get("issueSize"): bits.append(f"size {i['issueSize']}")
            if i.get("subscription"): bits.append(f"subscribed {i['subscription']}")
            if i.get("listingNote"): bits.append(i["listingNote"])
            when = {"upcoming": i.get("openDate"), "open": i.get("closeDate"), "closed": i.get("listingDate"), "listed": i.get("listingDate")}.get(i.get("status"), "") or i.get("openDate") or ""
            items.append({"category": "ipo", "company": i["name"], "cap": i.get("platform", ""), "ipo": i,
                          "fact": " · ".join(bits) or f"{i.get('status','').capitalize()} IPO", "when": when, "sortDate": when, "source": i.get("source", "news"),
                          "materiality": 2 if i.get("platform") == "Mainboard" else 1})
    if args.mode in ("full", "results", "custom"):
        cal = fetch_results_calendar()
        if not cal:
            try:
                prev = sorted(DATA.glob("*-full*.json"))[-3:]
                for f in reversed(prev):
                    d_prev = json.loads(f.read_text())
                    cal = [{"name": i["company"], "date": i.get("sortDate", ""), "purpose": "Results"} for i in d_prev.get("items", []) if i.get("category") == "calendar" and i.get("sortDate", "") >= t0.strftime("%Y-%m-%d")]
                    if cal:
                        stat("Results calendar", True, len(cal), f"carried over from {f.stem}"); break
            except Exception as ex:
                log(f"calendar carry-over failed: {ex}")
        seg = segment_map() if cal else {}
        by_name = {}
        for sym, nm in NAMES.items():
            by_name[re.sub(r"[^a-z0-9]", "", nm.lower())[:14]] = sym
        for c in cal:
            key = re.sub(r"[^a-z0-9]", "", c["name"].lower())[:14]
            sym = by_name.get(key)
            cap = seg.get(sym, "") if sym else ""
            when, sort_date = c["date"], c["date"]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%Y%m%d"):
                try:
                    d = dt.datetime.strptime(c["date"][:10].strip(), fmt); when = d.strftime("%a %d %b"); sort_date = d.strftime("%Y-%m-%d"); break
                except Exception:
                    continue
            c["date"] = sort_date
            items.append({"category": "calendar", "company": c["name"], "ticker": sym or "", "cap": cap,
                          "fact": f"Results board meeting · {when}", "when": when, "sortDate": c["date"],
                          "source": "BSE", "materiality": 3 if cap in ("Large", "Mid") else (2 if cap == "Small" else 1)})
    items = dedupe(items)
    items.sort(key=lambda i: (-i.get("materiality", 1), i["category"]))

    label_mode = {"full": "Full digest", "movers": "Movers", "orders": "Orders", "results": "Results", "policy": "Policy", "custom": "Custom: " + args.query[:40]}[args.mode]
    run_id = f"{t0.strftime('%Y-%m-%d')}-{t0.strftime('%H%M')}-{args.mode}"

    market = {}
    if glob:
        market = {k: v for k, v in glob.items() if not k.startswith("sec_")}
        market["sectors"] = {k[4:]: v for k, v in glob.items() if k.startswith("sec_")}
        if BREADTH.get("scanned"):
            market["breadth"] = dict(BREADTH)
        if flows: market["flows"] = flows
        if dma: market["dma"] = dma
        market["high52"] = len(highs); market["low52"] = len(lows)
        market["note"] = (f"Quotes as of {glob['asOf']}. " if glob.get("asOf") else "") + "Index, sector and commodity figures from Yahoo Finance; breadth is computed across all liquid NSE stocks."
    window = f"news since {since.astimezone(IST).strftime('%a %d %b %H:%M')} IST"
    doc = {"id": run_id, "label": f"{t0.strftime('%a %d %b')} {t0.strftime('%H:%M')} IST · {label_mode}", "window": window, "date": t0.strftime("%Y-%m-%d"),
           "generatedAt": iso_utc(), "mode": args.mode, "query": args.query, "threshold": args.threshold,
           "market": market, "items": items[:200],
           "sources": sorted({r.get("link", "") for r in raw if r.get("link")})[:80],
           "classifier": "llm" if any_llm else "rules", "stats": STATS, "rawCount": len(raw), "prices": PRICES}
    if args.mode in ("full", "custom", "movers") and doc["items"]:
        doc["brief"] = groq_brief(doc, key)
    DATA.mkdir(exist_ok=True)
    (DATA / f"{run_id}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    if NAMES:
        (DATA / "scrips.json").write_text(json.dumps({k: v[:40] for k, v in sorted(NAMES.items())}, ensure_ascii=False))

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
