"""
LIGHTER KAGIT TAKIP (FLIP_LIVE=true)
------------------------------------
1 dk BTC SuperTrend gir-cik stratejisini GERCEK PARA KULLANMADAN, Lighter'in gercek emir defteriyle olcer.

- Sinyal: 1 dk kapanmis mumlarda SuperTrend (varsayilan 7,2) yon degistirince ters pozisyon.
- Pozisyon: stop SuperTrend cizgisinde ve her mum cizgiyi TAKIP eder; buyukluk = stop olursa kayip
  bakiyenin FLIP_RISK_PCT'i olacak sekilde (ust sinir bakiye x kaldirac). Bilesik.
- Dolum: varsayim yok. Giris/cikis aninda Lighter emir defterinden, o notional'in GERCEKTEN dolacagi
  ortalama fiyat (defteri yururek) hesaplanir. Botun gercek gecikmesi de dahildir.
- Stop: emir defteri birkac saniyede bir yoklanir; en iyi fiyat stopu gecince defterden market cikis.
- Acil fren: bakiye zirveden FLIP_MAX_DD_PCT duserse islem durur, Telegram'a haber verilir.
- Olcumler: her emirde maliyet (mum kapanisina / stop seviyesine gore) ve yarim spread; periyodik rapor.
  Backtestteki kar/zarar esigi: emir basi ~%0,003.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests


def _env(name, default, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        if cast is bool:
            return v.strip().lower() in ("1", "true", "yes", "evet")
        return cast(v)
    except Exception:
        return default


FLIP_LIVE = _env("FLIP_LIVE", False, bool)
FLIP_SYMBOL = _env("FLIP_SYMBOL", "BTC").upper()
FLIP_ST_PERIOD = _env("FLIP_ST_PERIOD", 7, int)
FLIP_ST_MULT = _env("FLIP_ST_MULT", 2.0, float)
FLIP_BALANCE = _env("FLIP_BALANCE", 500.0, float)
FLIP_LEV = _env("FLIP_LEV", 25.0, float)
FLIP_RISK_PCT = _env("FLIP_RISK_PCT", 1.0, float)
FLIP_SIZING = _env("FLIP_SIZING", "risk").lower()        # risk | full (tum teminat x kaldirac)
FLIP_FEE = _env("FLIP_FEE", 0.0, float)                    # taker komisyon (Lighter standart: 0)
FLIP_MAX_DD_PCT = _env("FLIP_MAX_DD_PCT", 20.0, float)
FLIP_POLL_SEC = _env("FLIP_POLL_SEC", 3.0, float)
FLIP_REPORT_HOURS = _env("FLIP_REPORT_HOURS", 6.0, float)
FLIP_TRADE_MSGS = _env("FLIP_TRADE_MSGS", False, bool)
FLIP_ENTER_ON_START = _env("FLIP_ENTER_ON_START", True, bool)   # baslangicta mevcut yonde hemen gir
FLIP_TP_ROE = _env("FLIP_TP_ROE", 50.0, float)            # teminatta % kar olunca limit emirle kapat (0 = kapali)
LIGHTER_URL = _env("LIGHTER_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
LIGHTER_MARKET = _env("LIGHTER_MARKET", "")                # bos = otomatik bul
STATE_FILE = "flip_state.json"
BREAKEVEN_PCT = 0.00003                                   # backtest: emir basi ~%0,003 (oran: 0.00003) ustu zarar

_lock = threading.RLock()
S = {}                                                    # durum (kalici)


def _new_state():
    return {"eq": FLIP_BALANCE, "peak": FLIP_BALANCE, "start_eq": FLIP_BALANCE, "started": int(time.time() * 1000),
            "pos": 0, "entry": 0.0, "notional": 0.0, "stop": None, "opened": None,
            "halted": False, "last_bar": None, "prev_dir": None,
            "trades": [], "orders": [], "spreads": [], "errors": 0, "last_report": time.time(),
            "params": {"st": [FLIP_ST_PERIOD, FLIP_ST_MULT], "risk": FLIP_RISK_PCT, "lev": FLIP_LEV, "fee": FLIP_FEE}}


# ---------------------------------------------------------------------------- Lighter
class LighterBook:
    def __init__(self, log):
        self.log = log
        self.market_id = None
        self.http = requests.Session()

    def _get(self, path, **params):
        r = self.http.get(f"{LIGHTER_URL}{path}", params=params, timeout=8)
        if r.status_code == 429:
            time.sleep(2)
            r = self.http.get(f"{LIGHTER_URL}{path}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()

    def resolve(self):
        if LIGHTER_MARKET:
            self.market_id = int(LIGHTER_MARKET)
            return self.market_id
        j = self._get("/api/v1/orderBooks")
        books = j.get("order_books") or j.get("orderBooks") or (j if isinstance(j, list) else [])
        for b in books:
            sym = str(b.get("symbol", "")).upper()
            if sym in (FLIP_SYMBOL, f"{FLIP_SYMBOL}-USD", f"{FLIP_SYMBOL}USD", f"{FLIP_SYMBOL}-PERP"):
                mt = str(b.get("market_type", "perp")).lower()
                if "spot" in mt:
                    continue
                self.market_id = int(b.get("market_id"))
                return self.market_id
        raise RuntimeError(f"Lighter'da {FLIP_SYMBOL} piyasasi bulunamadi")

    @staticmethod
    def _levels(side):
        out = []
        for it in side or []:
            if isinstance(it, (list, tuple)) and len(it) >= 2:
                p, q = float(it[0]), float(it[1])
            elif isinstance(it, dict):
                p = float(it.get("price"))
                q = None
                for k in ("remaining_base_amount", "size", "amount", "base_amount", "initial_base_amount", "quantity"):
                    if it.get(k) not in (None, ""):
                        q = float(it[k])
                        break
                if q is None:
                    continue
            else:
                continue
            if p > 0 and q > 0:
                out.append((p, q))
        return out

    def candles(self, n=300):
        """Lighter 1 dk mumlari (kapanmis). Format/zaman birimi farkli olabilir: birkac istek bicimi denenir."""
        if self.market_id is None:
            self.resolve()
        now_ms = int(time.time() * 1000)
        now_s = now_ms // 1000
        tries = [
            dict(resolution="1m", start_timestamp=now_ms - (n + 5) * 60_000, end_timestamp=now_ms, count_back=n),
            dict(resolution="1m", start_timestamp=now_s - (n + 5) * 60, end_timestamp=now_s, count_back=n),
            dict(resolution="1m", start_timestamp=now_ms - (n + 5) * 60_000, end_timestamp=now_ms, count_back=n,
                 set_timestamp_to_end="true"),
            dict(resolution="1", start_timestamp=now_s - (n + 5) * 60, end_timestamp=now_s, count_back=n),
        ]
        if getattr(self, "_ok_try", None) is not None:
            tries = [tries[self._ok_try]] + tries
        rows, last = None, ""
        for k, p in enumerate(tries):
            try:
                r = self.http.get(f"{LIGHTER_URL}/api/v1/candlesticks", params=dict(market_id=self.market_id, **p), timeout=8)
                last = f"HTTP {r.status_code}: {r.text[:180]}"
                if r.status_code >= 300:
                    continue
                j = r.json()
            except Exception as ex:
                last = str(ex)[:180]
                continue
            rows = (j.get("candlesticks") or j.get("candles") or j.get("c") or j.get("data")) if isinstance(j, dict) else j
            if rows:
                if getattr(self, "_ok_try", None) is None:
                    self._ok_try = k
                break
        if not rows:
            raise RuntimeError(f"Lighter mum verisi bos · son cevap: {last}")
        out = []
        for c in rows:
            if isinstance(c, dict):
                ts = c.get("timestamp", c.get("t", c.get("start_timestamp", c.get("time"))))
                o, h, l, cl = (c.get(k2, c.get(k2[0])) for k2 in ("open", "high", "low", "close"))
            else:
                ts, o, h, l, cl = c[:5]
            ts = int(float(ts))
            ts = ts * 1000 if ts < 10**12 else ts
            out.append((ts, float(o), float(h), float(l), float(cl)))
        df = pd.DataFrame(out, columns=["ts", "open", "high", "low", "close"]).drop_duplicates("ts")
        df = df.sort_values("ts").reset_index(drop=True)
        df = df[df["ts"] + 60_000 <= now_ms].reset_index(drop=True)
        if len(df) < 50 or (df["high"] < df["low"]).any():
            raise RuntimeError(f"Lighter mum verisi gecersiz ({len(df)} mum)")
        return df

    def book(self):
        if self.market_id is None:
            self.resolve()
        j = self._get("/api/v1/orderBookOrders", market_id=self.market_id, limit=100)
        bids = sorted(self._levels(j.get("bids")), key=lambda x: -x[0])
        asks = sorted(self._levels(j.get("asks")), key=lambda x: x[0])
        if not bids or not asks:
            raise RuntimeError("Lighter emir defteri bos")
        return bids, asks


def walk(levels, notional_usd):
    """Defteri yuruyerek notional_usd'lik market emrinin ortalama dolum fiyati. (fiyat, doldu_mu)"""
    left, cost, qty = notional_usd, 0.0, 0.0
    for p, q in levels:
        take = min(q, left / p)
        cost += take * p
        qty += take
        left -= take * p
        if left <= 1e-9:
            return cost / qty, True
    return (cost / qty if qty > 0 else levels[0][0]), False


# ---------------------------------------------------------------------------- kalicilik
def _save(bot, remote=False):
    with _lock:
        S["trades"] = S["trades"][-3000:]
        S["orders"] = S["orders"][-6000:]
        S["spreads"] = S["spreads"][-3000:]
        payload = json.dumps(S, ensure_ascii=False, default=float)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        pass
    if remote and bot._gist_enabled():
        def push():
            try:
                requests.patch(f"https://api.github.com/gists/{bot.GIST_ID}", headers=bot._gist_headers(),
                               json={"files": {STATE_FILE: {"content": payload}}}, timeout=30)
            except Exception:
                pass
        threading.Thread(target=push, daemon=True).start()


def _load(bot):
    data = None
    if bot._gist_enabled():
        try:
            r = requests.get(f"https://api.github.com/gists/{bot.GIST_ID}", headers=bot._gist_headers(), timeout=30)
            f = r.json().get("files", {}).get(STATE_FILE) if r.status_code < 300 else None
            if f:
                txt = f.get("content") or ""
                if f.get("truncated") and f.get("raw_url"):
                    txt = requests.get(f["raw_url"], headers=bot._gist_headers(), timeout=60).text
                data = json.loads(txt)
        except Exception as e:
            bot.log.warning("Flip durumu Gist'ten okunamadi: %s", e)
    if data is None and os.path.exists(STATE_FILE):
        try:
            data = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            data = None
    S.clear()
    S.update(_new_state())
    if data:
        S.update(data)
        # acik kagit pozisyon KORUNUR: stop kontrolu kaldigi yerden devam eder; yon bilgisi pozisyondan alinir
        S["prev_dir"] = S["pos"] if S.get("pos") else None
        S["last_bar"] = None


# ---------------------------------------------------------------------------- islem mantigi
def _record_order(kind, side, ref, fill, half_spread, filled, delay_ms):
    cost = side * (fill / ref - 1) if ref else 0.0          # + = aleyhe (maliyet)
    S["orders"].append({"t": int(time.time() * 1000), "k": kind, "side": side, "ref": ref, "fill": fill,
                        "cost": cost, "hs": half_spread, "full": filled, "ms": delay_ms})
    return cost


def _close(bot, book, reason, ref_price, t_ref):
    """Acik pozisyonu defterden market ile kapat. ref_price: backtestin kullandigi fiyat (kapanis veya stop)."""
    pos = S["pos"]
    bids, asks = book
    mid = (bids[0][0] + asks[0][0]) / 2
    fill, ok = walk(bids if pos == 1 else asks, S["notional"])
    delay = int((time.time() - t_ref) * 1000) if t_ref else 0
    _record_order("stop" if reason == "stop" else "flip_cikis", -pos, ref_price, fill,
                  (asks[0][0] - bids[0][0]) / 2 / mid, ok, delay)
    gross = S["notional"] * pos * (fill / S["entry"] - 1)
    fee = S["notional"] * FLIP_FEE
    pnl = gross - fee
    S["eq"] += pnl
    S["trades"].append({"o": S["opened"], "c": int(time.time() * 1000), "dir": pos, "entry": S["entry"], "exit": fill,
                        "notional": S["notional"], "pnl": pnl, "why": reason, "eq": S["eq"]})
    S["pos"], S["stop"], S["notional"] = 0, None, 0.0
    S["peak"] = max(S["peak"], S["eq"])
    if FLIP_TRADE_MSGS:
        bot.send_telegram(f"🔁 Kâğıt {'LONG' if pos == 1 else 'SHORT'} kapandı ({reason}) {pnl:+.2f}$ · bakiye {S['eq']:.2f}$")
    if S["eq"] <= S["peak"] * (1 - FLIP_MAX_DD_PCT / 100) and not S["halted"]:
        S["halted"] = True
        bot.send_telegram(f"🛑 KÂĞIT TAKİP ACİL FREN: bakiye zirveden %{FLIP_MAX_DD_PCT:g} düştü "
                          f"({S['peak']:.2f}$ → {S['eq']:.2f}$). Yeni işlem açılmıyor. Sıfırlamak için FLIP_RESET=true.")


def _open(bot, book, direction, line, ref_price, t_ref, kind="giris"):
    bids, asks = book
    mid = (bids[0][0] + asks[0][0]) / 2
    side_levels = asks if direction == 1 else bids
    best = side_levels[0][0]
    dist = abs(best - line) / best
    if not np.isfinite(line) or dist <= 0.0002:
        return
    if FLIP_SIZING == "full":                    # tum teminatla: bakiye x kaldirac (testte dusus %84-95)
        notional = S["eq"] * FLIP_LEV
    else:                                         # risk: stop olursa kayip bakiyenin FLIP_RISK_PCT'i
        risk_usd = S["eq"] * FLIP_RISK_PCT / 100
        notional = min(risk_usd / dist, S["eq"] * FLIP_LEV)
    fill, ok = walk(side_levels, notional)
    delay = int((time.time() - t_ref) * 1000) if t_ref else 0
    _record_order(kind, direction, ref_price, fill, (asks[0][0] - bids[0][0]) / 2 / mid, ok, delay)
    S["eq"] -= notional * FLIP_FEE
    S.update(pos=direction, entry=fill, notional=notional, stop=float(line), opened=int(time.time() * 1000))


def on_bar(bot, df, book_src, t_close):
    """Yeni kapanmis 1 dk mum: cizgiyi takip ettir, donuste kapat + ters ac."""
    line, dirs = bot.calc_supertrend(df, FLIP_ST_PERIOD, FLIP_ST_MULT)
    line, dirs = np.asarray(line, float), np.asarray(dirs, float)
    cur, ln, close = int(dirs[-1]), float(line[-1]), float(df["close"].iloc[-1])
    with _lock:
        prev = S.get("prev_dir")
        if prev is None and FLIP_ENTER_ON_START and not S["pos"] and not S["halted"]:
            # baslangicta bir sonraki donusu beklemeden mevcut SuperTrend yonunde gir (stop = guncel cizgi)
            S["prev_dir"] = cur
            _open(bot, book_src.book(), cur, ln, close, t_close, kind="baslangic")
            if S["pos"]:
                bot.send_telegram(f"🔁 Kâğıt takip mevcut yönde başladı: {'LONG' if cur == 1 else 'SHORT'} · giriş "
                                  f"{S['entry']:,.1f} · stop (SuperTrend çizgisi) {S['stop']:,.1f} · pozisyon ${S['notional']:,.0f}")
            _save(bot, remote=True)
            return
        flipped = prev is not None and cur != prev
        book = book_src.book() if flipped else None       # once defter (hata olursa durum degismesin)
        S["prev_dir"] = cur
        if S["pos"] and np.isfinite(ln) and not flipped:  # iz suren stop
            S["stop"] = max(S["stop"], ln) if S["pos"] == 1 else min(S["stop"], ln)
        if not flipped:
            return
        if S["pos"]:
            _close(bot, book, "flip", close, t_close)
        if not S["halted"]:
            _open(bot, book, cur, ln, close, t_close)
    _save(bot, remote=True)


def _take_profit(bot, tp):
    """Limit TP (maker): defterde bekleyen emir fiyat gelince TP seviyesinden dolar; kayma ve komisyon yok."""
    pos = S["pos"]
    S["orders"].append({"t": int(time.time() * 1000), "k": "tp", "side": -pos, "ref": tp, "fill": tp,
                        "cost": 0.0, "hs": 0.0, "full": True, "ms": 0})
    pnl = S["notional"] * pos * (tp / S["entry"] - 1)
    S["eq"] += pnl
    S["trades"].append({"o": S["opened"], "c": int(time.time() * 1000), "dir": pos, "entry": S["entry"], "exit": tp,
                        "notional": S["notional"], "pnl": pnl, "why": "tp", "eq": S["eq"]})
    S["pos"], S["stop"], S["notional"] = 0, None, 0.0
    S["peak"] = max(S["peak"], S["eq"])
    bot.send_telegram(f"🎯 Kâğıt TP: teminatta %{FLIP_TP_ROE:g} kâr alındı · {pnl:+.2f}$ · bakiye {S['eq']:.2f}$")


def check_stop(bot, book_src):
    with _lock:
        if not S["pos"] or S["stop"] is None:
            return False
        bids, asks = book = book_src.book()
        best = bids[0][0] if S["pos"] == 1 else asks[0][0]
        hit = (S["pos"] == 1 and best <= S["stop"]) or (S["pos"] == -1 and best >= S["stop"])
        if hit:
            _close(bot, book, "stop", S["stop"], None)
        elif FLIP_TP_ROE > 0:
            tp = S["entry"] * (1 + S["pos"] * FLIP_TP_ROE / 100 / FLIP_LEV)
            # limit satis (long) en iyi ALIS fiyati TP'ye ulasinca dolar; short icin en iyi SATIS
            if (S["pos"] == 1 and bids[0][0] >= tp) or (S["pos"] == -1 and asks[0][0] <= tp):
                _take_profit(bot, tp)
                hit = True
    if hit:
        _save(bot, remote=True)
    return hit


def sample_spread(book_src):
    try:
        bids, asks = book_src.book()
        mid = (bids[0][0] + asks[0][0]) / 2
        n = max(S["eq"] * FLIP_LEV / 2, 1000)
        buy, _ = walk(asks, n)
        with _lock:
            S["spreads"].append({"t": int(time.time() * 1000), "hs": (asks[0][0] - bids[0][0]) / 2 / mid,
                                 "imp": buy / mid - 1})
    except Exception:
        pass


# ---------------------------------------------------------------------------- rapor
def summary():
    """Dashboard icin ozet (JSON)."""
    if not S:
        return {"enabled": False}
    with _lock:
        tr, od, sp = list(S["trades"]), list(S["orders"]), list(S["spreads"])
        cur = {"pos": S["pos"], "entry": S["entry"], "stop": S["stop"], "notional": S["notional"], "opened": S["opened"]}
        eq, start, peak, halted, started = S["eq"], S["start_eq"], S["peak"], S["halted"], S["started"]
    od = [o for o in od if o["k"] != "baslangic"]
    costs = [o["cost"] for o in od]
    by = {k: [o["cost"] for o in od if o["k"] == k] for k in ("giris", "flip_cikis", "stop", "tp")}
    curve = [[started, start]] + [[t["c"], t["eq"]] for t in tr]
    return {
        "enabled": True, "symbol": FLIP_SYMBOL, "st": [FLIP_ST_PERIOD, FLIP_ST_MULT], "risk": FLIP_RISK_PCT, "lev": FLIP_LEV,
        "max_dd": FLIP_MAX_DD_PCT, "tp_roe": FLIP_TP_ROE, "halted": halted, "started": started, "eq": eq, "start": start, "peak": peak,
        "dd": (1 - eq / peak) * 100 if peak else 0.0, "n_trades": len(tr), "n_orders": len(od),
        "win": float(np.mean([t["pnl"] > 0 for t in tr]) * 100) if tr else None,
        "avg_cost": float(np.mean(costs)) if costs else None, "threshold": BREAKEVEN_PCT,
        "cost_by": {k: (float(np.mean(v)) if v else None, len(v)) for k, v in by.items()},
        "half_spread": float(np.mean([s["hs"] for s in sp])) if sp else None,
        "impact": float(np.mean([s["imp"] for s in sp])) if sp else None,
        "delay_s": float(np.mean([o["ms"] for o in od if o["ms"]]) / 1000) if any(o["ms"] for o in od) else None,
        "curve": curve[-2000:], "trades": tr[-40:][::-1], "current": cur,
    }


def report_text():
    with _lock:
        tr, od, sp = list(S["trades"]), list(S["orders"]), list(S["spreads"])
        eq, start, peak = S["eq"], S["start_eq"], S["peak"]
    hours = (time.time() * 1000 - S["started"]) / 3600000
    pct = lambda x: f"%{x * 100:.4f}"
    lines = [f"🔁 LIGHTER KÂĞIT TAKİP · {FLIP_SYMBOL} 1 dk · SuperTrend({FLIP_ST_PERIOD},{FLIP_ST_MULT:g}) · "
             (f"TÜM TEMİNAT" if FLIP_SIZING == "full" else f"risk %{FLIP_RISK_PCT:g}") + f" · {FLIP_LEV:g}x" + (f" · TP %{FLIP_TP_ROE:g}" if FLIP_TP_ROE > 0 else "") + (" · 🛑 ACİL FREN AKTİF" if S["halted"] else ""),
             f"⏱ {hours:.1f} saat · {len(tr)} işlem · bakiye ${eq:,.2f} (başlangıç ${start:,.0f}, "
             f"{(eq / start - 1) * 100:+.1f}%) · zirveden düşüş %{(1 - eq / peak) * 100:.1f}"]
    od = [o for o in od if o["k"] != "baslangic"]       # baslangic girisi donus aninda degil: maliyete katilmaz
    if od:
        c = np.array([o["cost"] for o in od])
        for k, name in (("giris", "Giriş (mum kapanışına göre)"), ("flip_cikis", "Dönüş çıkışı (kapanışa göre)"),
                        ("stop", "Stop çıkışı (stop seviyesine göre)"), ("tp", "TP (limit, kaymasız)")):
            cc = [o["cost"] for o in od if o["k"] == k]
            if cc:
                lines.append(f"   {name}: ort. maliyet {pct(np.mean(cc))} ({len(cc)} emir)")
        ms = [o["ms"] for o in od if o["ms"]]
        lines.append(f"📏 Emir başı ORTALAMA maliyet: {pct(c.mean())} · eşik {pct(BREAKEVEN_PCT)} → "
                     f"{'✅ eşiğin altında' if c.mean() < BREAKEVEN_PCT else '❌ eşiğin üstünde'}")
        if ms:
            lines.append(f"   Mum kapanışından dolum hesabına gecikme: ort. {np.mean(ms) / 1000:.1f} sn")
        if any(not o["full"] for o in od):
            lines.append(f"   ⚠️ {sum(not o['full'] for o in od)} emirde defter derinliği yetmedi")
    if sp:
        hs = np.array([s["hs"] for s in sp])
        imp = np.array([s["imp"] for s in sp])
        lines.append(f"📖 Lighter defteri: yarım spread ort. {pct(hs.mean())} · yarım poz. büyüklüğünde emir etkisi "
                     f"ort. {pct(imp.mean())} ({len(sp)} ölçüm)")
    if tr:
        p = np.array([t["pnl"] for t in tr])
        lines.append(f"💰 Kazanan %{(p > 0).mean() * 100:.0f} · ort. kazanç ${p[p > 0].mean() if (p > 0).any() else 0:.2f} · "
                     f"ort. kayıp ${p[p <= 0].mean() if (p <= 0).any() else 0:.2f} · stop ile kapanan "
                     f"%{np.mean([t['why'] == 'stop' for t in tr]) * 100:.0f}")
    lines.append("Gerçek para kullanılmıyor; dolumlar Lighter'ın gerçek emir defterinden hesaplanıyor. Funding hariç.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- dongu
CANDLE_SRC = {"name": None}


def _candles(bot, ex, book_src=None):
    """Sinyal mumlari: once Lighter (islem yapilan yer), olmazsa MEXC."""
    if book_src is not None and (CANDLE_SRC["name"] != "MEXC" or time.time() - CANDLE_SRC.get("t", 0) > 1800):
        try:
            df = book_src.candles()
            if CANDLE_SRC["name"] != "Lighter":
                CANDLE_SRC["name"] = "Lighter"
                bot.log.info("Flip sinyal mumlari: Lighter")
            return df
        except Exception as e:
            CANDLE_SRC["t"] = time.time()
            if CANDLE_SRC["name"] is None:
                CANDLE_SRC["name"] = "MEXC"
                bot.log.warning("Lighter mumlari alinamadi (%s); sinyal MEXC mumlarindan", e)
                bot.send_telegram(f"⚠️ Lighter mum verisi alınamadı ({e}); sinyal MEXC mumlarından hesaplanacak. "
                                  "Fiyat farkı yüzünden dönüşler grafikle birebir örtüşmeyebilir.")
    return _candles_mexc(bot, ex)


def _candles_mexc(bot, ex):
    sym = f"{FLIP_SYMBOL}/USDT:USDT"
    raw = bot.with_retry(ex.fetch_ohlcv, sym, "1m", limit=300, desc=f"flip ohlcv {sym}")
    if not raw:
        return None
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df = df.sort_values("ts").reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    df = df[df["ts"] + 60_000 <= now_ms].reset_index(drop=True)   # sadece kapanmis mumlar
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df if len(df) >= 50 else None


def run(bot, ex_factory):
    log = bot.log
    _load(bot)
    if _env("FLIP_RESET", False, bool):
        S.clear()
        S.update(_new_state())
    book_src = LighterBook(log)
    try:
        mid = book_src.resolve()
        b, a = book_src.book()
        ex = ex_factory()
        _candles(bot, ex, book_src)
        bot.send_telegram(f"🔁 Lighter kâğıt takip başladı: {FLIP_SYMBOL} (piyasa #{mid}) · en iyi alış {b[0][0]:,.1f} / "
                          f"satış {a[0][0]:,.1f} · bakiye ${S['eq']:,.2f} · SuperTrend({FLIP_ST_PERIOD},{FLIP_ST_MULT:g}) · "
                          f"risk %{FLIP_RISK_PCT:g} · {FLIP_LEV:g}x · acil fren %{FLIP_MAX_DD_PCT:g} · sinyal mumları: {CANDLE_SRC['name']}")
    except Exception as e:
        bot.send_telegram(f"⚠️ Lighter kâğıt takip başlatılamadı (emir defterine erişilemiyor): {e}")
        log.exception("Lighter erisim hatasi: %s", e)
        return
    while True:
        try:
            now = time.time()
            nxt = (int(now // 60) + 1) * 60 + 1.0          # her dakika kapanisindan 1 sn sonra
            while time.time() < nxt:
                try:
                    check_stop(bot, book_src)
                except Exception as e:
                    S["errors"] = S.get("errors", 0) + 1
                    log.warning("Flip stop kontrolu hatasi: %s", e)
                time.sleep(min(FLIP_POLL_SEC, max(0.2, nxt - time.time())))
            t_close = int(time.time() // 60) * 60.0         # biten mumun kapanis ani
            df = _candles(bot, ex, book_src)
            if df is not None and df["ts"].iloc[-1] != S.get("last_bar"):
                S["last_bar"] = int(df["ts"].iloc[-1])
                on_bar(bot, df, book_src, t_close)
            sample_spread(book_src)
            if time.time() - S.get("last_report", 0) >= FLIP_REPORT_HOURS * 3600:
                S["last_report"] = time.time()
                bot.send_telegram(report_text())
                _save(bot, remote=True)
        except Exception as e:
            S["errors"] = S.get("errors", 0) + 1
            log.exception("Flip dongu hatasi: %s", e)
            time.sleep(5)


def start(bot, ex_factory):
    if not FLIP_LIVE:
        return None
    th = threading.Thread(target=run, args=(bot, ex_factory), daemon=True, name="flip_live")
    th.start()
    return th
