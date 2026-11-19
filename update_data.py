#!/usr/bin/env python3
"""
個股觀察室 每日資料更新腳本
--------------------------------
從證交所 (TWSE)、櫃買中心 (TPEx)、集保 (TDCC)、公開資訊觀測站 (MOPS)
抓取 12 檔觀察股的資料，產生網站讀取的 week-data.js（window.OFFICIAL_SNAPSHOT）
以及可在網站內匯入的 CSV。

只用 Python 標準函式庫，不需安裝套件。

用法：
    python3 update_data.py                # 增量更新（第一次會自動回補）
    python3 update_data.py --price-days 150 --flow-days 30
輸出：
    week-data.js        網站讀取的資料（同時輸出 week-data.json、week-audit.json 供下載）
    stock-data.csv      可用網站的 CSV 匯入功能載入
    data/history.json   累積的歷史資料（集保週資料會一週一週累積，請保留這個檔）
"""
import argparse, csv, datetime as dt, gzip, io, json, os, re, sys, time, urllib.parse, urllib.request
from html.parser import HTMLParser

TZ = dt.timezone(dt.timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
HIST_PATH = os.path.join(HERE, "data", "history.json")

# 與 engine.js 的 WATCH 清單一致；market: TWSE=上市（含創新板）, TPEX=上櫃
WATCH = [
    ("2408", "TWSE"), ("2344", "TWSE"), ("3006", "TWSE"), ("6949", "TWSE"),
    ("1608", "TWSE"), ("6147", "TPEX"), ("1802", "TWSE"), ("2492", "TWSE"),
    ("2481", "TWSE"), ("1815", "TPEX"), ("3441", "TPEX"), ("3311", "TWSE"),
]
CODES = {c for c, _ in WATCH}
MARKET = dict(WATCH)

UA = "Mozilla/5.0 (compatible; stockdesk-updater/1.0)"
SLEEP = 3.0  # 證交所對頻繁請求會暫時封鎖 IP，保持間隔
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


# ---------------------------------------------------------------- HTTP
def http(url, data=None, encoding="utf-8", retries=3):
    body = urllib.parse.urlencode(data).encode() if data else None
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "User-Agent": UA, "Accept-Encoding": "gzip",
                **({"Content-Type": "application/x-www-form-urlencoded"} if body else {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            time.sleep(SLEEP)
            return raw.decode(encoding, errors="replace")
        except Exception as e:  # 逾時、連線重置、429 等
            last = e
            wait = SLEEP * (2 ** attempt) * 2
            log(f"  ! {url[:90]} 失敗（{e}），{wait:.0f} 秒後重試")
            time.sleep(wait)
    raise RuntimeError(f"多次重試仍失敗：{url}（{last}）")


def get_json(url, data=None):
    txt = http(url, data)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        raise RuntimeError(f"回應不是 JSON：{url}")


def num(s):
    """'1,234' / '-5,678' / '--' / '' -> float or None"""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "-", "X", "除權息", "除息", "除權"):
        return None
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    y, m, d = (int(x) for x in re.split(r"[/\-]", s.strip()))
    if y < 1911:  # 民國年；櫃買指數等部分來源已改用西元年
        y += 1911
    return f"{y:04d}-{m:02d}-{d:02d}"


def ymd(d):
    return d.strftime("%Y%m%d")


def slash(d):
    return d.strftime("%Y/%m/%d")


def months_between(start, end):
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        cur = dt.date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)


# ---------------------------------------------------------------- 價量 + 指數（以月為單位抓，便宜）
def fetch_prices(start, end, rows):
    for m in months_between(start, end):
        for code, mk in WATCH:
            try:
                if mk == "TWSE":
                    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ymd(m)}&stockNo={code}&response=json"
                    o = get_json(url)
                    if o.get("stat") != "OK":
                        continue
                    for r in o.get("data", []):
                        d = roc_to_iso(r[0])
                        close, vol = num(r[6]), num(r[1])
                        if close and vol is not None:
                            rows.setdefault((code, d), {}).update(close=close, volume=int(vol), source=url)
                            rows[(code, d)].pop("precision_fields", None)
                            rows[(code, d)].pop("precision_note", None)
                else:
                    url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
                    o = get_json(url, {"code": code, "date": slash(m), "response": "json"})
                    t = (o.get("tables") or [{}])[0]
                    for r in t.get("data", []):
                        d = roc_to_iso(r[0])
                        close, lots = num(r[6]), num(r[1])
                        if close and lots is not None:
                            rec = rows.setdefault((code, d), {})
                            rec.update(close=close, volume=int(lots * 1000),
                                       source=f"https://www.tpex.org.tw/zh-tw/mainboard/trading/info/stock-pricing.html?code={code}",
                                       precision_fields="volume",
                                       precision_note="櫃買個股日成交資訊以張列示，換算股數為約數。")
            except Exception as e:
                log(f"價量 {code} {m:%Y-%m} 失敗：{e}")
        # 指數
        try:
            url = f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={ymd(m)}&response=json"
            o = get_json(url)
            for r in o.get("data", []):
                bench_set(rows, roc_to_iso(r[0]), "TWSE", num(r[4]), url)
        except Exception as e:
            log(f"加權指數 {m:%Y-%m} 失敗：{e}")
        try:
            url = "https://www.tpex.org.tw/www/zh-tw/indexInfo/inx"
            o = get_json(url, {"date": slash(m), "response": "json"})
            for r in (o.get("tables") or [{}])[0].get("data", []):
                bench_set(rows, roc_to_iso(r[0]), "TPEX", num(r[4]),
                          "https://www.tpex.org.tw/zh-tw/indexInfo/inx.html")
        except Exception as e:
            log(f"櫃買指數 {m:%Y-%m} 失敗：{e}")


BENCH = {}  # (market, date) -> (value, url)


def bench_set(rows, d, mk, v, url):
    if v:
        BENCH[(mk, d)] = (v, url)


def apply_bench(rows):
    for (code, d), rec in rows.items():
        b = BENCH.get((MARKET[code], d))
        if b:
            rec["benchmark"], rec["benchmark_source"] = b


# ---------------------------------------------------------------- 法人、融資、借券（以日為單位）
def fetch_daily(day, rows):
    d = day.isoformat()
    have = [c for c in CODES if (c, d) in rows]
    if not have:
        return  # 非交易日
    # --- 上市
    try:
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd(day)}&selectType=ALLBUT0999&response=json"
        o = get_json(url)
        for r in o.get("data", []):
            c = r[0].strip()
            if c in CODES and MARKET[c] == "TWSE" and (c, d) in rows:
                rows[(c, d)].update(foreign=int(num(r[4])), trust=int(num(r[10])),
                                    dealer=int(num(r[11])), flow_source=url)
    except Exception as e:
        log(f"上市法人 {d} 失敗：{e}")
    try:
        url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd(day)}&selectType=ALL&response=json"
        o = get_json(url)
        for t in o.get("tables", []):
            if "代號" not in (t.get("fields") or []):
                continue
            for r in t.get("data", []):
                c = r[0].strip()
                if c in CODES and MARKET[c] == "TWSE" and (c, d) in rows and num(r[6]) is not None:
                    rows[(c, d)].update(margin=int(num(r[6]) * 1000), margin_source=url)
    except Exception as e:
        log(f"上市融資 {d} 失敗：{e}")
    try:
        url = f"https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U?date={ymd(day)}&response=json"
        o = get_json(url)
        for r in o.get("data", []):
            c = r[0].strip()
            if c in CODES and MARKET[c] == "TWSE" and (c, d) in rows and num(r[12]) is not None:
                rows[(c, d)].update(short=int(num(r[12])), short_source=url)
    except Exception as e:
        log(f"上市借券 {d} 失敗：{e}")
    # --- 上櫃
    form = {"date": slash(day), "response": "json"}
    try:
        url = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
        o = get_json(url, {"type": "Daily", "sect": "EW", **form})
        for r in (o.get("tables") or [{}])[0].get("data", []):
            c = r[0].strip()
            if c in CODES and MARKET[c] == "TPEX" and (c, d) in rows:
                rows[(c, d)].update(foreign=int(num(r[4])), trust=int(num(r[13])),
                                    dealer=int(num(r[22])),
                                    flow_source="https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/detail/day.html")
    except Exception as e:
        log(f"上櫃法人 {d} 失敗：{e}")
    try:
        url = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
        o = get_json(url, form)
        for r in (o.get("tables") or [{}])[0].get("data", []):
            c = r[0].strip()
            if c in CODES and MARKET[c] == "TPEX" and (c, d) in rows and num(r[6]) is not None:
                rows[(c, d)].update(margin=int(num(r[6]) * 1000),
                                    margin_source="https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html")
    except Exception as e:
        log(f"上櫃融資 {d} 失敗：{e}")
    try:
        url = "https://www.tpex.org.tw/www/zh-tw/margin/sbl"
        o = get_json(url, form)
        for r in (o.get("tables") or [{}])[0].get("data", []):
            c = r[0].strip()
            if c in CODES and MARKET[c] == "TPEX" and (c, d) in rows and num(r[12]) is not None:
                rows[(c, d)].update(short=int(num(r[12])),
                                    short_source="https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/sbl.html")
    except Exception as e:
        log(f"上櫃借券 {d} 失敗：{e}")


# ---------------------------------------------------------------- 集保 400 張以上持股比例（每週，僅能取得最新一週，靠累積）
def fetch_tdcc(hist):
    url = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
    try:
        txt = http(url, encoding="utf-8-sig")
    except Exception as e:
        log(f"集保資料失敗：{e}")
        return
    pct, date = {}, None
    for r in csv.reader(io.StringIO(txt)):
        if len(r) < 6 or not r[0].strip().isdigit():
            continue
        c, level = r[1].strip(), int(num(r[2]) or 0)
        if c in CODES and 12 <= level <= 15:  # 12=400,001~600,000 股 … 15=1,000,001 股以上
            date = f"{r[0][:4]}-{r[0][4:6]}-{r[0][6:8]}"
            pct[c] = pct.get(c, 0.0) + (num(r[5]) or 0.0)
    if date:
        hist.setdefault("holders", {})[date] = {c: round(v, 2) for c, v in pct.items()}
        log(f"集保 {date}：取得 {len(pct)} 檔")


def apply_holders(rows, hist):
    weeks = sorted(hist.get("holders", {}).items())
    for (code, d), rec in rows.items():
        for k in ("holders_pct", "holders_date", "holders_source"):
            rec.pop(k, None)
        for wd, vals in reversed(weeks):
            if wd <= d and code in vals:
                rec.update(holders_pct=vals[code], holders_date=wd,
                           holders_source="https://www.tdcc.com.tw/portal/zh/smWeb/qryStock")
                break


# ---------------------------------------------------------------- 月營收（MOPS 彙總表，近 3 月合計年增率）
class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.in_td = [], None, "", False

    def handle_starttag(self, tag, a):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.in_td, self.cell = True, ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None:
            self.row.append(self.cell.strip())
            self.in_td = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_td:
            self.cell += data


def fetch_revenue(hist, today):
    rev = hist.setdefault("revenue", {})  # "YYYY-MM" -> {code: [當月, 去年當月]}
    # 最近 7 個「已公布」的月份（每月 10 日前公布上月）
    last = dt.date(today.year, today.month, 1) - dt.timedelta(days=1)
    if today.day < 11:
        last = dt.date(last.year, last.month, 1) - dt.timedelta(days=1)
    months = []
    m = dt.date(last.year, last.month, 1)
    for _ in range(7):
        months.append(m)
        m = dt.date(m.year - (m.month == 1), (m.month - 2) % 12 + 1, 1)
    for m in months:
        key = f"{m:%Y-%m}"
        if key in rev and len(rev[key]) >= len(CODES):
            continue
        got = rev.setdefault(key, {})
        for kind in ("sii", "otc"):
            url = f"https://mopsov.twse.com.tw/nas/t21/{kind}/t21sc03_{m.year - 1911}_{m.month}_0.html"
            try:
                p = TableRows()
                p.feed(http(url, encoding="big5"))
                for r in p.rows:
                    # 欄位：公司代號, 公司名稱, 當月營收, 上月營收, 去年當月營收, ...
                    if len(r) >= 5 and r[0] in CODES:
                        cur, ly = num(r[2]), num(r[4])
                        if cur is not None and ly is not None:
                            got[r[0]] = [cur, ly]
            except Exception as e:
                log(f"營收 {key} {kind} 失敗：{e}")
        log(f"營收 {key}：取得 {len(got)} 檔")


def apply_revenue(rows, hist):
    rev = hist.get("revenue", {})
    keys = sorted(rev)
    yoy = {}  # (code, month) -> 近3月合計年增率
    for i in range(2, len(keys)):
        trio = keys[i - 2:i + 1]
        y0, m0 = map(int, trio[0].split("-"))
        y2, m2 = map(int, trio[2].split("-"))
        if (y2 * 12 + m2) - (y0 * 12 + m0) != 2:
            continue
        for code in CODES:
            vals = [rev[k].get(code) for k in trio]
            if all(vals):
                cur, ly = sum(v[0] for v in vals), sum(v[1] for v in vals)
                if ly > 0:
                    yoy[(code, trio[2])] = round((cur / ly - 1) * 100, 2)
    for (code, d), rec in rows.items():
        for k in ("revenue3_yoy", "revenue_month", "revenue_source"):
            rec.pop(k, None)
        y, mo, day = map(int, d.split("-"))
        # 交易日 d 當下可得的最新月份：d 在 11 日以後可用上個月，否則上上個月
        back = 1 if day >= 11 else 2
        idx = y * 12 + (mo - 1) - back
        month = f"{idx // 12:04d}-{idx % 12 + 1:02d}"
        if (code, month) in yoy:
            rec.update(revenue3_yoy=yoy[(code, month)], revenue_month=month,
                       revenue_source="https://mops.twse.com.tw/mops/#/web/t05st10_ifrs")


# ---------------------------------------------------------------- 輸出
FIELDS = ["code", "date", "close", "volume", "source", "foreign", "trust", "dealer", "flow_source",
          "margin", "margin_source", "short", "short_source", "benchmark", "benchmark_source",
          "holders_pct", "holders_date", "holders_source", "revenue3_yoy", "revenue_month",
          "revenue_source", "precision_fields", "precision_note"]


LOT_FIELDS = ("volume", "foreign", "trust", "dealer", "margin", "short")


def write_outputs(rows, today, keep_days):
    cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
    recs = []
    for (code, d), rec in sorted(rows.items(), key=lambda x: (x[0][1], x[0][0])):
        if d < cutoff or d > today.isoformat() or not rec.get("close") or "source" not in rec:
            continue
        out = {"code": code, "date": d, **{k: rec[k] for k in FIELDS[2:] if k in rec}}
        # 網站一律以「張」為單位：history.json 內部存股數，輸出時換算成整數張
        for k in LOT_FIELDS:
            if k in out:
                out[k] = round(out[k] / 1000)
        # 網站會把新舊資料合併；明確標示「none」才能蓋掉舊快照留在手機裡的約數標記
        out["precision_fields"], out["precision_note"] = "none", "數量單位為張（官方股數除以 1,000 四捨五入）。"
        recs.append(out)
    dates = sorted({r["date"] for r in recs})
    cov = {}
    for d in dates:
        day = [r for r in recs if r["date"] == d]
        cov[d] = {k: sum(1 for r in day if k in r) for k in
                  ["close", "volume", "foreign", "trust", "dealer", "margin", "short", "benchmark"]}
        cov[d]["approximate"] = sum(1 for r in day if r.get("precision_fields", "none") != "none")
    latest_full = next((d for d in reversed(dates)
                        if cov[d]["close"] == len(CODES) and cov[d]["margin"] == len(CODES)
                        and cov[d]["foreign"] == len(CODES)), dates[-1] if dates else today.isoformat())
    now = dt.datetime.now(TZ)
    snap = {
        "version": 1, "demo": False,
        "snapshot_id": f"auto-{now:%Y%m%d-%H%M}",
        "retrieved_at": now.isoformat(),
        "week_start": dates[0] if dates else None,
        "week_end": dates[-1] if dates else None,
        "preferred_date": latest_full,
        "coverage": cov,
        "notes": ["由 update_data.py 自動抓取 TWSE/TPEx/TDCC/MOPS 官方資料產生。",
                  "成交量、法人買賣超、融資餘額、借券賣出餘額一律以「張」為單位。",
                  "集保 400 張以上持股比例每週累積，需連續 3 週才會亮燈。",
                  "重大事件需人工查核，未自動填入。"] + [f"本次警告：{m}" for m in LOG if "失敗" in m][:20],
        "records": recs,
        "source_mix": "official",
    }
    with open(os.path.join(HERE, "week-data.js"), "w", encoding="utf-8") as f:
        f.write("window.OFFICIAL_SNAPSHOT=" + json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + ";\n")
    with open(os.path.join(HERE, "week-data.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "week-audit.json"), "w", encoding="utf-8") as f:
        json.dump({"retrieved_at": now.isoformat(), "log": LOG}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "stock-data.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(recs)
    log(f"完成：{len(recs)} 筆，{dates[0] if dates else '-'} ～ {dates[-1] if dates else '-'}，preferred_date={latest_full}")
    for d in dates[-3:]:
        log(f"  {d} 覆蓋：{cov[d]}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-days", type=int, default=150, help="價量、指數回補天數（趨勢與相對強弱需 21 筆，營收需跨 3 個月）")
    ap.add_argument("--flow-days", type=int, default=21, help="法人、融資、借券回補天數（需 6 筆）")
    ap.add_argument("--keep-days", type=int, default=150, help="輸出保留天數")
    args = ap.parse_args()

    today = dt.datetime.now(TZ).date()
    os.makedirs(os.path.dirname(HIST_PATH), exist_ok=True)
    hist = json.load(open(HIST_PATH, encoding="utf-8")) if os.path.exists(HIST_PATH) else {}
    rows = {tuple(k.split("|")): v for k, v in hist.get("rows", {}).items()}

    last_dates = sorted({d for _, d in rows})
    price_start = today - dt.timedelta(days=args.price_days)
    if last_dates:  # 增量：只重抓最近 1 個月（月資料便宜，順便修正調帳）
        price_start = max(price_start, dt.date.fromisoformat(last_dates[-1]) - dt.timedelta(days=31))
        gaps = [d for (c, d), r in rows.items() if "benchmark" not in r and d >= price_start.isoformat()]
        if gaps:  # 指數有缺就從最早缺的那天重抓
            price_start = min(price_start, dt.date.fromisoformat(min(gaps)))
    log(f"價量／指數：{price_start} ～ {today}")
    fetch_prices(price_start, today, rows)
    apply_bench(rows)

    flow_start = today - dt.timedelta(days=args.flow_days)
    day = flow_start
    while day <= today:
        d = day.isoformat()
        need = any((c, d) in rows and ("margin" not in rows[(c, d)] or "foreign" not in rows[(c, d)]
                                       or "short" not in rows[(c, d)]) for c in CODES)
        if need or day >= today - dt.timedelta(days=3):  # 近 3 天一律重抓，信用交易會調帳
            log(f"法人／融資／借券：{d}")
            fetch_daily(day, rows)
        day += dt.timedelta(days=1)

    fetch_tdcc(hist)
    apply_holders(rows, hist)
    fetch_revenue(hist, today)
    apply_revenue(rows, hist)

    hist["rows"] = {f"{c}|{d}": v for (c, d), v in rows.items()}
    with open(HIST_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    write_outputs(rows, today, args.keep_days)
    return 0 if not any("失敗" in m for m in LOG) else 2


if __name__ == "__main__":
    sys.exit(main())
