"""Daily bars -> indicators and market breadth for the watchlist, written as JSON for the dashboard.

Usage: python -m tech.run_tech tw|us
Writes data/tw_latest.json or data/us_latest.json. Exits non-zero only if nothing usable came back.
Environment: optional FINMIND_TOKEN.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from tech.indicators import adjust_splits, rsi_wilder, sma, snapshot

FINMIND = "https://api.finmindtrade.com/api/v4/data"
UA = {"User-Agent": "Mozilla/5.0 (sim-portfolio-bot)"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DAYS = 520          # calendar days fetched: ~250 breadth days plus 60 bars of warm-up
BREADTH_KEEP = 250


def finmind(dataset: str, data_id: str | None, start: str, end: str) -> list[dict]:
    params = {"dataset": dataset, "start_date": start, "end_date": end}
    if data_id:
        params["data_id"] = data_id
    if os.environ.get("FINMIND_TOKEN"):
        params["token"] = os.environ["FINMIND_TOKEN"]
    last = None
    for attempt in range(4):
        try:
            r = requests.get(FINMIND, params=params, headers=UA, timeout=60)
            if r.status_code in (402, 429):
                raise RuntimeError(f"rate limited ({r.status_code})")
            r.raise_for_status()
            j = r.json()
            if j.get("status") not in (200, None):
                raise RuntimeError(j.get("msg") or str(j.get("status")))
            return j.get("data", [])
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"FinMind {dataset} {data_id}: {last}")


def tw_bars(code: str, start: str, end: str) -> list[dict]:
    rows = finmind("TaiwanStockPrice", code, start, end)
    return [{"date": x["date"], "open": x["open"] or None, "high": x["max"], "low": x["min"],
             "close": x["close"], "volume": x.get("Trading_Volume") or 0}
            for x in rows if x.get("close")]


def us_bars(code: str, start: str, end: str) -> tuple[list[dict], str]:
    try:
        rows = finmind("USStockPrice", code, start, end)
        bars = [{"date": x["date"], "open": x.get("Open"), "high": x.get("High"), "low": x.get("Low"),
                 "close": x["Close"], "volume": x.get("Volume") or 0} for x in rows if x.get("Close")]
        if bars:
            return bars, "FinMind"
    except Exception as e:  # noqa: BLE001
        print(f"[warn] FinMind US {code}: {e}")
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{code}",
                     params={"range": "2y", "interval": "1d"}, headers=UA, timeout=60)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    ny = ZoneInfo("America/New_York")
    bars = []
    for i, ts in enumerate(res["timestamp"]):
        if q["close"][i] is None:
            continue
        bars.append({"date": datetime.fromtimestamp(ts, ny).strftime("%Y-%m-%d"), "open": q["open"][i],
                     "high": q["high"][i], "low": q["low"][i], "close": q["close"][i],
                     "volume": q["volume"][i] or 0})
    return [b for b in bars if b["date"] >= start], "Yahoo"


def r2(v):
    return None if v is None else round(v, 2)


def breadth(series: dict[str, list[dict]], dates: list[str]) -> list[dict]:
    """For each date, counts over stocks with at least 60 bars up to and including that date."""
    idx = {code: {b["date"]: i for i, b in enumerate(bars)} for code, bars in series.items()}
    closes = {code: [b["close"] for b in bars] for code, bars in series.items()}
    out = []
    for d in dates:
        row = {"date": d, "n": 0, "above_ma5": 0, "above_ma20": 0, "above_ma60": 0, "low60": 0, "rsi_lt30": 0}
        for code in series:
            i = idx[code].get(d)
            if i is None or i < 59:
                continue
            cs = closes[code][: i + 1]
            c = cs[-1]
            row["n"] += 1
            row["above_ma5"] += c > sma(cs, 5)
            row["above_ma20"] += c > sma(cs, 20)
            row["above_ma60"] += c > sma(cs, 60)
            row["low60"] += c <= min(cs[-60:])
            rsi = rsi_wilder(cs)
            row["rsi_lt30"] += rsi is not None and rsi < 30
        if row["n"]:
            out.append(row)
    return out


def stock_row(meta: dict, bars: list[dict], asof: str, extra_keys: tuple) -> dict:
    row = {k: meta[k] for k in extra_keys if k in meta}
    if not bars:
        return {**row, "close": None, "chg": None, "ma5": None, "ma20": None, "ma60": None, "rsi": None,
                "trend": "無資料", "low60": None, "stale": True}
    s = snapshot([b["close"] for b in bars], [b.get("volume") or 0 for b in bars])
    out = {**row, **{k: (r2(v) if isinstance(v, float) else v) for k, v in s.items()}}
    out["last_date"] = bars[-1]["date"]
    out["stale"] = bars[-1]["date"] != asof
    out["bars"] = len(bars)
    return out


def run_tw(cfg: dict) -> dict:
    tz = ZoneInfo("Asia/Taipei")
    today = datetime.now(tz).date()
    start, end = (today - timedelta(days=HISTORY_DAYS)).isoformat(), (today + timedelta(days=1)).isoformat()
    series, notes, missing = {}, [], []
    for m in cfg["tw"]:
        code = m["code"]
        try:
            if m.get("fetch"):
                new = tw_bars(m["fetch"], start, end)
                first = new[0]["date"] if new else end
                old = [b for b in tw_bars(m["merge_from"], start, end) if b["date"] < first]
                bars = old + new
                notes.append(f"{code}：{first} 前用 {m['merge_from']}、之後用 {m['fetch']} 的日K 接起來")
            else:
                bars = tw_bars(code, start, end)
            if m.get("no_adjust"):
                notes.append(f"{code} {m['name']}：不做價格斷層還原（{m['no_adjust']}）")
            else:
                bars, adj = adjust_splits(bars)
                notes += [f"{code} {m['name']}：{a}" for a in adj]
            series[code] = bars
        except Exception as e:  # noqa: BLE001
            missing.append(f"{code} {m['name']}：{e}")
            series[code] = []
        time.sleep(0.4)
    ref = series.get("2330") or max(series.values(), key=len)
    asof = ref[-1]["date"] if ref else None
    if not asof:
        raise RuntimeError("沒有抓到任何台股日K")
    stocks = [stock_row(m, series[m["code"]], asof, ("code", "name", "sector", "tier")) for m in cfg["tw"]]
    for s in stocks:
        if s.get("stale") and s.get("last_date"):
            missing.append(f"{s['code']} {s['name']}：最新一筆是 {s['last_date']}，不是 {asof}")
    eq = {m["code"]: series[m["code"]] for m in cfg["tw"] if m["sector"] != "ETF/避險" and series[m["code"]]}
    dates = [b["date"] for b in ref][-BREADTH_KEEP:]
    br = breadth(eq, dates)
    # Index level and foreign net buy/sell (億元) for the breadth rows.
    try:
        tx = {x["date"]: x["close"] for x in finmind("TaiwanStockPrice", "TAIEX", dates[0], end)}
    except Exception as e:  # noqa: BLE001
        tx = {}
        missing.append(f"TAIEX：{e}")
    try:
        inst = finmind("TaiwanStockTotalInstitutionalInvestors", None, dates[0], end)
        fn = {}
        for x in inst:
            if x.get("name") == "Foreign_Investor":  # 外資及陸資（不含外資自營商），與新聞口徑一致
                fn[x["date"]] = fn.get(x["date"], 0) + (x["buy"] - x["sell"]) / 1e8
    except Exception as e:  # noqa: BLE001
        fn = {}
        missing.append(f"三大法人：{e}")
    prev = None
    for row in br:
        t = tx.get(row["date"])
        row["taiex"] = r2(t)
        row["taiex_chg"] = r2((t / prev - 1) * 100) if t and prev else None
        row["foreign_net"] = r2(fn.get(row["date"]))
        prev = t or prev
    return {"market": "TW", "asof": asof, "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
            "source": "FinMind TaiwanStockPrice 日K（GitHub 機器人計算，價格斷層已還原）",
            "stocks": stocks, "breadth": br, "notes": notes, "missing": missing}


def run_us(cfg: dict) -> dict:
    ny = ZoneInfo("America/New_York")
    today = datetime.now(ny).date()
    start, end = (today - timedelta(days=HISTORY_DAYS)).isoformat(), (today + timedelta(days=1)).isoformat()
    series, missing, used = {}, [], {}
    for m in cfg["us"]:
        try:
            series[m["code"]], used[m["code"]] = us_bars(m["code"], start, end)
        except Exception as e:  # noqa: BLE001
            series[m["code"]] = []
            missing.append(f"{m['code']}：{e}")
        time.sleep(0.4)
    ref = series.get("SPY") or max(series.values(), key=len)
    asof = ref[-1]["date"]
    # A bar dated today before 16:00 New York time is still moving; drop it.
    now = datetime.now(ny)
    if asof == now.date().isoformat() and now.hour < 16:
        for k in series:
            series[k] = [b for b in series[k] if b["date"] != asof]
        ref = series.get("SPY") or ref
        asof = ref[-1]["date"]
    stocks = [stock_row(m, series[m["code"]], asof, ("code", "name", "group", "tier")) for m in cfg["us"]]
    for s in stocks:
        if s.get("stale") and s.get("last_date"):
            missing.append(f"{s['code']}：最新一筆是 {s['last_date']}，不是 {asof}")
    eq = {m["code"]: series[m["code"]] for m in cfg["us"] if m["tier"] != "避險" and series[m["code"]]}
    br = breadth(eq, [b["date"] for b in ref][-BREADTH_KEEP:])
    src = sorted(set(used.values())) or ["無"]
    return {"market": "US", "asof": asof,
            "generated_at": datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds"),
            "source": f"{'/'.join(src)} 日K（GitHub 機器人計算）", "stocks": stocks, "breadth": br,
            "notes": [], "missing": missing}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "tw"
    cfg = json.load(open(os.path.join(ROOT, "tech", "watchlist.json"), encoding="utf-8"))
    result = run_tw(cfg) if which == "tw" else run_us(cfg)
    path = os.path.join(ROOT, "data", f"{which}_latest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    ok = sum(1 for s in result["stocks"] if s.get("close") is not None and not s.get("stale"))
    print(f"{which}: asof {result['asof']}, {ok}/{len(result['stocks'])} stocks current, "
          f"{len(result['breadth'])} breadth rows, missing {len(result['missing'])}")
    for m in result["missing"]:
        print("  missing:", m)


if __name__ == "__main__":
    main()
