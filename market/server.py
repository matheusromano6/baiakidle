"""Painel web local: oportunidades, posicoes e acompanhamento de lucro.

  python server.py           inicia o servidor e abre o navegador
  python server.py --no-open  sem abrir o navegador

Mercado varrido a cada poll_seconds (padrao 2 min). O preco ao vivo do que ja
esta monitorado (oportunidades, acompanhamentos e posicoes abertas) e' consultado
individualmente a cada track_poll_seconds (padrao 10 s) ate encerrar.
"""
import argparse
import json
import os
import platform
import subprocess
import sqlite3
import statistics
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import analyze
import api
import items
import scanner
from store import Store

VOCATIONS = ["knight", "paladin", "sorcerer", "druid", "monk"]

HERE = Path(__file__).parent
CFG = scanner.load_config()
CFG["db_path"] = os.environ.get("BAIAK_DB", CFG["db_path"])


def _backup_db(path, keep=15):
    """Copia consistente do banco pra backups/ na inicializacao."""
    src = Path(path)
    if not src.exists():
        return
    bdir = src.parent / "backups"
    bdir.mkdir(exist_ok=True)
    dst = bdir / f"{src.stem}-{time.strftime('%Y%m%d-%H%M%S')}{src.suffix}"
    try:
        con = sqlite3.connect(str(src))
        bk = sqlite3.connect(str(dst))
        with bk:
            con.backup(bk)
        bk.close()
        con.close()
    except Exception as e:  # noqa: BLE001
        print(f"[backup] falhou: {e}")
        return
    old = sorted(bdir.glob(f"{src.stem}-*{src.suffix}"))[:-keep]
    for f in old:
        f.unlink(missing_ok=True)
    print(f"[backup] {dst.name}")


STORE = Store(CFG["db_path"])
ACTIVE = []            # ultimo snapshot de leiloes ativos (pra concorrencia)
ACTIVE_AT = 0
_GM = {"at": 0, "data": None}      # cache do momento do mercado de gold (5 min)


def _gold_market():
    now = time.time()
    if now - _GM["at"] > 300:
        data = analyze.gold_market(STORE, CFG)
        if data:
            rows, auctions = STORE.gold_snap_counts()
            data["snaps"], data["snap_auctions"] = rows, auctions
        _GM.update(at=now, data=data)
    return _GM["data"]


# ---------------------------------------------------------------- pnl / state
def _midnight_ms():
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


def position_net(p):
    if p["state"] != "sold":
        return None
    rake = CFG["rake_pct"]
    gross = (p["sell_price"] or 0) * (1 - rake)
    return round(gross - (p["buy_price"] or 0) - (p["fee_coins"] or 0))


_STAT_KEYS = ("id", "slot", "atk", "def", "arm", "wt", "elementType", "elementAtk", "range",
              "twoHanded", "ammoType", "hitChance", "skills", "absorb",
              "magicEl", "imb_slots", "imb_cats", "hpRegen", "mpRegen",
              "durationSec", "wandMin", "wandMax", "manaShot", "critDmg",
              "critChance", "moveSpeed", "lifeLeech", "manaLeech", "charges",
              "reflect", "maxHitChance", "cleave", "mShieldFlat", "mShieldPct")


def _item_stats(info):
    """Estatisticas base do item (ataque/defesa/armadura/etc) pra exibir nos
    detalhes expandidos do painel. None se nao ha nada alem do ja mostrado."""
    if not info:
        return None
    st = {k: info[k] for k in _STAT_KEYS if k in info}
    return st or None


def _gear_extras(name, key, table):
    """Raridade/atributos (da chave gravada) + ficha do item (da tabela
    estatica) - usado em posicoes/acompanhando, que so' guardam nome+chave."""
    info = table.get(name)
    return {
        "rarity": analyze.tier_from_key(key),
        "attrs_text": analyze.decode_attrs(analyze.parse_attrs(key)),
        "item_class": (info or {}).get("class"),
        "req_level": (info or {}).get("level"),
        "req_vocs": (info or {}).get("vocs"),
        "item_stats": _item_stats(info),
    }


_BM = {}     # id da busca -> (assinatura, ACTIVE_AT, ofertas, total): recalcula so' quando muda


def _bounty_matches(b, since, table):
    sig = tuple(b[k] for k in ("name", "rarities", "kinds", "vocs", "classes", "attrs"))
    hit = _BM.get(b["id"])
    if hit and hit[0] == sig and hit[1] == ACTIVE_AT:
        return hit[2], hit[3]
    matches, total = analyze.match_bounty(b, ACTIVE, STORE, CFG, since, table)
    for m in matches:
        info = table.get(m["name"]) if m["kind"] == "gear" else None
        m["stats"] = _item_stats(info)
        m["item_class"] = (info or {}).get("class")
        m["req_level"] = (info or {}).get("level")
        m["req_vocs"] = (info or {}).get("vocs")
    _BM[b["id"]] = (sig, ACTIVE_AT, matches, total)
    return matches, total


_CRF = {"sig": None, "data": None}


def _craft(now):
    """Upar pra revender (recalcula quando muda o snapshot de leiloes ou a hora)."""
    sig = (ACTIVE_AT, now // 3_600_000)
    if _CRF["sig"] != sig:
        data = analyze.craft_table(STORE, CFG, ACTIVE, now, scanner.gold_fee_coins(CFG, STORE))
        table = items.get(CFG["items_refresh_days"])
        for r in data["rows"] + data["others"]:      # ficha do item (igual ao resto do painel)
            info = table.get(r["name"]) or {}
            r.update(req_level=info.get("level"), req_vocs=info.get("vocs"),
                     item_class=info.get("class"), item_stats=_item_stats(info))
        _CRF.update(sig=sig, data=data)
    return _CRF["data"]


_STK = {"sig": None, "data": None}
_STOCK_KEYS = ("id", "name", "kind", "key", "rarity", "attrs_text", "item_class",
               "req_level", "req_vocs", "item_stats", "buy_price", "opened_at", "note")


def _stock(holding, now):
    """Estoque = o que voce ja comprou (arrematado) e ainda nao vendeu nem
    usou: quanto vale hoje, quanto sobra se vender (rake + taxa), se vale
    anunciar agora ou segurar, preco inicial e melhor hora de anunciar."""
    hours = CFG["sell_list_hours"]
    sig = (tuple(sorted((p["id"], p["buy_price"]) for p in holding)), ACTIVE_AT,
           now // 3_600_000)
    if _STK["sig"] == sig:
        return _STK["data"]
    since = now - CFG["history_days"] * 86_400_000
    rake, win_h = CFG["rake_pct"], CFG["flip_horizon_hours"]
    fee = scanner.gold_fee_coins(CFG, STORE)
    windows = analyze.supply_windows(ACTIVE)
    list_now, list_at = analyze.next_list_window(now, hours)
    out = []
    for p in holding:
        d = {k: p.get(k) for k in _STOCK_KEYS}
        d["days_held"] = round((now - (p["opened_at"] or now)) / 86_400_000, 1)
        value = trend = basis = n = None
        label = state = start = note = None
        if p["kind"] == "gold":
            gr = analyze.gold_rate(STORE, CFG)
            if gr and p["use_max"] and p["median_at_buy"]:
                value = p["use_max"] * gr["rate"] / (p["median_at_buy"] * 100)
                trend, basis = gr["trend"], "taxa"
            label, state = "gold: so' pra usar", "mid"
        else:
            gear = p["kind"] == "gear"
            nz = (analyze.nz_from_gear_key(p["key"]) if gear else
                  {"key": p["key"], "kind": p["kind"], "name": p["name"]})
            val = analyze.value_item(nz, STORE, CFG, since)
            if val:
                trend, basis, n = val.get("trend"), val["basis"], val["n"]
                value = (val["fair"] if gear else
                         (p["use_max"] * val["fair"] / p["median_at_buy"]
                          if p["use_max"] and p["median_at_buy"] else None))
                ends = windows.get(analyze.supply_key(nz), [])
                closing = sum(1 for e in ends if e - now <= win_h * 3_600_000)
                pressure = round(closing / max(val["per_day"] * win_h / 24, 0.3), 1)
                label, state = analyze.sell_verdict(val["fair"], val["per_day"],
                                                    pressure, trend, CFG)
                rows = STORE.sales_for_key(p["key"], since) if gear else []
                if len(rows) >= 5:
                    prices = sorted(r["unit_price"] for r in rows)
                    bmed = statistics.median([r["bid_count"] or 0 for r in rows])
                    start, note = analyze.start_price(
                        bmed, analyze._percentile(prices, 25), statistics.median(prices))
                elif value:
                    start, note = max(25, round(value)), "pouca venda desse item - use o valor justo"
        buy = p["buy_price"] or 0
        if value is not None:
            d["value_now"] = round(value)
            d["net_now"] = round(value * (1 - rake) - fee) if p["kind"] != "gold" else round(value)
            d["pnl"] = d["net_now"] - buy
            d["pnl_pct"] = round(d["pnl"] / buy, 3) if buy else None
        d.update(basis=basis, n=n, trend=trend, sell_label=label, sell_state=state,
                 start_price=start, start_note=note)
        out.append(d)
    rank = {"good": 0, "mid": 1, "floor": 2, "slow": 3, "hold": 4}
    out.sort(key=lambda x: (rank.get(x["sell_state"], 5), -(x["days_held"] or 0)))
    priced = [x for x in out if x.get("value_now") is not None]
    data = {"items": out, "summary": {
        "count": len(out), "cost": sum(x["buy_price"] or 0 for x in out),
        "value": sum(x["value_now"] for x in priced),
        "net": sum(x["net_now"] for x in priced),
        "pnl": sum(x["pnl"] for x in priced),
        "invested_priced": sum(x["buy_price"] or 0 for x in priced),
        "unpriced": len(out) - len(priced),
        "list_now": list_now, "list_at": list_at,
        "close_txt": f"{(min(hours) + 6) % 24}h-{(max(hours) + 6) % 24}h"}}
    _STK.update(sig=sig, data=data)
    return data


def build_appraise(aid):
    """Avaliador de item: busca o leilao (API publica, so' leitura) e decompoe
    o preco pelas especificacoes dele."""
    try:
        r = api.item(aid)
    except Exception as e:  # noqa: BLE001
        return {"error": f"não achei o leilão #{aid} ({e})"}
    if not r or r.get("type") != "item" or (r.get("item") or {}).get("qty"):
        return {"error": "o avaliador é só para equipamento"}
    nz = analyze.normalize(r)
    now = analyze.now_ms()
    ap = analyze.appraise(nz, STORE, CFG, now, scanner.gold_fee_coins(CFG, STORE))
    if not ap:
        return {"error": "sem histórico de vendas desse item e raridade pra avaliar"}
    info = items.get(CFG["items_refresh_days"]).get(nz["name"]) or {}
    return {"auction": {"id": aid, "status": r.get("status"), "price": r.get("currentPrice"),
                        "bids": r.get("bidCount"), "ends_at": r.get("endsAt"),
                        "secs_left": round(((r.get("endsAt") or now) - now) / 1000)},
            "item": {"name": nz["name"], "tier": nz["tier"], "attrs_text": analyze.decode_attrs(nz["attrs"]),
                     "req_level": info.get("level"), "req_vocs": info.get("vocs"),
                     "item_class": info.get("class"), "item_stats": _item_stats(info)},
            "appraisal": ap}


def build_state():
    now = analyze.now_ms()

    mode = STORE.get_setting("filter_mode", "all")
    chars = [dict(c) for c in STORE.list_characters()]
    active_chars = [c for c in chars if c["active"]]
    table = items.get(CFG["items_refresh_days"])

    opps = []
    rake = CFG["rake_pct"]
    for r in STORE.list_opportunities(active_only=True):
        d = dict(r)
        if mode == "chars" and (
                d["kind"] == "gold"
                or not items.fits(d["name"], active_chars, table)):
            continue
        d["secs_left"] = round(((r["ends_at"] or now) - now) / 1000)
        lot = (r["current_price"] / r["unit_price"]) if r["unit_price"] else 1
        d["breakeven"] = round((r["median"] or 0) * (1 - rake) * lot)
        if d["kind"] == "gold":
            # o que se espera PAGAR ao fechar (o preco sobe ate la) e a chance
            # de ainda sobrar >= gold_use_min_discount abaixo da taxa
            d["exp_disc"] = analyze.gold_curve(d["discount"], CFG["gold_curve_exp"])
            d["p_use"] = analyze.gold_curve(d["discount"], CFG["gold_curve_p"])
            d["exp_saving"] = round(d["exp_disc"] * (r["median"] or 0) * lot)
            d["anchor"] = analyze.is_anchor(r["unit_price"] * 100)
        info = table.get(d["name"])
        if info:
            d["req_level"] = info.get("level")
            d["req_vocs"] = info.get("vocs")
            d["item_class"] = info.get("class")
        if d["kind"] == "gear" and d.get("ftier"):
            d["forge_text"] = analyze.forge_text(
                (info or {}).get("slot"), d["ftier"])
        if d["kind"] == "gear":
            d["item_stats"] = _item_stats(info)
            d["upg"] = analyze.upgrade_econ(analyze.nz_from_gear_key(d["key"]),
                                            r["median"] or 0, STORE, CFG,
                                            now - CFG["history_days"] * 86_400_000)
        opps.append(d)

    positions = []
    for r in STORE.list_positions():
        d = dict(r)
        d["net"] = position_net(r)
        if r["auction_ends_at"]:
            d["auction_secs_left"] = round((r["auction_ends_at"] - now) / 1000)
        if d["kind"] == "gear" and d.get("key"):
            d.update(_gear_extras(d["name"], d["key"], table))
        positions.append(d)

    sold = [p for p in positions if p["state"] == "sold"]
    holding = [p for p in positions if p["state"] == "holding"]

    def realized(frm):
        return sum(p["net"] for p in sold if (p["sold_at"] or 0) >= frm and p["net"])

    initial = float(STORE.get_setting("initial_capital", 0) or 0)
    total = realized(0)
    pnl = {
        "initial_capital": initial,
        "realized_total": total,
        "realized_today": realized(_midnight_ms()),
        "realized_7d": realized(now - 7 * 86_400_000),
        "realized_30d": realized(now - 30 * 86_400_000),
        "open_cost": sum(p["buy_price"] or 0 for p in holding),
        "open_count": len(holding),
        "sold_count": len(sold),
        "roi": (total / initial) if initial else None,
        "deduct_listing_fee": STORE.get_setting("deduct_listing_fee", "1") == "1",
        "gold_fee_coins": scanner.gold_fee_coins(CFG, STORE),
        "rake_pct": CFG["rake_pct"],
    }
    watches = []
    for w in STORE.list_watch():
        d = dict(w)
        d["secs_left"] = round(((w["ends_at"] or now) - now) / 1000)
        if d["kind"] == "gear" and d.get("key"):
            d.update(_gear_extras(d["name"], d["key"], table))
        watches.append(d)

    since = now - CFG["history_days"] * 86_400_000
    bounties = []
    for b in STORE.list_bounties():
        csv = lambda k, conv=str: [conv(x) for x in (b[k] or "").split(",") if x.strip()]
        d = {"id": b["id"], "name": b["name"], "note": b["note"],
             "active": b["active"], "added_at": b["added_at"],
             "last_hit_at": b["last_hit_at"],
             "rarities": csv("rarities", int), "kinds": csv("kinds"),
             "vocs": csv("vocs"), "classes": csv("classes", int),
             "attrs": csv("attrs", int), "matches": [], "total": 0}
        if b["active"] and ACTIVE:
            d["matches"], d["total"] = _bounty_matches(b, since, table)
        d["match_count"] = d["total"]
        d["good_count"] = sum(1 for m in d["matches"] if m["good"])
        bounties.append(d)

    return {
        "opportunities": opps,
        "positions": positions,
        "watches": watches,
        "bounties": bounties,
        "gold_market": _gold_market(),
        "craft": _craft(now),
        "boss": analyze.boss_monitor(STORE, CFG, ACTIVE, now),
        "attr_values": analyze.attr_multipliers(STORE, CFG),
        "stock": _stock([p for p in positions if p["state"] == "holding"], now),
        "pnl": pnl,
        "characters": [dict(c) for c in chars],
        "meta": {
            "now": now,
            "last_scan": int(STORE.get_setting("last_scan", 0) or 0),
            "poll_seconds": CFG["poll_seconds"],
            "track_poll_seconds": CFG["track_poll_seconds"],
            "budget_max": CFG["budget_max_coins"],
            "filter_mode": mode,
            "vocations": VOCATIONS,
            "items_count": len(table),
            "flash_min_rarity": CFG["flash_min_rarity"],
            "flash_min_gap_coins": CFG["flash_min_gap_coins"],
            "flash_max_seconds": CFG["flash_max_seconds"],
            "watch_alert_seconds": CFG["watch_alert_seconds"],
            "gold_use_min_discount": CFG["gold_use_min_discount"],
            "attr_names": analyze.ATTR_NAME,
        },
    }


# ---------------------------------------------------------------- actions
def act_create_position(body):
    """Cria uma posicao (Dar lance). Se o leilao ja esta na tabela de
    oportunidades (varredura normal), usa o preco justo ja calculado ali. Se
    nao (ex.: achado em "Procurar por oferta", que nao passa pela tabela),
    busca o leilao ao vivo e avalia na hora - mesmo mecanismo do Acompanhando."""
    oid = int(body["auction_id"])
    row = next((o for o in STORE.list_opportunities() if o["auction_id"] == oid), None)
    if row:
        o = dict(row)
    else:
        try:
            live = api.item(oid)
        except Exception as e:  # noqa: BLE001
            return {"error": f"leilão #{oid}: {e}"}, 404
        if live.get("status") != "active":
            return {"error": f"leilão #{oid} não está mais ativo ({live.get('status')})"}, 400
        nz = analyze.normalize(live)
        table = items.get(CFG["items_refresh_days"])
        val = analyze.value_for_watch(nz, STORE, CFG, table)
        fair = val["fair"] if val else live["currentPrice"]
        thr = CFG["gold_discount_threshold"] if nz["kind"] == "gold" else CFG["discount_threshold"]
        o = {"auction_id": oid, "key": nz["key"], "name": nz["name"], "kind": nz["kind"],
             "unit_price": nz["unit_price"], "current_price": live["currentPrice"],
             "median": fair, "suggested_max": round(fair * (1 - thr)) if val else None,
             "bid_count": live.get("bidCount", 0), "ends_at": live["endsAt"]}
    max_bid = int(body.get("max_bid") or o["suggested_max"] or o["current_price"])
    STORE.create_position(o, max_bid, note=body.get("note", ""))
    STORE.dismiss_opportunity(oid)
    return {"ok": True}, 200


def act_update_position(pid, body):
    p = STORE.get_position(pid)
    if not p:
        return {"error": "posicao nao encontrada"}, 404
    action = body.get("action")
    now = analyze.now_ms()
    if action == "won":
        price = int(body.get("buy_price") or p["last_auction_price"] or p["max_bid"] or 0)
        STORE.update_position(pid, state="holding", auction_status="won", buy_price=price)
    elif action == "lost":
        STORE.update_position(pid, state="lost", auction_status="lost")
    elif action == "sold":
        STORE.update_position(pid, state="sold", sell_price=int(body["sell_price"]),
                              sold_at=now, fee_coins=scanner.gold_fee_coins(CFG, STORE))
    elif action == "used":            # ficou pra uso proprio: sai do estoque, sem venda
        STORE.update_position(pid, state="used", sold_at=now)
    elif action == "reopen":
        STORE.update_position(
            pid, state="holding" if p["state"] in ("sold", "used") else "bidding",
            sell_price=None, sold_at=None)
    elif action == "edit":
        fields = {k: body[k] for k in ("buy_price", "sell_price", "max_bid", "note")
                  if k in body}
        STORE.update_position(pid, **fields)
    else:
        return {"error": "acao invalida"}, 400
    return {"ok": True}, 200


def build_history(days, page=0, per=10, flash_only=False):
    since = analyze.now_ms() - days * 86_400_000
    rake = CFG["rake_pct"]
    table = items.get(CFG["items_refresh_days"])
    z = lambda: {"n": 0, "flip": 0, "missed": 0, "profit": 0, "cost": 0}
    summ = {"detected": 0, "flip": 0, "use": 0, "expensive": 0,
            "expired": 0, "floor": 0, "open": 0, "acted": 0,
            "flip_profit": 0, "flip_missed": 0, "flip_cost": 0, "flash": z(),
            # gold e' so' pra USO: separado do flip (sem rake, "lucro" = quanto
            # abaixo da taxa fechou)
            "gold": {"good": 0, "ok": 0, "expensive": 0, "missed": 0,
                     "saving": 0, "cost": 0}}
    rows = []
    for r in STORE.list_opp_log(since):
        d = dict(r)
        info = table.get(r["name"])
        if r["kind"] == "gear" and r["ftier"]:
            d["forge_text"] = analyze.forge_text((info or {}).get("slot"), r["ftier"])
        d["item_class"] = (info or {}).get("class")
        if r["kind"] == "gear":
            d["item_stats"] = _item_stats(info)
            d["req_level"] = (info or {}).get("level")
            d["req_vocs"] = (info or {}).get("vocs")
        is_flash = bool(r["flash"])
        summ["detected"] += 1
        if is_flash:
            summ["flash"]["n"] += 1
        if r["acted"]:
            summ["acted"] += 1
        if r["closed_at"] is None:
            d["hypo_profit"] = None
            summ["open"] += 1
        else:
            oc = r["outcome"]
            fair_lot = r["fair_lot"] or r["fair"] or 0
            entry = (r["final_price"] + 1) if r["final_price"] is not None else None
            is_gold = r["kind"] == "gold" and oc in ("flip", "use", "expensive")
            if not is_gold:
                summ[oc] = summ.get(oc, 0) + 1
            # pra ganhar o leilao voce paga 1 acima de quem arrematou
            if r["kind"] == "gold":
                hp = round(fair_lot - entry) if entry is not None else None
            else:
                hp = round(fair_lot * (1 - rake) - entry) if entry is not None else None
            d["hypo_profit"] = hp
            if is_gold:
                g = summ["gold"]
                g[{"flip": "good", "use": "ok", "expensive": "expensive"}[oc]] += 1
                if oc == "flip" and not r["acted"] and hp and hp > 0:
                    g["missed"] += 1
                    g["saving"] += hp
                    g["cost"] += entry
            elif oc == "flip":
                if is_flash:
                    summ["flash"]["flip"] += 1
                if not r["acted"] and hp and hp > 0:
                    summ["flip_profit"] += hp
                    summ["flip_missed"] += 1
                    summ["flip_cost"] += entry
                    if is_flash:
                        summ["flash"]["missed"] += 1
                        summ["flash"]["profit"] += hp
                        summ["flash"]["cost"] += entry
        if not flash_only or is_flash:
            rows.append(d)
    total = len(rows)
    page = max(0, min(page, (total - 1) // per if total else 0))
    return {"rows": rows[page * per:(page + 1) * per], "summary": summ,
            "days": days, "total": total, "page": page, "per": per,
            "flash_only": flash_only}


def build_lookup(q, days):
    """Consulta de venda: quanto um item costuma vender e preco inicial."""
    q = q.strip()
    if len(q) < 2:
        return {"query": q, "days": days, "groups": []}
    since = analyze.now_ms() - days * 86_400_000
    rows = STORE.sales_for_name(f"%{q}%", since)
    rake = CFG["rake_pct"]
    fee = scanner.gold_fee_coins(CFG, STORE)
    windows = analyze.supply_windows(ACTIVE)       # concorrencia + quando fecha
    now = analyze.now_ms()
    table = items.get(CFG["items_refresh_days"])

    groups = {}
    for r in rows:
        band = r["key"].split(":")[-1] if r["kind"] == "gold" else ""
        gk = (r["name"], r["kind"], r["tier"], r["up_level"], r["ftier"], band)
        groups.setdefault(gk, []).append(r)

    out = []
    for (name, kind, tier, up, ftier, band), rs in groups.items():
        prices = sorted(x["final_price"] for x in rs)
        units = sorted(x["unit_price"] for x in rs if x["unit_price"] is not None)
        bids = [x["bid_count"] or 0 for x in rs]
        qtys = sorted(x["qty"] for x in rs if x["qty"])
        times = [x["ends_at"] for x in rs if x["ends_at"]]
        span_days = max((max(times) - min(times)) / 86_400_000, 1.0) if times else 1.0

        bmed = statistics.median(bids) if bids else 0
        pmed = statistics.median(prices)
        p25 = analyze._percentile(prices, 25)
        _cut = now - 2 * 86_400_000
        rec = [x["final_price"] for x in rs if x["ends_at"] and x["ends_at"] >= _cut]
        old = [x["final_price"] for x in rs if x["ends_at"] and x["ends_at"] < _cut]
        trend = (round(statistics.median(rec) / statistics.median(old) - 1, 3)
                 if len(rec) >= CFG["trend_min_recent"] and old else None)
        start, note = analyze.start_price(bmed, p25, pmed)

        per_day = round(len(rs) / span_days, 2)
        sk = (f"g:{name}|t{tier}" if kind == "gear"
              else f"stack:{name}" if kind == "stack" else f"gold:{band}")
        ends = windows.get(sk, [])
        active = len(ends)
        win_h = CFG["flip_horizon_hours"]
        closing_win = sum(1 for e in ends if e - now <= win_h * 3_600_000)
        demand_win = per_day * win_h / 24
        pressure = round(closing_win / max(demand_win, 0.3), 1)
        closing_6h = sum(1 for e in ends if e - now <= 6 * 3_600_000)

        # veredito de VENDA: vale listar agora ou segurar?
        sell = analyze.sell_verdict(pmed, per_day, pressure, trend, CFG)

        info = table.get(name) if kind == "gear" else None
        out.append({
            "name": name, "kind": kind, "tier": tier, "up_level": up, "ftier": ftier,
            "band": band, "n": len(rs), "per_day": per_day, "trend": trend,
            "sell_label": sell[0], "sell_state": sell[1],
            "item_class": (info or {}).get("class"),
            "req_level": (info or {}).get("level"),
            "req_vocs": (info or {}).get("vocs"),
            "item_stats": _item_stats(info),
            "active": active, "supply_pressure": pressure, "closing_6h": closing_6h,
            "days_supply": round(active / max(per_day, 0.1), 1),
            "price_median": pmed, "price_p25": p25,
            "price_p75": analyze._percentile(prices, 75),
            "price_min": prices[0], "price_max": prices[-1],
            "unit_median": statistics.median(units) if units else None,
            "bids_median": bmed,
            "qty_median": statistics.median(qtys) if qtys else None,
            "suggested_start": start, "start_note": note,
            "net_at_median": round(pmed * (1 - rake) - fee),
            "recent": [{"price": x["final_price"], "qty": x["qty"],
                        "bids": x["bid_count"], "ends_at": x["ends_at"]}
                       for x in rs[:8]],
        })
    out.sort(key=lambda g: -g["n"])
    return {"query": q, "days": days, "groups": out}


def build_watch_search(q):
    q = q.strip().lower()
    if len(q) < 2:
        return {"results": [], "q": q}
    table = items.get(CFG["items_refresh_days"])
    out = []
    for row in ACTIVE:
        it = row.get("item") or {}
        nm = it.get("name") or ("gold" if row["type"] == "gold" else "")
        if q not in nm.lower():
            continue
        nz = analyze.normalize(row)
        is_gear = nz["kind"] == "gear"
        info = table.get(nm) if is_gear else None
        out.append({
            "auction_id": row["id"], "name": nm, "kind": nz["kind"],
            "current_price": row["currentPrice"], "bid_count": row.get("bidCount", 0),
            "ends_at": row["endsAt"], "qty": it.get("qty"),
            "rarity": it.get("tier") if is_gear else None,
            "gold_millions": round((row.get("goldAmount") or 0) / 1e6) or None,
            "attrs_text": analyze.decode_attrs(nz["attrs"]) if is_gear else None,
            "item_class": (info or {}).get("class"),
            "req_level": (info or {}).get("level"),
            "req_vocs": (info or {}).get("vocs"),
            "item_stats": _item_stats(info),
        })
    out.sort(key=lambda x: x["ends_at"])
    return {"results": out[:40], "q": q}


def act_add_watch(body):
    aid = int(body["auction_id"])
    try:
        row = api.item(aid)
    except Exception as e:  # noqa: BLE001
        return {"error": f"leilão #{aid}: {e}"}, 404
    if row.get("status") != "active":
        return {"error": f"leilão #{aid} não está mais ativo ({row.get('status')})"}, 400
    nz = analyze.normalize(row)
    table = items.get(CFG["items_refresh_days"])
    val = analyze.value_for_watch(nz, STORE, CFG, table)
    lot = (row["currentPrice"] / nz["unit_price"]) if nz["unit_price"] else 1
    fair = val["fair"] if val else None
    thr = (CFG["gold_discount_threshold"] if nz["kind"] == "gold"
           else CFG["discount_threshold"])
    STORE.add_watch({
        "auction_id": aid, "name": nz["name"], "kind": nz["kind"], "key": nz["key"],
        "note": body.get("note", ""), "fair": fair,
        "fair_lot": round(fair * lot) if fair else None,
        "suggested_max": round(fair * (1 - thr) * lot) if fair else None,
        "est": int(val["est"]) if val else 0, "basis": val["basis"] if val else None,
        "n": val["n"] if val else None,
        "current_price": row["currentPrice"], "bid_count": row.get("bidCount", 0),
        "ends_at": row["endsAt"],
    })
    return {"ok": True}, 200


BOUNTY_KINDS = ("weapon1h", "weapon2h", "helmet", "armor", "legs", "boots",
                "shield", "amulet", "ring", "trinket")


def act_add_bounty(body):
    def pick(key, ok, conv=str):
        return ",".join(str(conv(x)) for x in (body.get(key) or []) if conv(x) in ok)

    name = (body.get("name") or "").strip()
    f = {"rarities": pick("rarities", range(6), int),
         "kinds": pick("kinds", BOUNTY_KINDS), "vocs": pick("vocs", VOCATIONS),
         "classes": pick("classes", range(1, 5), int),
         "attrs": pick("attrs", analyze.ATTR_NAME, int)}
    if len(name) == 1 or not (name or any(f.values())):
        return {"error": "informe um nome (2+ letras) ou algum filtro"}, 400
    STORE.add_bounty(name.lower(), f["rarities"], (body.get("note") or "").strip(),
                     f["kinds"], f["vocs"], f["classes"], f["attrs"])
    return {"ok": True}, 200


def act_settings(body):
    if "initial_capital" in body:
        STORE.set_setting("initial_capital", float(body["initial_capital"]))
    if "deduct_listing_fee" in body:
        STORE.set_setting("deduct_listing_fee", "1" if body["deduct_listing_fee"] else "0")
    if "filter_mode" in body:
        STORE.set_setting("filter_mode",
                          "chars" if body["filter_mode"] == "chars" else "all")
    return {"ok": True}, 200


def act_character(body):
    STORE.add_character(body["name"].strip(), body["vocation"], body.get("level", 0))
    return {"ok": True}, 200


def act_update_character(cid, body):
    fields = {}
    for k in ("name", "vocation", "level", "active"):
        if k in body:
            fields[k] = int(body[k]) if k in ("level", "active") else body[k]
    STORE.update_character(cid, **fields)
    return {"ok": True}, 200


# ---------------------------------------------------------------- http
DASHBOARD = (HERE / "dashboard.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, status=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass          # o navegador fechou/recarregou no meio da resposta: sem problema

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._send(DASHBOARD.encode(), ctype="text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(build_state())
        if path == "/api/appraise":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                aid = int((q.get("id") or [""])[0])
            except ValueError:
                return self._send({"error": "informe o número do leilão"})
            return self._send(build_appraise(aid))
        if path == "/api/history":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            days = max(1, min(90, int((q.get("days") or ["7"])[0])))
            page = max(0, int((q.get("page") or ["0"])[0]))
            flash = (q.get("flash") or ["0"])[0] == "1"
            return self._send(build_history(days, page, flash_only=flash))
        if path == "/api/lookup":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            days = max(1, min(90, int((q.get("days") or ["7"])[0])))
            return self._send(build_lookup((q.get("q") or [""])[0], days))
        if path == "/api/watch/search":
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            return self._send(build_watch_search((q.get("q") or [""])[0]))
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/position":
                return self._send(*act_create_position(body))
            if path == "/api/settings":
                return self._send(*act_settings(body))
            if path == "/api/scan":
                threading.Thread(target=lambda: scanner.run_scan(CFG, STORE),
                                 daemon=True).start()
                return self._send({"ok": True})
            if path == "/api/character":
                return self._send(*act_character(body))
            if path == "/api/watch":
                return self._send(*act_add_watch(body))
            if path == "/api/bounty":
                return self._send(*act_add_bounty(body))
            if path.startswith("/api/opportunity/") and path.endswith("/dismiss"):
                STORE.dismiss_opportunity(int(path.split("/")[3]))
                return self._send({"ok": True})
        except Exception as e:  # noqa: BLE001
            return self._send({"error": str(e)}, 400)
        self._send({"error": "not found"}, 404)

    def do_PATCH(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/position/"):
                return self._send(*act_update_position(int(path.split("/")[3]), self._body()))
            if path.startswith("/api/character/"):
                return self._send(*act_update_character(int(path.split("/")[3]), self._body()))
            if path.startswith("/api/bounty/"):
                b = self._body()
                STORE.update_bounty(int(path.split("/")[3]), active=int(b.get("active", 1)))
                return self._send({"ok": True})
        except Exception as e:  # noqa: BLE001
            return self._send({"error": str(e)}, 400)
        self._send({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/position/"):
            STORE.delete_position(int(path.split("/")[3]))
            return self._send({"ok": True})
        if path.startswith("/api/character/"):
            STORE.delete_character(int(path.split("/")[3]))
            return self._send({"ok": True})
        if path.startswith("/api/watch/"):
            STORE.delete_watch(int(path.split("/")[3]))
            return self._send({"ok": True})
        if path.startswith("/api/bounty/"):
            STORE.delete_bounty(int(path.split("/")[3]))
            return self._send({"ok": True})
        self._send({"error": "not found"}, 404)


# ---------------------------------------------------------------- poller
def poller():
    last_scan = 0.0
    items.get(CFG["items_refresh_days"])          # aquece a tabela de itens
    first = STORE.get_setting("backfilled") != "1"
    if first:
        print("[poller] backfill inicial (~7 dias de historico), pode levar 1 min...")
    while True:
        try:
            now = time.time()
            if now - last_scan >= CFG["poll_seconds"]:
                global ACTIVE, ACTIVE_AT
                ACTIVE = scanner.collect(CFG, STORE)
                ACTIVE_AT = int(now * 1000)
                opps = scanner.run_scan(CFG, STORE, active=ACTIVE)
                scanner.refresh_bounties(CFG, STORE, ACTIVE)
                last_scan = now
                STORE.set_setting("last_scan", int(now * 1000))
                print(f"[scan] {len(opps)} oportunidades | "
                      f"{time.strftime('%H:%M:%S')}")
            scanner.refresh_opportunities(CFG, STORE)     # preco ao vivo do que monitora
            scanner.refresh_watches(CFG, STORE)           # itens acompanhados pelo id
            if STORE.open_auction_positions():
                scanner.refresh_positions(CFG, STORE)
        except Exception as e:  # noqa: BLE001
            print(f"[poller] erro: {e}")
        time.sleep(CFG["track_poll_seconds"])


def _find_chrome_executable():
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium-browser"]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def open_dashboard(url):
    """Abre o painel numa janela de 'app' dedicada (sem abas, sem barra de
    extensoes) com um perfil proprio (Store/market/chrome_profile) - separado
    tanto do perfil pessoal do usuario quanto do perfil do bot do jogo. Sem
    isso, 'webbrowser.open()' abria no Chrome padrao do sistema (perfil
    pessoal, com todas as extensoes instaladas carregando junto) so' pra
    mostrar uma pagininha local - processos e memoria gastos a toa.
    Cai pra 'webbrowser.open()' se nao achar o Chrome instalado nos locais
    usuais."""
    chrome_path = _find_chrome_executable()
    if not chrome_path:
        webbrowser.open(url)
        return
    # BAIAK_MARKET_PROFILE_DIR: usado pelo gui.py quando o app esta instalado
    # num lugar sem permissao de escrita (ex: /Applications no Mac) - sem
    # isso, essa pasta tentaria se criar do lado do proprio server.py, no
    # mesmo local protegido, e falharia silenciosamente. Vazio (uso normal,
    # fora do build distribuido) mantem o comportamento de sempre.
    base = os.environ.get("BAIAK_MARKET_PROFILE_DIR") or str(Path(__file__).parent / "chrome_profile")
    profile_dir = Path(base)
    profile_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            chrome_path,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )


class _SingleInstanceHTTPServer(ThreadingHTTPServer):
    """Igual ThreadingHTTPServer, mas com allow_reuse_address desligado.

    HTTPServer liga isso por padrao (SO_REUSEADDR), e no Windows isso deixa
    2 processos diferentes 'bind' na MESMA porta ao mesmo tempo sem erro
    nenhum (SO_REUSEADDR no Windows e' bem mais permissivo que no Linux/Mac)
    - foi exatamente isso que deixava 2 janelas do bot abrirem 2 pollers
    escrevendo no mesmo market.db ('database is locked'). Desligando, a 2a
    tentativa de bind volta a falhar com OSError de verdade, como deveria."""

    allow_reuse_address = False


def start_server(port=None, open_browser=False):
    """Inicia o poller e o servidor HTTP, os dois em threads daemon - versao
    reutilizavel de 'main()' pra ser chamada de dentro de outro processo (ex:
    gui.py do bot, pra rodar tudo junto) sem depender de argparse nem
    bloquear a thread de quem chama. Retorna (srv, url); 'srv.shutdown()'
    para o servidor HTTP (o poller, sendo daemon, morre junto quando o
    processo encerra).

    Liga a porta ANTES de tocar no banco/poller de proposito: com 2 janelas
    do bot abertas (ver 'Nova Janela'), a 2a chamada aqui deve falhar bem
    cedo (porta ja em uso pela 1a) sem nunca chegar a criar um 2o poller -
    2 pollers de processos diferentes escrevendo no mesmo market.db ao
    mesmo tempo e' o que causava 'database is locked' o tempo todo."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    actual_port = port or CFG["http_port"]
    srv = _SingleInstanceHTTPServer(("127.0.0.1", actual_port), Handler)

    _backup_db(CFG["db_path"])
    threading.Thread(target=poller, daemon=True).start()
    url = f"http://127.0.0.1:{actual_port}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[server] {url}")
    if open_browser:
        threading.Timer(1.0, lambda: open_dashboard(url)).start()
    return srv, url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--port", type=int, default=CFG["http_port"])
    args = ap.parse_args()

    srv, url = start_server(port=args.port, open_browser=not args.no_open)
    print(f"[server] {url}  (Ctrl+C para parar)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[server] encerrado")


if __name__ == "__main__":
    main()
