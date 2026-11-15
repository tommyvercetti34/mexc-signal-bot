"""
Bybit hisse kontratlarinda otomatik emir (once DEMO hesap)
==========================================================
- Sinyal: bist.py motoru (ayni strateji kodu), ABD hissesinin kendi gunluk verisinden (Yahoo).
- Emir: Bybit USDT hisse perpetual kontratlari (TSLAUSDT ...), 1x kaldirac, market emir.
- Zamanlama: ABD kapanisindan sonra (16:30 ET) sinyal. EXEC_FILL=hemen (varsayilan): kontrat 7/24 acik oldugu icin
  emir hemen girilir. EXEC_FILL=acilis: testteki gibi ertesi seans acilisinda (09:32 ET).
  Bot ilk acildiginda son kapanmis gunun sinyallerine bakar (Pazartesiyi beklemez).
- Sadece botun kendi actigi pozisyonlar yonetilir; elle acilanlara dokunulmaz.
- Teminat: hesap degeri / EXEC_SLOTS (kar eklendikce buyur), pozisyon = teminat x EXEC_LEVERAGE (varsayilan 3x).
- Kademeli giris: ilk kademe market, digerleri ATR kadar asagida limit emir (EXEC_LADDER / EXEC_LADDER_ATR).
- Hisse kontratlari ve kripto sinyalleri ayni hesabi paylasir: hesapta en fazla EXEC_SLOTS pozisyon.
- Varsayilan DEMO (api-demo.bybit.com). Gercek para: EXEC_MODE=real VE EXEC_CONFIRM_REAL=evet gerekir.

Render ayarlari:
  EXEC_ENABLED=true
  BYBIT_DEMO_KEY=...  BYBIT_DEMO_SECRET=...      (demo hesapta olusturulan anahtar)
  EXEC_SPEC=mplus:swk=1,vol=1,exit=sinyal,hlen=50 (strateji; ABD testinin sonucuna gore degisir)
Telegram: /bybit
"""

import hashlib
import hmac
import json
import math
import os
import threading
import time
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

import bist

log = logging.getLogger("exec_bybit")
ET = ZoneInfo("America/New_York")


def _env(name, default, cast=str):
    return bist._env(name, default, cast)


EXEC_ENABLED = _env("EXEC_ENABLED", False, bool)
EXEC_MODE = _env("EXEC_MODE", "demo").lower()                 # demo | real
EXEC_CONFIRM_REAL = _env("EXEC_CONFIRM_REAL", "").lower() in ("evet", "yes", "true")
EXEC_SPEC = _env("EXEC_SPEC", "mplus:swk=1,vol=1,exit=sinyal,hlen=50")
EXEC_SLOTS = _env("EXEC_SLOTS", 5, int)
EXEC_LEVERAGE = _env("EXEC_LEVERAGE", 3, int)                 # teminat x kaldirac = pozisyon
EXEC_LADDER = [float(x) for x in _env("EXEC_LADDER", "0.5,0.25,0.25").split(",")]        # kademe agirliklari
EXEC_LADDER_ATR = [float(x) for x in _env("EXEC_LADDER_ATR", "0,0.5,1.0").split(",")]    # kademe uzakligi (ATR)
EXEC_LADDER_DAYS = _env("EXEC_LADDER_DAYS", 5, int)            # dolmayan kademe emirleri bu kadar gun sonra iptal
EXEC_CRYPTO = _env("EXEC_CRYPTO", True, bool)                  # kripto sinyal botunun islemlerini de Bybit'e gonder
EXEC_MAX_ORDER = _env("EXEC_MAX_ORDER_USDT", 0.0, float)      # 0 = sinir yok (islem basina ust sinir)
EXEC_UNIVERSE = _env("EXEC_UNIVERSE", "us100").lower()        # us100: en buyuk 100 ABD hissesiyle kesisim | all
EXEC_SCAN_ET = _env("EXEC_SCAN_ET", "16:30")                  # ABD saatiyle sinyal taramasi
EXEC_OPEN_ET = _env("EXEC_OPEN_ET", "09:32")                  # EXEC_FILL=acilis iken ABD saatiyle emir zamani
EXEC_FILL = _env("EXEC_FILL", "hemen").lower()                # hemen: sinyalden hemen sonra (kontrat 7/24) | acilis
EXEC_DATA = _env("EXEC_DATA", "bybit").lower()                # bybit: kontratin kendi 7/24 mumlari | yahoo: hissenin gunluk verisi
EXEC_TF = _env("EXEC_TF", "D").upper().strip()                # bybit verisinde mum: D | 240 | 60 | 30 | 15 | 5 ...
EXEC_TF = {"15M": "15", "30M": "30", "5M": "5", "1M": "1", "3M": "3", "1H": "60", "2H": "120", "4H": "240",
           "6H": "360", "12H": "720", "1D": "D", "GÜNLÜK": "D"}.get(EXEC_TF, EXEC_TF)
EXEC_MIN_TURNOVER = _env("EXEC_MIN_TURNOVER", 1_000_000.0, float)   # aktif kontrat: son 24 saat islem hacmi (USDT)
EXEC_MIN_BARS = _env("EXEC_MIN_BARS", 150, int)                # sinyal icin en az mum
EXEC_CATCHUP = _env("EXEC_CATCHUP_BARS", 5, int)               # son N mumda AL verip henuz SAT vermemis hisse de alinir
TF_MS = {"D": 86_400_000, **{str(m): m * 60_000 for m in (1, 3, 5, 15, 30, 60, 120, 240, 360, 720)}}
if EXEC_TF not in TF_MS:
    logging.warning("Gecersiz EXEC_TF=%r, gunluk (D) kullaniliyor", EXEC_TF)
    EXEC_TF = "D"


def tf_text(tf=None):
    tf = tf or EXEC_TF
    if tf == "D":
        return "günlük"
    m = TF_MS[tf] // 60_000
    return f"{m // 60} saatlik" if m >= 60 else f"{m} dakikalık"
STATE_FILE = "exec_state.json"
REAL = EXEC_MODE == "real" and EXEC_CONFIRM_REAL
BASE = "https://api.bybit.com" if REAL else "https://api-demo.bybit.com"
PUBLIC = "https://api.bybit.com"
KEY = os.getenv("BYBIT_KEY" if REAL else "BYBIT_DEMO_KEY", "")
SECRET = os.getenv("BYBIT_SECRET" if REAL else "BYBIT_DEMO_SECRET", "")
MODE_TXT = ""                                                  # mesajlarda hesap turu yazilmaz
MT = f" {MODE_TXT}" if MODE_TXT else ""


# ============================================================================
# BYBIT v5 API
# ============================================================================
class BybitError(Exception):
    pass


class Bybit:
    def __init__(self, key, secret, base=BASE, public=PUBLIC, recv=10000):
        self.key, self.secret, self.base, self.public, self.recv = key, secret, base, public, recv

    def sign(self, ts, payload):
        msg = f"{ts}{self.key}{self.recv}{payload}"
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def req(self, method, path, params=None, auth=True, public=False):
        params = params or {}
        url = (self.public if public else self.base) + path
        headers = {"Content-Type": "application/json"}
        if method == "GET":
            qs = urlencode(params)
            payload, data = qs, None
            if qs:
                url += "?" + qs
        else:
            payload = data = json.dumps(params, separators=(",", ":"))
        if auth:
            ts = str(int(time.time() * 1000))
            headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": str(self.recv),
                            "X-BAPI-SIGN": self.sign(ts, payload), "X-BAPI-SIGN-TYPE": "2"})
        r = requests.request(method, url, headers=headers, data=data, timeout=20)
        j = r.json()
        if j.get("retCode") != 0:
            raise BybitError(f"{path}: {j.get('retCode')} {j.get('retMsg')}")
        return j.get("result") or {}

    # --- piyasa ---
    def stock_instruments(self):
        """Hisse perpetual kontratlari: {sembol: {'base','step','min_qty','min_notional'}}."""
        out, cursor = {}, ""
        for _ in range(10):
            p = {"category": "linear", "symbolType": "stock", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            res = self.req("GET", "/v5/market/instruments-info", p, auth=False, public=True)
            for it in res.get("list", []):
                st = (it.get("symbolType") or "").lower()
                if st and st != "stock":
                    continue
                if it.get("status") not in (None, "Trading") or it.get("quoteCoin") != "USDT":
                    continue
                lot = it.get("lotSizeFilter", {})
                out[it["symbol"]] = {"base": it.get("baseCoin") or it["symbol"].replace("USDT", ""),
                                     "step": float(lot.get("qtyStep") or 0.01),
                                     "min_qty": float(lot.get("minOrderQty") or 0),
                                     "min_notional": float(lot.get("minNotionalValue") or 0)}
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    def klines(self, symbol, interval="D", limit=1000):
        """Kapanmis+acik mumlar, eskiden yeniye: [(baslangic_ms, o, h, l, c, hacim)]."""
        res = self.req("GET", "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": interval,
                                                   "limit": limit}, auth=False, public=True)
        rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in res.get("list", [])]
        return sorted(rows)

    def turnovers(self):
        res = self.req("GET", "/v5/market/tickers", {"category": "linear"}, auth=False, public=True)
        return {t["symbol"]: float(t.get("turnover24h") or 0) for t in res.get("list", [])}

    def last_price(self, symbol):
        res = self.req("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol}, auth=False, public=True)
        lst = res.get("list") or []
        return float(lst[0].get("lastPrice") or lst[0].get("markPrice")) if lst else None

    # --- hesap ---
    def equity(self):
        res = self.req("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        lst = res.get("list") or []
        return float(lst[0].get("totalEquity") or 0) if lst else 0.0

    def positions(self):
        res = self.req("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200})
        out = {}
        for p in res.get("list", []):
            size = float(p.get("size") or 0)
            if size > 0:
                out[p["symbol"]] = {"size": size, "side": p.get("side"), "avg": float(p.get("avgPrice") or 0),
                                    "upnl": float(p.get("unrealisedPnl") or 0), "mark": float(p.get("markPrice") or 0),
                                    "im": float(p.get("positionIM") or 0)}
        return out

    def set_leverage(self, symbol, lev):
        try:
            self.req("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": symbol,
                                                           "buyLeverage": str(lev), "sellLeverage": str(lev)})
        except BybitError as e:
            if "110043" not in str(e):                 # "kaldirac zaten bu" hatasi sorun degil
                raise

    def market(self, symbol, side, qty, reduce_only=False, tp=None, sl=None):
        p = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": qty,
             "positionIdx": 0, "reduceOnly": reduce_only}
        if tp:
            p["takeProfit"] = f"{tp:.4f}"
        if sl:
            p["stopLoss"] = f"{sl:.4f}"
        if tp or sl:
            p["tpslMode"] = "Full"
        return self.req("POST", "/v5/order/create", p)


def _px(v):
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _limit(self, symbol, side, qty, price, reduce_only=False, tp=None, sl=None):
    p = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Limit", "qty": qty, "price": _px(price),
         "timeInForce": "GTC", "positionIdx": 0, "reduceOnly": reduce_only}
    if tp:
        p["takeProfit"] = _px(tp)
    if sl:
        p["stopLoss"] = _px(sl)
    if tp or sl:
        p["tpslMode"] = "Full"
    return self.req("POST", "/v5/order/create", p)


def _cancel_all(self, symbol):
    return self.req("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": symbol})


def _trading_stop(self, symbol, sl=None, tp=None):
    p = {"category": "linear", "symbol": symbol, "tpslMode": "Full", "positionIdx": 0}
    if sl:
        p["stopLoss"] = _px(sl)
    if tp:
        p["takeProfit"] = _px(tp)
    return self.req("POST", "/v5/position/trading-stop", p)


Bybit.limit, Bybit.cancel_all, Bybit.trading_stop = _limit, _cancel_all, _trading_stop


def round_qty(qty, step):
    if step <= 0:
        return qty
    n = math.floor(qty / step + 1e-9)
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, dec)


def qty_str(q):
    return f"{q:.8f}".rstrip("0").rstrip(".")


# ============================================================================
# DURUM VE ZAMANLAMA
# ============================================================================
S = {}
_lock = threading.RLock()
_RD = {"ok": False}


def _new_state():
    return {"v": 1, "mine": {}, "pend": {}, "crypto": {}, "scan_day": None, "exec_day": None, "log": [],
            "started": time.time()}


def _used_slots(pos):
    """Hesaptaki dolu yerler: acik pozisyonlar + kademe emri bekleyen bot islemleri (hisse ve kripto)."""
    syms = set(pos) | set(S.get("mine", {})) | {r["sym"] for r in S.get("crypto", {}).values()}
    return len(syms)


def _ladder(api, sym, inf, side, px, atr, margin, tp=None, sl=None):
    """Kademeli giris: ilk kademe market, digerleri ATR kadar ters yonde limit. (gonderilen_adet, mesaj) doner."""
    notional = margin * EXEC_LEVERAGE
    if EXEC_MAX_ORDER > 0:
        notional = min(notional, EXEC_MAX_ORDER)
    sg = 1 if side == "Buy" else -1
    sent, parts = 0.0, []
    for w, off in zip(EXEC_LADDER, EXEC_LADDER_ATR):
        lvl = px - sg * off * atr if off > 0 and atr else px
        q = round_qty(notional * w / lvl, inf["step"])
        if q < inf["min_qty"] or q * lvl < inf["min_notional"] or q <= 0:
            continue
        if off <= 0 or not atr:
            api.market(sym, side, qty_str(q), tp=tp, sl=sl)
            parts.append(f"{qty_str(q)} @ ~{px:,.2f}")
        else:
            api.limit(sym, side, qty_str(q), lvl, tp=tp, sl=sl)
            parts.append(f"{qty_str(q)} @ {lvl:,.2f} (limit)")
        sent += q
    return sent, " + ".join(parts)


def _recent_entry(ent, ex, k):
    """Son k mum icinde AL sinyali varsa ve sonrasinda SAT gelmediyse o mumun indeksi."""
    n = len(ent)
    for j in range(n - 1, max(-1, n - 1 - max(1, k)), -1):
        if ex[j]:
            return None
        if ent[j]:
            return j
    return None


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _note(msg):
    S["log"] = (S.get("log", []) + [{"t": time.time(), "m": msg}])[-300:]


def _universe(insts):
    if EXEC_UNIVERSE == "all":
        return {s: i for s, i in insts.items()}
    allow = set(bist.US_SYMBOLS) | {x.replace("-", ".") for x in bist.US_SYMBOLS}
    return {s: i for s, i in insts.items() if i["base"] in allow or i["base"].replace(".", "-") in allow}


def _yahoo(base):
    return base.replace(".", "-")                         # BRK.B -> BRK-B


def scan(bot, api, now_et, fetcher=None):
    """ABD kapanisindan sonra: her hisse icin son kapanan gunluk mumda sinyal. Bekleyen emirleri yazar."""
    fetcher = fetcher or (lambda sym: bist.fetch(sym, "1d", "2y", retries=2, suffix=""))
    fam, p = bist.parse_spec(EXEC_SPEC)
    SP, P = bist.sig_params(p), bist.exit_params(p)
    for sym, m in list(S["mine"].items()):                # eski, dolmamis kademe emirlerini iptal et
        if m.get("ladder_open") and time.time() - m["t"] > EXEC_LADDER_DAYS * 86400:
            try:
                api.cancel_all(sym)
            except Exception:
                pass
            m["ladder_open"] = False
    insts = _universe(api.stock_instruments())
    today = now_et.date()
    open_now = now_et.weekday() < 5 and now_et.hour * 60 + now_et.minute < 16 * 60 + 15
    buys, sells, n_ok, sig_day = [], [], 0, None
    for sym, inf in insts.items():
        d = fetcher(_yahoo(inf["base"]))
        if d is None or len(d) < bist.WARMUP:
            continue
        if open_now and d.index[-1].date() == today:      # bugunun mumu henuz kapanmadi
            d = d.iloc[:-1]
        if len(d) < bist.WARMUP or (today - d.index[-1].date()).days > 5:
            continue
        sig_day = max(sig_day, d.index[-1].date()) if sig_day else d.index[-1].date()
        n_ok += 1
        lvl = _exit_level(d, fam, p)
        if lvl and sym in S["mine"]:
            S["mine"][sym]["exit_lvl"] = lvl
        ent, ex = bist.SIG[fam](d, SP)
        X = bist.Bars(d)
        j = X.n - 1
        if sym in S["mine"] and ex[j]:
            sells.append(sym)
        elif sym not in S["mine"]:
            k = _recent_entry(ent, ex, EXEC_CATCHUP)
            sig_ts = X.idx[k].isoformat() if k is not None else None
            if k is not None and S.get("used_sig", {}).get(sym) != sig_ts:      # ayni sinyal bir kez kullanilir
                buys.append({"sym": sym, "lvl": lvl, "score": float(X.vr[k]) - (j - k) * 0.01, "atr": float(X.atr[j]),
                             "close": float(X.c[j]), "age": j - k, "sig": sig_ts})
    S["pend"] = {"sell": sells, "buy": sorted(buys, key=lambda b: -b["score"]), "day": today.isoformat(),
                 "tp": P["tp"], "sl": P["sl"]}
    if not open_now:                                       # seans icindeki ilk tarama aksamki taramayi engellemesin
        S["scan_day"] = today.isoformat()
    S["sig_day"] = sig_day.isoformat() if sig_day else (S.get("sig_day") or "yok")
    when = "şimdi giriliyor" if EXEC_FILL == "hemen" else "ABD açılışında"
    msg = (f"🤖 Bybit{MT} · {sig_day:%d.%m} kapanışı sinyalleri ({n_ok}/{len(insts)} hisse) · {when}:"
           + (f"\n🟢 AL: {', '.join(b['sym'].replace('USDT', '') for b in S['pend']['buy'])}" if buys else "")
           + (f"\n🔴 SAT: {', '.join(s.replace('USDT', '') for s in sells)}" if sells else "")
           + ("" if buys or sells else "\nYeni sinyal yok."))
    _note(msg)
    return msg


def _kline_df(rows, now_ms, tf_ms):
    import pandas as pd
    rows = [r for r in rows if r[0] + tf_ms <= now_ms]                # sadece kapanmis mumlar
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(bist.TZ)
    return df.drop(columns="ts")


def _exit_level(d, fam, p):
    """Matrix+ SAT bolgesi: kapanis trend cizgisi - swk*ATR(100) altina inerse (hacim onayiyla) SAT gelir."""
    if fam != "mplus":
        return None
    try:
        c, h, l = d["close"].to_numpy(float), d["high"].to_numpy(float), d["low"].to_numpy(float)
        mid = bist.liq_mid(c, int(p.get("hlen", 50)))[-1]
        a = bist._atr(h, l, c, 100)[-1]
        v = float(mid - float(p.get("swk", 1.0)) * a)
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def scan_live(bot, api, now_ms=None):
    """7/24: Bybit kontratlarinin kendi mumlariyla, her mum kapanisinda. Aktif (likit) hisse kontratlari."""
    now_ms = now_ms or int(time.time() * 1000)
    tf_ms = TF_MS.get(EXEC_TF, 86_400_000)
    fam, p = bist.parse_spec(EXEC_SPEC)
    SP, P = bist.sig_params(p), bist.exit_params(p)
    for sym, m in list(S["mine"].items()):                # eski, dolmamis kademe emirlerini iptal et
        if m.get("ladder_open") and time.time() - m["t"] > EXEC_LADDER_DAYS * 86400:
            try:
                api.cancel_all(sym)
            except Exception:
                pass
            m["ladder_open"] = False
    insts = api.stock_instruments()
    turn = api.turnovers()
    active = {s_: i for s_, i in insts.items() if turn.get(s_, 0) >= EXEC_MIN_TURNOVER or s_ in S["mine"]}
    buys, sells, n_ok, last_bar = [], [], 0, None
    for sym in active:
        d = _kline_df(api.klines(sym, EXEC_TF), now_ms, tf_ms)
        if d is None or len(d) < EXEC_MIN_BARS:
            continue
        n_ok += 1
        last_bar = max(last_bar or d.index[-1], d.index[-1])
        lvl = _exit_level(d, fam, p)
        if lvl and sym in S["mine"]:
            S["mine"][sym]["exit_lvl"] = lvl
        ent, ex = bist.SIG[fam](d, SP)
        X = bist.Bars(d)
        j = X.n - 1
        if sym in S["mine"] and ex[j]:
            sells.append(sym)
        elif sym not in S["mine"]:
            k = _recent_entry(ent, ex, EXEC_CATCHUP)
            sig_ts = X.idx[k].isoformat() if k is not None else None
            if k is not None and S.get("used_sig", {}).get(sym) != sig_ts:      # ayni sinyal bir kez kullanilir
                buys.append({"sym": sym, "lvl": lvl, "score": float(X.vr[k]) - (j - k) * 0.01, "atr": float(X.atr[j]),
                             "close": float(X.c[j]), "age": j - k, "sig": sig_ts})
    S["pend"] = {"sell": sells, "buy": sorted(buys, key=lambda b: -b["score"]), "day": "live", "tp": P["tp"], "sl": P["sl"]}
    S["bar_ts"] = (now_ms // tf_ms) * tf_ms
    tf_txt = tf_text()
    close_t = last_bar + timedelta(milliseconds=tf_ms) if last_bar is not None else None
    msg = (f"🤖 Bybit{MT} · {tf_txt} mum kapanışı ({close_t:%d.%m %H:%M} · {n_ok}/{len(active)} aktif kontrat):"
           if last_bar is not None else f"🤖 Bybit{MT} · tarama: uygun veri yok ({len(active)} aktif kontrat)")
    msg += ((f"\n🟢 AL: {', '.join(b['sym'].replace('USDT', '') for b in S['pend']['buy'])}" if buys else "")
            + (f"\n🔴 SAT: {', '.join(x.replace('USDT', '') for x in sells)}" if sells else "")
            + ("" if buys or sells else "\nYeni sinyal yok."))
    _note(msg)
    if not buys and not sells and tf_ms < 4 * 3_600_000:   # kisa mumlarda "sinyal yok" mesaji Telegram'a gitmez (/bybit'te gorunur)
        return None
    return msg


def execute(bot, api, now_et):
    """ABD seansi acilisinda bekleyen emirler: once satislar, sonra alislar (hacim puani yuksek once)."""
    pend = S.get("pend") or {}
    msgs = []
    pos = api.positions()
    for sym in pend.get("sell", []):
        mine = S["mine"].get(sym)
        p = pos.get(sym)
        if mine:
            try:
                api.cancel_all(sym)                        # dolmamis kademe emirleri
            except Exception:
                pass
        if not mine or not p:
            S["mine"].pop(sym, None)
            continue
        q = p["size"]
        mine = dict(mine, entry=p["avg"] or mine["entry"])
        try:
            api.market(sym, "Sell", qty_str(q), reduce_only=True)
            px = api.last_price(sym) or p["mark"]
            pnl = (px - mine["entry"]) * q
            msgs.append(f"🤖 Bybit{MT} · 🔴 SAT {sym.replace('USDT', '')} {qty_str(q)} @ ~{px:,.2f} · "
                        f"{'✅' if pnl >= 0 else '🔻'} {pnl:+,.2f} USDT ({(px / mine['entry'] - 1) * 100:+.2f}%)")
            S["mine"].pop(sym, None)
        except Exception as e:
            msgs.append(f"⚠️ Bybit SAT {sym} başarısız: {e}")
    pos = api.positions()
    # botun pozisyonu kapanmissa (TP/SL ya da elle) kayittan dus
    for sym in list(S["mine"]):
        if sym not in pos and not S["mine"][sym].get("ladder_open"):
            msgs.append(f"🤖 Bybit · {sym.replace('USDT', '')} pozisyonu kapanmış (hedef/stop ya da elle), kayıttan düşüldü.")
            S["mine"].pop(sym, None)
    eq = api.equity()
    insts = api.stock_instruments()
    for b in pend.get("buy", []):
        sym = b["sym"]
        if _used_slots(pos) >= EXEC_SLOTS:
            msgs.append(f"🤖 Bybit · ⏭ {sym.replace('USDT', '')} AL atlandı (hesapta {EXEC_SLOTS} yer dolu)")
            continue
        if sym in pos or sym in S["mine"]:
            continue                                       # elle acilmis pozisyon ya da zaten bizim
        inf = insts.get(sym)
        px = api.last_price(sym)
        if not inf or not px:
            continue
        margin = eq / EXEC_SLOTS
        tp = px + pend.get("tp", 0) * b["atr"] if pend.get("tp") else None
        sl = px - pend.get("sl", 0) * b["atr"] if pend.get("sl") else None
        try:
            api.set_leverage(sym, EXEC_LEVERAGE)
            q, txt = _ladder(api, sym, inf, "Buy", px, b["atr"], margin, tp=tp, sl=sl)
            if q <= 0:
                msgs.append(f"🤖 Bybit · {sym.replace('USDT', '')} AL atlandı (miktar borsanın alt sınırının altında)")
                continue
            S["mine"][sym] = {"qty": q, "entry": px, "t": time.time(), "ladder_open": len(EXEC_LADDER) > 1,
                              "exit_lvl": b.get("lvl")}
            if b.get("sig"):
                S.setdefault("used_sig", {})[sym] = b["sig"]
            pos[sym] = {"size": 0, "side": "Buy", "avg": px, "upnl": 0, "mark": px}   # yer sayimi icin
            msgs.append(f"🤖 Bybit{MT} · 🟢 AL {sym.replace('USDT', '')} · teminat ~{margin:,.0f} USDT × "
                        f"{EXEC_LEVERAGE}x · kademeler: {txt}"
                        + (f" · hedef {tp:,.2f}" if tp else "") + (f" · stop {sl:,.2f}" if sl else "")
                        + (f" · çıkış: kapanış ~{b['lvl']:,.2f} altına inerse (hacim onaylı SAT)" if b.get("lvl") else ""))
        except Exception as e:
            if "110126" in str(e):                         # hisse kontrati sozlesmesi imzalanmamis: bir kez uyar
                if not S.get("agree_warned"):
                    S["agree_warned"] = True
                    msgs.append(f"⚠️ Bybit {sym.replace('USDT', '')} alınamadı: hisse kontratları için Bybit'te "
                                f"sözleşme onayı gerekiyor. Bybit'te {sym} kontratını aç, çıkan "
                                f"sözleşmeyi onayla. Onaydan sonra sinyaller otomatik alınır; bu uyarı tekrar gelmez.")
                continue
            msgs.append(f"⚠️ Bybit AL {sym} başarısız: {e}")
    S["pend"] = {}
    S["exec_day"] = now_et.date().isoformat()
    for m in msgs:
        _note(m)
    return msgs


def step(bot, api, now=None, fetcher=None):
    now_et = (now or datetime.now(ET)).astimezone(ET)
    if EXEC_DATA == "bybit":                               # 7/24: her mum kapanisinda tara, emri hemen gir
        tf_ms = TF_MS.get(EXEC_TF, 86_400_000)
        now_ms = int(now_et.timestamp() * 1000)
        cur = (now_ms // tf_ms) * tf_ms                    # son kapanan mumun bittigi an
        if S.get("bar_ts") != cur and now_ms - cur >= 60_000:   # kapanistan 1 dk sonra (borsa mumu yazsin)
            out = [m for m in [scan_live(bot, api, now_ms)] if m]
            if S.get("pend"):
                out += execute(bot, api, now_et)
            return out
        return []
    hm = now_et.hour * 60 + now_et.minute
    today = now_et.date().isoformat()
    weekday = now_et.weekday() < 5
    out = []
    # acilis modu: bir onceki taramanin emirleri ABD seansi acilisinda
    if EXEC_FILL != "hemen" and weekday and S.get("pend") and S["pend"].get("day") != today \
            and _hm(EXEC_OPEN_ET) <= hm <= 15 * 60 + 30 and S.get("exec_day") != today:
        out += execute(bot, api, now_et)
    # tarama: ilk acilista hemen (son kapanmis gun), sonra her is gunu kapanistan sonra
    first = not S.get("sig_day")
    if first or (weekday and hm >= _hm(EXEC_SCAN_ET) and S.get("scan_day") != today):
        m = scan(bot, api, now_et, fetcher)
        if m:
            out.append(m)
        if EXEC_FILL == "hemen" and S.get("pend"):
            out += execute(bot, api, now_et)
    return out


def status_text(api=None):
    api = api or API.get("api")
    if not api:
        return "Bybit bağlantısı kapalı (EXEC_ENABLED=true ve API anahtarları gerekli)."
    try:
        eq, pos = api.equity(), api.positions()
    except Exception as e:
        return f"⚠️ Bybit'e bağlanılamadı: {e}"
    lines = [f"🤖 Bybit{MT} · hesap {eq:,.2f} USDT · dolu yer {_used_slots(pos)}/{EXEC_SLOTS} · "
             f"teminat/işlem ~{eq / EXEC_SLOTS:,.0f} USDT × {EXEC_LEVERAGE}x",
             f"Hisse stratejisi: {EXEC_SPEC}"]
    scans = [x for x in S.get("log", []) if "kapanış" in x["m"] or "tarama" in x["m"]]
    if scans:
        last = scans[-1]
        res = last["m"].split("\n", 1)[1].replace("\n", " · ") if "\n" in last["m"] else "-"
        lines.append(f"Son tarama: {datetime.fromtimestamp(last['t'], bist.TZ):%d.%m %H:%M} · {res}")
    else:
        lines.append("Son tarama: henüz yok")
    if EXEC_DATA == "bybit":
        nxt = (int(time.time() * 1000) // TF_MS.get(EXEC_TF, 86_400_000) + 1) * TF_MS.get(EXEC_TF, 86_400_000) + 60_000
        lines.append(f"Sonraki tarama: {datetime.fromtimestamp(nxt / 1000, bist.TZ):%d.%m %H:%M}")
    for k, r in S.get("crypto", {}).items():
        p = pos.get(r["sym"])
        lines.append(f"   • kripto {r['sym']} {'LONG' if r['side'] == 'Buy' else 'SHORT'}"
                     + (f" · {qty_str(p['size'])} · {p['upnl']:+,.2f} USDT" if p else " · kademe bekliyor"))
    for sym, m in S.get("mine", {}).items():
        p = pos.get(sym)
        if p:
            lines.append(f"   • {sym.replace('USDT', '')} {qty_str(p['size'])} · giriş {m['entry']:,.2f} · şimdi "
                         f"{p['mark']:,.2f} · {p['upnl']:+,.2f} USDT"
                         + (f" · çıkış bölgesi < {m['exit_lvl']:,.2f}" if m.get("exit_lvl") else ""))
    pend = S.get("pend") or {}
    if pend.get("buy") or pend.get("sell"):
        lines.append("⏳ Bekleyen: " + " ".join([f"AL {b['sym'].replace('USDT', '')}" for b in pend.get("buy", [])] +
                                               [f"SAT {s.replace('USDT', '')}" for s in pend.get("sell", [])]))
    return "\n".join(lines)


# ============================================================================
# KRIPTO SINYAL BOTU -> BYBIT (kagit motor yonetir, borsa aynalar)
# ============================================================================
def _ckey(t):
    return f"{t['symbol']}|{t['opened_ms']}"


def crypto_open(bot, t, sym_id):
    """Sinyal botu yeni islem actiginda: ayni planla (kademeler, stop, hedef) Bybit'te emir."""
    api = API.get("api")
    if not api or not EXEC_CRYPTO:
        return
    with _lock:
        try:
            s = t["state"]
            side = "Buy" if s["sign"] > 0 else "Sell"
            pos = api.positions()
            if _used_slots(pos) >= EXEC_SLOTS or sym_id in pos:
                bot.send_telegram(f"🤖 Bybit · ⏭ kripto {sym_id} atlandı (hesapta {EXEC_SLOTS} yer dolu ya da açık pozisyon var)")
                return
            inf = _crypto_inst(api, sym_id)
            px = api.last_price(sym_id)
            if not inf or not px:
                return
            margin = api.equity() / EXEC_SLOTS
            api.set_leverage(sym_id, EXEC_LEVERAGE)
            notional = margin * EXEC_LEVERAGE
            parts, sent = [], 0.0
            for i, (lvl, w) in enumerate(zip(s["entries"], s["weights"])):
                price = px if i == 0 else lvl
                q = round_qty(notional * w / price, inf["step"])
                if q < inf["min_qty"] or q * price < inf["min_notional"] or q <= 0:
                    continue
                if i == 0:
                    api.market(sym_id, side, qty_str(q), sl=s["sl"])
                else:
                    api.limit(sym_id, side, qty_str(q), lvl, sl=s["sl"])
                parts.append(f"{qty_str(q)} @ {price:,.6g}")
                sent += q
            if sent <= 0:
                return
            S["crypto"][_ckey(t)] = {"sym": sym_id, "side": side, "sl": s["sl"], "tp1": False, "t": time.time()}
            _save_quiet(bot)
            bot.send_telegram(f"🤖 Bybit{MT} · kripto {'🟢 LONG' if side == 'Buy' else '🔴 SHORT'} {sym_id} · "
                              f"teminat ~{margin:,.0f} USDT × {EXEC_LEVERAGE}x · kademeler: {' + '.join(parts)} · "
                              f"stop {s['sl']:,.6g}")
        except Exception as e:
            bot.send_telegram(f"⚠️ Bybit kripto emri ({sym_id}) başarısız: {e}")


def _crypto_inst(api, sym_id):
    res = api.req("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": sym_id}, auth=False,
                  public=True)
    lst = res.get("list") or []
    if not lst:
        return None
    lot = lst[0].get("lotSizeFilter", {})
    return {"step": float(lot.get("qtyStep") or 0.001), "min_qty": float(lot.get("minOrderQty") or 0),
            "min_notional": float(lot.get("minNotionalValue") or 0)}


def crypto_sync(bot, t, sym_id, pos_cache=None):
    """Her takip turunda: kagit motordaki duruma gore borsadaki pozisyonu esitle (TP1 kismi kapama, stop, kapanis)."""
    api = API.get("api")
    rec = S.get("crypto", {}).get(_ckey(t))
    if not api or not rec:
        return
    with _lock:
        try:
            s = t["state"]
            pos = (pos_cache if pos_cache is not None else api.positions()).get(sym_id)
            close_side = "Sell" if rec["side"] == "Buy" else "Buy"
            if s["closed"]:
                try:
                    api.cancel_all(sym_id)
                except Exception:
                    pass
                txt = ""
                if pos:
                    api.market(sym_id, close_side, qty_str(pos["size"]), reduce_only=True)
                    txt = f" · borsada kapatıldı (açık K/Z {pos['upnl']:+,.2f} USDT)"
                S["crypto"].pop(_ckey(t), None)
                _save_quiet(bot)
                bot.send_telegram(f"🤖 Bybit · kripto {sym_id} işlemi bitti ({s['outcome']}){txt}")
                return
            if s.get("cancelled") and not rec.get("ladder_cancelled"):
                try:
                    api.cancel_all(sym_id)
                except Exception:
                    pass
                rec["ladder_cancelled"] = True
            if pos and s["tp1_done"] and not rec["tp1"]:
                inf = _crypto_inst(api, sym_id)
                q = round_qty(pos["size"] * s["tp1_frac"], inf["step"] if inf else 0.001)
                if q > 0:
                    api.market(sym_id, close_side, qty_str(q), reduce_only=True)
                rec["tp1"] = True
                bot.send_telegram(f"🤖 Bybit · kripto {sym_id} hedef 1: {qty_str(q)} kapatıldı")
            if pos and abs(s["sl"] - rec["sl"]) > 1e-12 * max(1.0, abs(s["sl"])):
                api.trading_stop(sym_id, sl=s["sl"])
                rec["sl"] = s["sl"]
        except Exception as e:
            log.warning("Bybit kripto esitleme hatasi %s: %s", sym_id, e)


def reconcile(api, now=None):
    """Botun kendi pozisyonlari borsada kapanmissa (elle, stop ya da hedef): kalan kademe emirlerini iptal et,
    yeri hemen bosalt. Ilk kademe market oldugu icin pozisyon acilir acilmaz gorunur; yeni emirlere 2 dk tolerans."""
    now = now or time.time()
    pos = api.positions()
    msgs = []
    for sym, m in list(S.get("mine", {}).items()):
        if sym in pos or now - m.get("t", now) < 120:
            continue
        try:
            api.cancel_all(sym)
        except Exception:
            pass
        S["mine"].pop(sym, None)
        msgs.append(f"🤖 Bybit · {sym.replace('USDT', '')} pozisyonu kapanmış (elle / stop / hedef): "
                    f"kalan kademe emirleri iptal edildi, yer boşaldı.")
    for k, r in list(S.get("crypto", {}).items()):
        if r["sym"] in pos or now - r.get("t", now) < 120:
            continue
        try:
            api.cancel_all(r["sym"])
        except Exception:
            pass
        S["crypto"].pop(k, None)
        msgs.append(f"🤖 Bybit · kripto {r['sym']} pozisyonu kapanmış (elle / stop): kalan emirler iptal edildi, "
                    f"yer boşaldı. (Kâğıt takip kendi kurallarıyla devam eder.)")
    for m_ in msgs:
        _note(m_)
    return msgs


def _save_quiet(bot):
    try:
        _save(bot)
    except Exception:
        pass


# ============================================================================
# KALICILIK (Gist) ve BASLATMA
# ============================================================================
def _save(bot):
    with _lock:
        payload = json.dumps(S, ensure_ascii=False, default=float)
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        pass
    if bot._gist_enabled() and _RD["ok"]:
        try:
            requests.patch(f"https://api.github.com/gists/{bot.GIST_ID}", headers=bot._gist_headers(),
                           json={"files": {STATE_FILE: {"content": payload}}}, timeout=30)
        except Exception:
            pass


def _load(bot):
    data = None
    if bot._gist_enabled():
        try:
            r = requests.get(f"https://api.github.com/gists/{bot.GIST_ID}", headers=bot._gist_headers(), timeout=30)
            r.raise_for_status()
            f = r.json().get("files", {}).get(STATE_FILE)
            if f:
                txt = f.get("content") or ""
                if f.get("truncated") and f.get("raw_url"):
                    txt = requests.get(f["raw_url"], headers=bot._gist_headers(), timeout=60).text
                data = json.loads(txt) if txt.strip() else None
            _RD["ok"] = True
        except Exception as e:
            bot.log.warning("Bybit durumu Gist'ten okunamadi (okunana kadar yazilmayacak): %s", e)
    if data is None and os.path.exists(STATE_FILE):
        try:
            data = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            data = None
    S.clear()
    S.update(_new_state())
    if data:
        S.update(data)


API = {}
SNAP = {}                                         # panel icin son durum (dakikada bir yenilenir)


def refresh_snapshot(api):
    try:
        eq, pos = api.equity(), api.positions()
    except Exception as e:
        SNAP.update({"enabled": True, "error": str(e), "ts": time.time()})
        return
    bot_syms = set(S.get("mine", {})) | {r["sym"] for r in S.get("crypto", {}).values()}
    rows = []
    for sym, p in pos.items():
        kind = "hisse" if sym in S.get("mine", {}) else ("kripto" if sym in bot_syms else "elle")
        rows.append({"sym": sym, "kind": kind, "side": p["side"], "size": p["size"], "avg": p["avg"],
                     "mark": p["mark"], "upnl": p["upnl"],
                     "exit": (S.get("mine", {}).get(sym) or {}).get("exit_lvl"), "im": p.get("im", 0)})
    # gunluk K/Z: TR saatiyle gun basindaki hesap degerine gore; % = bugun kullanilan teminata (girilen miktar) gore
    today = datetime.now(bist.TZ).date().isoformat()
    im_now = sum(p.get("im", 0) for p in pos.values())
    de = S.get("day_eq") or {}
    if de.get("d") != today:
        de = {"d": today, "eq": eq, "im": 0.0}
        S["day_dirty"] = True
    de["im"] = max(de.get("im", 0.0), im_now)
    S["day_eq"] = de
    hist = S.setdefault("eq_hist", [])                    # sermaye egrisi: 15 dk'da bir nokta (Gist'e saatte bir yazilir)
    now_s = time.time()
    if not hist or now_s - hist[-1][0] >= 900:
        hist.append([int(now_s), round(eq, 2)])
        del hist[:-4000]
        if len(hist) % 4 == 1:
            S["day_dirty"] = True
    if im_now > S.get("max_im", 0.0):                    # toplam K/Z % icin: bugune kadar girilen en yuksek teminat
        S["max_im"] = im_now
        S["day_dirty"] = True
    tf_ms = TF_MS.get(EXEC_TF, 86_400_000)
    nxt = (int(time.time() * 1000) // tf_ms + 1) * tf_ms + 60_000 if EXEC_DATA == "bybit" else None
    SNAP.update({"enabled": True, "error": None, "ts": time.time(), "mode": MODE_TXT, "equity": eq,
                 "slots": EXEC_SLOTS, "used": _used_slots(pos), "lev": EXEC_LEVERAGE, "spec": EXEC_SPEC,
                 "tf": EXEC_TF, "data": EXEC_DATA, "margin": eq / EXEC_SLOTS, "positions": rows,
                 "pend": S.get("pend") or {}, "log": [{**x, "m": str(x.get("m", "")).replace("Bybit demo hesap", "Bybit")} for x in S.get("log", [])[-80:]], "next_scan": nxt,
                 "start_eq": S.setdefault("start_eq", eq), "day_start_eq": de["eq"], "day_pnl": eq - de["eq"],
                 "day_im": de["im"], "im_now": im_now, "max_im": S.get("max_im", 0.0),
                 "eq_hist": hist + [[int(now_s), round(eq, 2)]]})


def api_data():
    return dict(SNAP) if SNAP else {"enabled": False}


def run(bot):
    _load(bot)
    api = Bybit(KEY, SECRET)
    API["api"] = api
    try:
        eq = api.equity()
        insts = api.stock_instruments()
        if EXEC_DATA == "bybit":
            turn = api.turnovers()
            n = sum(1 for s_ in insts if turn.get(s_, 0) >= EXEC_MIN_TURNOVER)
            mode = (f"7/24 tarama · Bybit {tf_text()} mumlar · aktif kontratlar (24s hacim ≥ "
                    f"{EXEC_MIN_TURNOVER:,.0f} USDT)")
        else:
            n = len(_universe(insts))
            mode = "sinyalden hemen sonra" if EXEC_FILL == "hemen" else "ABD açılışında"
        bot.send_telegram(f"🤖 Bybit{MT} bağlandı · hesap {eq:,.2f} USDT · {n} hisse kontratı izleniyor · "
                          f"en fazla {EXEC_SLOTS} pozisyon (hisse + kripto), teminat bakiye/{EXEC_SLOTS} × "
                          f"{EXEC_LEVERAGE}x, kademeli giriş · {mode} · strateji {EXEC_SPEC} · /bybit")
    except BybitError as e:
        bot.send_telegram(f"⚠️ Bybit{MT} bağlantısı kurulamadı: {e}\n"
                          f"Anahtarın doğru hesapta oluşturulduğundan ve 'Contract - Trade' yetkisi olduğundan emin ol.")
    except Exception as e:
        bot.send_telegram(f"⚠️ Bybit modülü başlarken hata: {e}\n"
                          f"(GitHub'daki bist.py en son sürüm mü? ABD hisse listesi ve veri fonksiyonu oradan geliyor.)")
    last_rec = 0.0
    refresh_snapshot(api)
    while True:
        try:
            with _lock:
                msgs = step(bot, api)
                if time.time() - last_rec >= 120:
                    last_rec = time.time()
                    msgs += reconcile(api)
            for m in msgs:
                bot.send_telegram(m)
            if msgs:
                _save(bot)
            refresh_snapshot(api)
            if S.pop("day_dirty", False):
                _save_quiet(bot)
        except Exception as e:
            bot.log.exception("Bybit dongu hatasi: %s", e)
        time.sleep(60)


def start(bot):
    if not EXEC_ENABLED:
        return False
    if not hasattr(bist, "US_SYMBOLS") or not hasattr(bist, "Bars"):
        bot.send_telegram("⚠️ Bybit modülü başlamadı: GitHub'daki bist.py eski sürüm. En son gönderilen bist.py'yi yükle.")
        return False
    if EXEC_MODE == "real" and not EXEC_CONFIRM_REAL:
        bot.send_telegram("⚠️ EXEC_MODE=real ama EXEC_CONFIRM_REAL=evet yok: güvenlik için demo hesapla çalışılıyor.")
    if not KEY or not SECRET:
        bot.send_telegram("⚠️ Bybit anahtarları eksik (BYBIT_DEMO_KEY / BYBIT_DEMO_SECRET). Bybit emir modülü başlamadı.")
        return False
    threading.Thread(target=run, args=(bot,), daemon=True, name="exec_bybit").start()
    try:
        import flip_live
        flip_live.EXTRA_CMDS["/bybit"] = lambda: status_text()
        flip_live.start_commands(bot)
    except Exception as e:
        bot.log.warning("/bybit komutu baglanamadi: %s", e)
    return True
