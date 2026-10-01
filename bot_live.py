"""
BOT DE SURVIE — ORB 5 min « Stocks in Play », achat seul, 50 $ VIRTUELS, compte Alpaca PAPER.

Test de 2 semaines. Objectif principal : mesurer en conditions réelles la part des trades
stoppés dans la minute d'entrée (backtest : 13 % en hypothèse optimiste, 47 % en pessimiste).

Fonctionnement (lancé automatiquement par GitHub Actions, 2 fois par jour de bourse) :
  - 9:35 NY  : choisit le Top 5 (volume relatif de la 1re bougie de 5 min), garde les bougies
               haussières, place un ordre d'achat « stop » au plus haut de cette bougie (1 action).
  - Toute la journée : dès qu'un achat est exécuté, place le stop à 10 % de l'ATR sous le prix payé.
  - 15:55 NY : annule tout, ferme tout, calcule la journée, écrit le journal.
Le compte paper achète 1 action par trade (pour mesurer les vrais prix d'exécution).
Le capital du bot (50 $ au départ) est calculé à part, avec les règles de taille du backtest.

Règles de survie : 2 % de risque par trade, 20 % du capital max par position,
arrêt de la journée à −5 %, « mort » si le capital virtuel passe sous 0,50 $.
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import time as systime
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

NY, PARIS = ZoneInfo("America/New_York"), ZoneInfo("Europe/Paris")
UNIVERSE = """AAPL MSFT NVDA AMZN META GOOGL TSLA AMD NFLX AVGO ORCL CRM ADBE INTC MU QCOM TXN AMAT LRCX KLAC
MRVL SMCI ARM PLTR SNOW CRWD PANW NET DDOG ZS SHOP UBER LYFT ABNB DASH RBLX U COIN HOOD SOFI PYPL AFRM
UPST MARA RIOT MSTR RIVN LCID NIO F GM BA CAT DE GE JPM BAC C WFC GS MS SCHW XOM CVX OXY SLB HAL DVN
FCX NEM AA CLF UNH LLY PFE MRNA JNJ ABBV BMY GILD CVS WMT COST TGT HD LOW NKE SBUX MCD DIS WBD T VZ
CMCSA ROKU SNAP PINS SPOT ZM DOCU ENPH FSLR PLUG CCL AAL UAL DAL""".split()

TOP_N, RISK, MAX_POS, ATR_STOP = 5, 0.02, 0.20, 0.10
START_CAPITAL, DAILY_STOP, DEATH = 50.0, 0.05, 0.50
SELECT_AT, LATEST_SELECT = time(9, 35, 20), time(10, 30)
PHASE_A_END, FLATTEN_AT = time(12, 45), time(15, 55)
POLL = 10  # secondes
STATE, TRADES, JOURNAL, STATUS = Path("state.json"), Path("trades.csv"), Path("JOURNAL.md"), Path("README.md")


def log(msg):
    print(f"{datetime.now(NY):%Y-%m-%d %H:%M:%S} NY | {msg}", flush=True)


# ============================================================== accès Alpaca (paper uniquement)
class Broker:
    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        k, s = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not k or not s:
            sys.exit("Clés manquantes (secrets GitHub ALPACA_API_KEY / ALPACA_SECRET_KEY).")
        self.trading = TradingClient(k, s, paper=True)          # PAPER forcé, jamais de compte réel
        self.data = StockHistoricalDataClient(k, s)

    def now(self):
        return datetime.now(NY)

    def is_trading_day(self):
        from alpaca.trading.requests import GetCalendarRequest
        d = self.now().date()
        cal = self.trading.get_calendar(GetCalendarRequest(start=d, end=d))
        return bool(cal)

    def daily_bars(self, symbols, start, end):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start, end=end,
                               feed=DataFeed.SIP, adjustment=Adjustment.ALL)
        return self.data.get_stock_bars(req).df

    def bars_5min_iex(self, symbols, start):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start, feed=DataFeed.IEX)
        return self.data.get_stock_bars(req).df

    def last_price(self, sym):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        r = self.data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))
        return float(r[sym].price)

    def orders_today(self):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        after = datetime.combine(self.now().date(), time(4, 0), tzinfo=NY)
        return self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=500))

    def stop_order(self, sym, side, qty, stop_price, coid):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        return self.trading.submit_order(StopOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, stop_price=stop_price, client_order_id=coid))

    def market_buy(self, sym, qty, coid):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        return self.trading.submit_order(MarketOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, client_order_id=coid))

    def market_sell(self, sym, qty, coid):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        return self.trading.submit_order(MarketOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=coid))

    def cancel(self, order_id):
        self.trading.cancel_order_by_id(order_id)

    def positions(self):
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}


# ============================================================== état persistant (dans le dépôt GitHub)
def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"equity": START_CAPITAL, "alive": True, "started": None, "days": {}}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1, default=str))


# ============================================================== sélection du matin
def select(broker, today):
    """Top 5 par volume relatif de la bougie 9:30-9:35 ; ne garde que les bougies haussières."""
    d0 = broker.daily_bars(UNIVERSE, datetime.combine(today - timedelta(days=45), time(0), tzinfo=NY),
                           datetime.combine(today - timedelta(days=1), time(23, 59), tzinfo=NY))
    feats = {}
    for sym, g in d0.groupby(level="symbol"):
        g = g.droplevel("symbol").sort_index()
        if len(g) < 15:
            continue
        pc = g["close"].shift(1)
        tr = pd.concat([g.high - g.low, (g.high - pc).abs(), (g.low - pc).abs()], axis=1).max(axis=1)
        feats[sym] = dict(atr=float(tr.iloc[-14:].mean()), avgvol=float(g.volume.iloc[-14:].mean()))
    b5 = pd.DataFrame()
    for attempt in range(8):            # la bougie 9:30 peut mettre quelques secondes à arriver
        b5 = broker.bars_5min_iex(UNIVERSE, datetime.combine(today - timedelta(days=30), time(9, 0), tzinfo=NY))
        b5 = b5.reset_index()
        b5["ts"] = pd.to_datetime(b5["timestamp"]).dt.tz_convert(NY)
        first = b5[b5.ts.dt.time == time(9, 30)]
        if (first.ts.dt.date == today).sum() >= len(UNIVERSE) * 0.6:
            break
        log(f"bougie 9:30 incomplète ({(first.ts.dt.date == today).sum()} actions), nouvel essai…")
        systime.sleep(15)
    first = b5[b5.ts.dt.time == time(9, 30)].copy()
    first["d"] = first.ts.dt.date
    rows = []
    for sym, g in first.groupby("symbol"):
        g = g.sort_values("d")
        tod = g[g.d == today]
        hist = g[g.d < today].tail(14)
        if tod.empty or len(hist) < 10 or sym not in feats:
            continue
        b = tod.iloc[0]
        rel = b.volume / hist.volume.mean() if hist.volume.mean() > 0 else 0
        rows.append(dict(sym=sym, open=float(b.open), high=float(b.high), low=float(b.low), close=float(b.close),
                         relvol=float(rel), **feats[sym]))
    c = pd.DataFrame(rows)
    if c.empty:
        return [], c
    c = c[(c.open > 5) & (c.avgvol > 1e6) & (c.atr > 0.5) & (c.relvol >= 1) & (c.close != c.open)]
    c = c.sort_values("relvol", ascending=False).head(TOP_N)
    picks = c[c.close > c.open]
    return [dict(sym=r.sym, trigger=round(math.ceil(r.high * 100) / 100, 2), stopdist=round(ATR_STOP * r.atr, 4),
                 relvol=round(r.relvol, 2), atr=round(r.atr, 3)) for r in picks.itertuples()], c


# ============================================================== boucle de la journée
def coid(today, sym, kind):
    return f"orb-{today:%Y%m%d}-{sym}-{kind}"


def by_coid(orders):
    return {o.client_order_id: o for o in orders if o.client_order_id and o.client_order_id.startswith("orb-")}


def place_entries(broker, day, today):
    existing = by_coid(broker.orders_today())
    for p in day["picks"]:
        cid = coid(today, p["sym"], "E")
        if cid in existing:
            continue
        try:
            px = broker.last_price(p["sym"])
            if px >= p["trigger"]:
                # cassure déjà faite : entrée immédiate au marché (comme l'entrée « gap » du backtest)
                broker.market_buy(p["sym"], 1, cid)
                p["market_entry"] = True
                log(f"{p['sym']}: déjà au-dessus du déclencheur ({px:.2f} ≥ {p['trigger']}) → achat au marché")
            else:
                broker.stop_order(p["sym"], "buy", 1, p["trigger"], cid)
                log(f"{p['sym']}: ordre d'achat stop à {p['trigger']} (stop prévu à −{p['stopdist']:.2f} $)")
        except Exception as e:
            if "stop price must be greater" in str(e):
                try:
                    broker.market_buy(p["sym"], 1, cid)
                    p["market_entry"] = True
                    log(f"{p['sym']}: le prix a dépassé le déclencheur entre-temps → achat au marché")
                    continue
                except Exception as e2:
                    e = e2
            log(f"{p['sym']}: ordre refusé ({e})")
            p["error"] = str(e)[:120]


def manage(broker, day, today, st):
    """Place les stops de protection dès qu'un achat est exécuté. Vérifie l'arrêt journalier."""
    orders = by_coid(broker.orders_today())
    for p in day["picks"]:
        e = orders.get(coid(today, p["sym"], "E"))
        if e is None or str(e.status).split(".")[-1].lower() != "filled":
            continue
        s_id = coid(today, p["sym"], "S")
        if s_id in orders or p.get("stop_placed"):
            continue
        fill = float(e.filled_avg_price)
        stop = math.floor((fill - p["stopdist"]) * 100) / 100
        px = broker.last_price(p["sym"])
        if px <= stop:
            broker.market_sell(p["sym"], float(e.filled_qty), coid(today, p["sym"], "X1"))
            log(f"{p['sym']}: déjà sous le stop au moment de le placer → vente immédiate")
        else:
            broker.stop_order(p["sym"], "sell", float(e.filled_qty), stop, s_id)
            log(f"{p['sym']}: acheté à {fill:.2f} → stop placé à {stop:.2f}")
        p["stop_placed"] = True
    # arrêt journalier à −5 % (sur le capital virtuel) : pertes réalisées + latentes
    pnl, pos = 0.0, broker.positions()
    all_orders = broker.orders_today()
    for p in day["picks"]:
        e = orders.get(coid(today, p["sym"], "E"))
        if e is None or not e.filled_avg_price:
            continue
        fill = float(e.filled_avg_price)
        sh = virtual_shares(day["equity_start"], fill, p["stopdist"])
        if pos.get(p["sym"], 0) > 0:
            px = broker.last_price(p["sym"])
        else:
            sells = [o for o in all_orders if o.symbol == p["sym"] and o.filled_avg_price and
                     o.client_order_id and not o.client_order_id.endswith("-E")]
            px = float(sells[-1].filled_avg_price) if sells else fill
        pnl += sh * (px - fill)
    if pnl <= -DAILY_STOP * day["equity_start"] and not day.get("daily_stop"):
        log("Arrêt journalier −5 % atteint → on ferme tout")
        day["daily_stop"] = True
        flatten(broker, today)


def flatten(broker, today):
    for o in broker.orders_today():
        if o.client_order_id and o.client_order_id.startswith(f"orb-{today:%Y%m%d}") and \
                str(o.status).split(".")[-1].lower() in ("new", "accepted", "pending_new", "partially_filled", "held"):
            try:
                broker.cancel(o.id)
            except Exception as e:
                log(f"annulation impossible {o.client_order_id}: {e}")
    systime.sleep(3)
    for sym, qty in broker.positions().items():
        if qty > 0:
            broker.market_sell(sym, qty, coid(today, sym, f"X{int(systime.time()) % 100000}"))
            log(f"{sym}: fermeture de fin de journée ({qty} action)")


def virtual_shares(equity, entry, stopdist):
    return min(equity * RISK / stopdist, equity * MAX_POS / entry)


# ============================================================== fin de journée : calcul & journal
def finalize(broker, day, today, st):
    systime.sleep(5)
    orders = [o for o in broker.orders_today() if o.client_order_id and
              o.client_order_id.startswith(f"orb-{today:%Y%m%d}")]
    eq0 = day["equity_start"]
    rows, pnl_total = [], 0.0
    for p in day["picks"]:
        mine = [o for o in orders if o.symbol == p["sym"]]
        e = next((o for o in mine if o.client_order_id.endswith("-E")), None)
        if e is None or not e.filled_avg_price:
            rows.append(dict(date=today, sym=p["sym"], triggered=False))
            continue
        sells = sorted([o for o in mine if o.client_order_id.split("-")[-1] != "E" and o.filled_avg_price],
                       key=lambda o: o.filled_at)
        if not sells:
            rows.append(dict(date=today, sym=p["sym"], triggered=True, note="sortie introuvable"))
            continue
        x = sells[-1]
        entry, exit_ = float(e.filled_avg_price), float(x.filled_avg_price)
        t_in, t_out = pd.Timestamp(e.filled_at).tz_convert(NY), pd.Timestamp(x.filled_at).tz_convert(NY)
        R = (exit_ - entry) / p["stopdist"]
        stopped = x.client_order_id.endswith("-S") or x.client_order_id.endswith("-X1")
        same_min = stopped and t_in.floor("min") == t_out.floor("min")
        sh = virtual_shares(eq0, entry, p["stopdist"])
        pnl = sh * (exit_ - entry)
        pnl_total += pnl
        rows.append(dict(date=today, sym=p["sym"], triggered=True, trigger=p["trigger"], entry=round(entry, 4),
                         exit=round(exit_, 4), t_entry=f"{t_in:%H:%M:%S}", t_exit=f"{t_out:%H:%M:%S}",
                         stopdist=p["stopdist"], R=round(R, 3), stopped=stopped, stopped_same_minute=same_min,
                         slippage_entry_R=round((entry - p["trigger"]) / p["stopdist"], 3),
                         virtual_shares=round(sh, 5), virtual_pnl=round(pnl, 4)))
    st["equity"] = round(eq0 + pnl_total, 4)
    if st["equity"] < DEATH:
        st["alive"] = False
    day["final"] = True
    day["pnl"] = round(pnl_total, 4)
    write_outputs(st, today, day, rows)


def write_outputs(st, today, day, rows):
    fields = ["date", "sym", "triggered", "trigger", "entry", "exit", "t_entry", "t_exit", "stopdist", "R",
              "stopped", "stopped_same_minute", "slippage_entry_R", "virtual_shares", "virtual_pnl", "note"]
    new = not TRADES.exists()
    with TRADES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    done = [r for r in rows if r.get("R") is not None]
    lines = [f"\n## {today:%A %d %B %Y}"]
    if day.get("skipped"):
        lines.append(f"- ⏭️ Journée sautée : {day.get('skip_reason', 'raison inconnue')}")
    else:
        top5 = ", ".join(f"{c['sym']} ({c['relvol']:.1f}x)" for c in day.get("top5", [])) or "aucune action éligible"
        lines.append(f"- Top 5 à {day.get('selected_at', '?')} NY : {top5}")
        lines.append(f"- Achats possibles (bougie haussière) : "
                     f"{', '.join(p['sym'] for p in day['picks']) or 'aucun — pas de bougie haussière dans le Top 5'}")
        for p in day["picks"]:
            if p.get("error"):
                lines.append(f"  - ⚠️ {p['sym']} : ordre refusé ({p['error']})")
            elif p.get("market_entry"):
                lines.append(f"  - {p['sym']} : cassure déjà faite → achat au marché")
    lines.append(f"- Trades exécutés : {len(done)}")
    for r in done:
        lines.append(f"  - {r['sym']} : achat {r['entry']} à {r['t_entry']} → sortie {r['exit']} à {r['t_exit']} "
                     f"= **{r['R']:+.2f} R**{' (stoppé dans la minute d’entrée)' if r['stopped_same_minute'] else ''}")
    if day.get("daily_stop"):
        lines.append("- ⚠️ Arrêt journalier −5 % déclenché")
    lines.append(f"- Résultat du jour : {day['pnl']:+.2f} $ → capital du bot : **{st['equity']:.2f} $** "
                 f"({'EN VIE' if st['alive'] else '💀 MORT'})")
    with JOURNAL.open("a") as f:
        f.write("\n".join(lines) + "\n")
    write_status(st)


def write_status(st):
    t = pd.read_csv(TRADES) if TRADES.exists() else pd.DataFrame()
    t = t[t.get("R", pd.Series(dtype=float)).notna()] if len(t) else t
    n = len(t)
    stopped_min = t.stopped_same_minute.astype(str).str.lower().eq("true").mean() if n else 0
    txt = f"""# 🤖 Bot de survie ORB — état au {datetime.now(PARIS):%d/%m/%Y %H:%M} (Paris)

| | |
|---|---|
| Capital du bot (départ 50 $) | **{st['equity']:.2f} $** |
| Statut | {'✅ EN VIE' if st['alive'] else '💀 MORT'} |
| Jours de test | {sum(1 for d in st['days'].values() if d.get('final') and not d.get('skipped'))} |
| Trades exécutés | {n} |
| Gagnants | {(t.R > 0).mean()*100 if n else 0:.0f} % |
| R moyen réel | {t.R.mean() if n else 0:+.3f} |
| **Stoppés dans la minute d'entrée** | **{stopped_min*100:.0f} %** (backtest : 13 % optimiste / 47 % pessimiste) |
| Glissement moyen à l'entrée | {t.slippage_entry_R.mean() if n else 0:+.3f} R |

Le test porte sur 2 semaines : trop peu de trades pour juger la rentabilité,
mais assez pour savoir si le % « stoppés dans la minute d'entrée » est proche de 13 % ou de 47 %.
Compte Alpaca PAPER uniquement — aucun argent réel.
"""
    STATUS.write_text(txt)


# ============================================================== programme principal
def main(broker=None):
    broker = broker or Broker()
    st = load_state()
    now = broker.now()
    today = now.date()
    if not st["alive"]:
        log("Le bot est mort (capital épuisé). Rien à faire."); return
    if now.time() < time(7, 0) or now.time() > time(16, 10):
        # lancement manuel hors séance = simple test de connexion, sans rien modifier
        if hasattr(broker, "trading"):
            acc = broker.trading.get_account()
            log(f"Connexion OK ✔ — compte PAPER, statut {acc.status}, solde {float(acc.equity):,.2f} $ (fictifs)")
        log(f"Capital du bot : {st['equity']:.2f} $ — hors séance, rien à faire.")
        return
    if not broker.is_trading_day():
        log("Pas de bourse aujourd'hui."); return
    st["started"] = st["started"] or str(today)
    day = st["days"].setdefault(str(today), {"picks": [], "equity_start": st["equity"]})
    if day.get("final"):
        log("Journée déjà terminée et enregistrée."); return
    until = PHASE_A_END if now.time() < time(12, 30) else FLATTEN_AT

    # 1) sélection du matin
    if not day.get("selected"):
        if now.time() > LATEST_SELECT:
            reason = (f"le workflow GitHub n'a démarré qu'à {now:%H:%M} (heure de New York), "
                      f"après l'heure limite de sélection ({LATEST_SELECT:%H:%M})")
            log(f"Journée sautée : {reason}.")
            day.update(selected=True, skipped=True, picks=[], skip_reason=reason)
        else:
            wait = (datetime.combine(today, SELECT_AT, tzinfo=NY) - broker.now()).total_seconds()
            if wait > 0:
                log(f"Attente de la fin de la 1re bougie ({wait/60:.0f} min)…"); systime.sleep(wait)
            picks, cand = select(broker, today)
            day.update(selected=True, picks=picks, selected_at=f"{broker.now():%H:%M:%S}",
                       top5=cand[["sym", "relvol"]].to_dict("records") if len(cand) else [])
            log(f"Top 5 : {[c['sym'] for c in day['top5']]} → achats possibles : {[p['sym'] for p in picks]}")
            place_entries(broker, day, today)
        save_state(st)

    # 2) surveillance (on s'arrête avant la limite de durée d'un job GitHub : 6 h)
    deadline = now + timedelta(minutes=335)
    while broker.now().time() < min(until, FLATTEN_AT) and broker.now() < deadline:
        try:
            manage(broker, day, today, st)
        except Exception as e:
            log(f"erreur de surveillance (on continue) : {e}")
        save_state(st)
        systime.sleep(POLL)

    # 3) fin de journée
    if broker.now().time() >= FLATTEN_AT:
        flatten(broker, today)
        finalize(broker, day, today, st)
        log(f"Journée terminée. Capital du bot : {st['equity']:.2f} $")
    elif broker.now().time() < FLATTEN_AT:
        Path(".dispatch_next").write_text("1")   # le workflow relance aussitôt la phase suivante
        log("Fin de cette phase → relance automatique de la phase suivante.")
    save_state(st)


if __name__ == "__main__":
    main()
