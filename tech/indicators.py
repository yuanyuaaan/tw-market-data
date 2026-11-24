"""Technical indicators on a list of closing prices (oldest first). Pure Python, no numpy."""


def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema_series(xs: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi_wilder(xs: list[float], n: int = 14) -> float | None:
    if len(xs) <= n:
        return None
    gains = [max(xs[i] - xs[i - 1], 0) for i in range(1, len(xs))]
    losses = [max(xs[i - 1] - xs[i], 0) for i in range(1, len(xs))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd_hist(xs: list[float]) -> float | None:
    if len(xs) < 35:
        return None
    fast, slow = ema_series(xs, 12), ema_series(xs, 26)
    dif = [a - b for a, b in zip(fast, slow)]
    sig = ema_series(dif[25:], 9)
    return dif[-1] - sig[-1]


def trend_label(close, ma5, ma20, ma60, hist) -> str:
    if None in (close, ma5, ma20, ma60, hist):
        return "資料不足"
    score = sum([close > ma5, close > ma20, close > ma60, hist > 0, ma20 > ma60])
    return {5: "多頭", 4: "偏多", 3: "盤整", 2: "偏空"}.get(score, "轉弱")


def snapshot(closes: list[float], vols: list[float] | None = None) -> dict:
    """Indicators as of the last close."""
    c = closes[-1]
    ma5, ma10, ma20, ma60 = (sma(closes, n) for n in (5, 10, 20, 60))
    hist = macd_hist(closes)
    rsi = rsi_wilder(closes)
    w60 = closes[-60:]
    out = {
        "close": c,
        "chg": (c / closes[-2] - 1) * 100 if len(closes) > 1 else None,
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "rsi": rsi, "macd_hist": hist,
        "dev20": (c / ma20 - 1) * 100 if ma20 else None,
        "high20": max(closes[-20:]) if len(closes) >= 20 else None,
        "low20": min(closes[-20:]) if len(closes) >= 20 else None,
        "low60": (c <= min(w60)) if len(closes) >= 60 else None,
        "high60": (c >= max(w60)) if len(closes) >= 60 else None,
        "trend": trend_label(c, ma5, ma20, ma60, hist),
    }
    if vols and len(vols) >= 6 and sum(vols[-6:-1]) > 0:
        out["vol_ratio"] = vols[-1] / (sum(vols[-6:-1]) / 5)
    return out


def adjust_splits(bars: list[dict], limit: float = 0.105) -> tuple[list[dict], list[str]]:
    """Back-adjust price gaps beyond Taiwan's 10% daily limit (splits, capital reductions,
    large ex-rights). A move past the limit cannot be real trading, so earlier prices are
    scaled by the gap ratio. Returns new bars and a note per adjustment."""
    bars = [dict(b) for b in bars]
    notes = []
    for i in range(len(bars) - 1, 0, -1):
        prev, cur = bars[i - 1]["close"], bars[i]["close"]
        if prev and abs(cur / prev - 1) > limit:
            ratio = cur / prev
            # Use the open as the first post-event trade when it exists; the gap is at the open.
            op = bars[i].get("open")
            if op and abs(cur / op - 1) <= limit and abs(op / prev - 1) > limit:
                ratio = op / prev
            for b in bars[:i]:
                for k in ("open", "high", "low", "close"):
                    if b.get(k) is not None:
                        b[k] = b[k] * ratio
            notes.append(f"{bars[i]['date']} 價格斷層 {(cur / prev - 1) * 100:+.1f}%，之前價格乘以 {ratio:.4f} 還原")
    return bars, notes
