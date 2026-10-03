"""Codex da conta x market.

O bot (que tem a conexao com o jogo) le o Codex da conta - cada entrada com a
recompensa e os requisitos 'tem/precisa' - e grava 'codex_progress.json' ao
lado do banco (ver bot.read_codex_progress). Aqui o market so' LE esse arquivo
e cruza o que FALTA entregar com os leiloes de empilhaveis ativos e o
historico de preco: quanto custa fechar cada entrada comprando no market e
quais lotes valem a pena agora. Sem o arquivo (market rodando sozinho, sem o
bot) a secao mostra so' o aviso."""
import json
import os
import statistics
import threading
import time

import analyze

# mesma lista do filtro nativo do Codex (bot.CODEX_ATTRIBUTES)
ATTRS = [
    ("critDmg", "Dano crítico"), ("onslaught", "Onslaught"), ("critChance", "Chance de crítico"),
    ("atkPct", "Ataque"), ("spellDmgPct", "Dano de magia"), ("elementDmgPct", "Dano elemental"),
    ("hpPct", "Vida"), ("manaPct", "Mana"), ("armorFlat", "Armadura"), ("defFlat", "Defesa"),
    ("absorbPct", "Resistência elemental"), ("lifeLeech", "Roubo de vida"),
    ("manaLeech", "Roubo de mana"), ("spellHealPct", "Cura de magia"),
    ("moveSpeed", "Velocidade de movimento"),
]
ATTR_LABEL = dict(ATTRS)

# ordenacoes aceitas (a tela escolhe; ver view)
ENTRY_SORTS = ("easy", "complete", "missing", "cost_asc", "cost_desc")
ITEM_SORTS = ("best", "serves", "cost_asc", "missing")

# pedido de atualizacao feito pelo painel: quem embute o market (o GUI do bot)
# registra aqui a funcao que manda o bot reler o Codex; rodando sozinho fica None.
_REFRESHER = {"fn": None, "running": False, "last_error": None}


def set_refresher(fn):
    _REFRESHER["fn"] = fn


def can_refresh():
    return _REFRESHER["fn"] is not None


def refreshing():
    return _REFRESHER["running"]


def request_refresh():
    """Dispara a releitura do Codex numa thread (o painel so' volta a ver o
    resultado pelo arquivo). Retorna (ok, mensagem)."""
    fn = _REFRESHER["fn"]
    if fn is None:
        return False, "só funciona com o market aberto pelo bot (ele que lê o Codex no jogo)"
    if _REFRESHER["running"]:
        return True, "já está lendo o Codex"

    def run():
        _REFRESHER["running"] = True
        _REFRESHER["last_error"] = None
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            _REFRESHER["last_error"] = str(e)
        finally:
            _REFRESHER["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True, "lendo o Codex no jogo..."


def progress_path(cfg):
    return os.path.join(os.path.dirname(os.path.abspath(cfg["db_path"])), "codex_progress.json")


def load_progress(cfg):
    """O que o bot leu (ou None). 'mtime' entra pro cache saber que mudou."""
    path = progress_path(cfg)
    try:
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    data["_mtime"] = mtime
    return data


def _norm(name):
    return (name or "").strip().lower()


def _lots_index(active_rows, now, alert_secs):
    """{nome: [lote]} so' dos leiloes de empilhavel (item com 'qty')."""
    idx = {}
    for row in active_rows or []:
        it = row.get("item") or {}
        qty = it.get("qty")
        if row.get("type") != "item" or not qty:
            continue
        secs = (row["endsAt"] - now) / 1000
        if secs <= 0:
            continue
        idx.setdefault(_norm(it.get("name")), []).append({
            "auction_id": row["id"], "qty": qty, "price": row["currentPrice"],
            "unit": row["currentPrice"] / qty, "bids": row.get("bidCount", 0),
            "ends_at": row["endsAt"], "secs_left": round(secs),
            "early": secs > alert_secs,        # leilao novo comeca no piso: o preco ainda sobe
        })
    return idx


def _lots_for(lots, fair):
    """Lotes ordenados pelo custo estimado por unidade. Todo leilao tem piso de
    25 coins e comeca no piso, entao 'quanto custa de verdade' nao e' unidade x
    quantidade: leilao ainda cedo usa o que um comprador tipico paga (preco
    justo x qtd, nunca abaixo do preco de agora); perto do fim o preco ja e' real."""
    out = []
    for l in lots:
        est = l["price"]
        if l["early"] and fair is not None:
            est = max(l["price"], round(fair * l["qty"]))
        out.append({**l, "est_cost": est, "est_unit": est / l["qty"]})
    out.sort(key=lambda l: l["est_unit"])
    return out


def _cover(lots, fair, missing):
    """Compra gulosa pelos lotes mais baratos ate cobrir o que falta. O que
    sobrar sem lote hoje entra pelo preco justo. Retorna (custo ou None,
    cobre tudo?, lotes usados)."""
    got = cost = used = 0
    for l in lots:
        if got >= missing:
            break
        got += l["qty"]
        cost += l["est_cost"]
        used += 1
    rest = max(0, missing - got)
    if rest and fair is not None:
        cost += round(rest * fair)
    return (cost if (rest == 0 or fair is not None) else None), rest == 0, used


def _serves(qty, entry_missings):
    """Nomes das entradas (as de menor necessidade primeiro) que um lote de
    'qty' unidades atende POR INTEIRO, somando o que cada uma precisa."""
    total, names = 0, []
    for missing, name in entry_missings:
        if total + missing > qty:
            break
        total += missing
        names.append(name)
    return names


def view(store, cfg, active_rows, now, progress, prio=(), include_locked=False,
         sort_entries="easy", sort_items="best"):
    """Entradas abertas do Codex (filtradas pelos efeitos escolhidos) + o que
    comprar no market pra fechar. Um mesmo item costuma faltar em varias
    entradas: comprar um lote grande atende todas de uma vez, entao o custo de
    cada entrada e' RATEADO pelo plano conjunto (cobrir o que falta no total
    pelos lotes mais baratos), e cada lote mostra quantas entradas atende."""
    if not progress:
        return {"available": False}
    since = now - cfg["codex_days"] * 86_400_000
    thr = cfg["discount_threshold"]
    lots_by_item = _lots_index(active_rows, now, cfg["alert_window_seconds"])
    prio = [p for p in prio if p in ATTR_LABEL]
    sort_entries = sort_entries if sort_entries in ENTRY_SORTS else "easy"
    sort_items = sort_items if sort_items in ITEM_SORTS else "best"

    price_cache = {}

    def price(item):
        """(preco justo por unidade, n vendas, vendas/dia) - None sem historico."""
        if item not in price_cache:
            st = analyze.stats(store.sales_for_key(f"stack:{item}", since), now)
            if st and st["n"] >= cfg["min_sales_loose"]:
                fair, _trend = analyze._fair_with_trend(st, cfg)
                price_cache[item] = (fair, st["n"], st["per_day"])
            else:
                price_cache[item] = (None, st["n"] if st else 0, 0.0)
        return price_cache[item]

    entries_all = progress["entries"]
    open_entries = [e for e in entries_all if e.get("status") == "open"]
    locked_entries = [e for e in entries_all if e.get("status") == "locked"]
    done_n = sum(1 for e in entries_all if e.get("status") == "done")
    pool = open_entries + (locked_entries if include_locked else [])

    # 1) entradas escolhidas e o que falta de cada item
    chosen = []
    for e in pool:
        hit = [k for k in (e.get("bonuses") or {}) if k in prio]
        if prio and not hit:
            continue
        reqs = []
        for r in e.get("reqs") or []:
            need, have, ready = int(r.get("need") or 0), int(r.get("have") or 0), int(r.get("ready") or 0)
            missing = max(0, need - have - ready)
            if missing:
                reqs.append({"item": _norm(r.get("item")), "need": need, "have": have,
                             "ready": ready, "missing": missing})
        chosen.append({
            "cat": e.get("cat"), "name": e.get("name"), "status": e.get("status"),
            "progress": e.get("progress", 0), "bonus_text": e.get("bonus_text", ""),
            "prio_hit": [ATTR_LABEL[k] for k in hit], "unlock_cost": e.get("unlock_cost") or 0,
            "reqs": reqs, "deliver_now": not reqs,
        })

    # 2) por item: quanto falta no TOTAL, em quais entradas e o plano conjunto
    agg = {}
    for e in chosen:
        for r in e["reqs"]:
            a = agg.setdefault(r["item"], {"missing": 0, "entries": []})
            a["missing"] += r["missing"]
            a["entries"].append((r["missing"], e["name"]))
    plan = {}
    for item, a in agg.items():
        a["entries"].sort()
        fair, n, per_day = price(item)
        lots = _lots_for(lots_by_item.get(item, []), fair)
        cost, covered, used = _cover(lots, fair, a["missing"])
        plan[item] = {"fair": fair, "n": n, "per_day": per_day, "lots": lots,
                      "cost": cost, "covered": covered, "used": used}

    # 3) custo de cada entrada: sozinha (so' o que ela precisa) e rateado (parte
    #    dela no plano conjunto - o lote grande que atende varias sai mais barato)
    for e in chosen:
        for r in e["reqs"]:
            p, a = plan[r["item"]], agg[r["item"]]
            solo, covered, used = _cover(p["lots"], p["fair"], r["missing"])
            share = p["cost"] * r["missing"] / a["missing"] if p["cost"] is not None else None
            r.update(fair=p["fair"], n=p["n"], lots_n=len(p["lots"]), covered=covered,
                     lots_used=used, best_unit=min((l["unit"] for l in p["lots"]), default=None),
                     cost_solo=solo, cost=None if share is None else round(share),
                     shared_with=len(a["entries"]) - 1)
        rq = e["reqs"]
        e.update(cost=sum(r["cost"] for r in rq if r["cost"] is not None),
                 cost_solo=sum(r["cost_solo"] for r in rq if r["cost_solo"] is not None),
                 unpriced=sum(1 for r in rq if r["cost"] is None),
                 in_market=sum(1 for r in rq if r["lots_n"]),
                 covered_all=all(r["covered"] for r in rq),
                 missing_units=sum(r["missing"] for r in rq),
                 shared_n=sum(1 for r in rq if r["shared_with"] > 0))

    entry_keys = {
        # o que ja da pra entregar, depois o que da pra comprar tudo nos lotes de hoje
        "easy": lambda x: (not x["deliver_now"], x["unpriced"] > 0, not x["covered_all"], x["cost"], -x["progress"]),
        "complete": lambda x: (-x["progress"], x["unpriced"] > 0, x["cost"]),         # mais completas
        "missing": lambda x: (x["missing_units"], x["unpriced"] > 0, x["cost"]),      # menos unidades faltando
        "cost_asc": lambda x: (x["unpriced"] > 0, x["cost"], -x["progress"]),
        "cost_desc": lambda x: (x["unpriced"] > 0, -x["cost"], -x["progress"]),
    }
    chosen.sort(key=entry_keys[sort_entries])

    # 4) lista de compras (por item)
    units_cache = {}

    def units_per_day(item):
        if item not in units_cache:
            units_cache[item] = store.stack_units_sold(f"stack:{item}", since) / max(1, cfg["codex_days"])
        return units_cache[item]

    items = []
    for item, a in agg.items():
        p = plan[item]
        fair = p["fair"]
        lots = []
        for l in p["lots"]:
            names = _serves(l["qty"], a["entries"])
            disc = (1 - l["unit"] / fair) if fair else None
            lots.append({**l, "discount": disc, "covers": min(1.0, l["qty"] / a["missing"]),
                         "good": bool(fair and disc is not None and disc >= thr),
                         "serves_n": len(names), "serves": names[:3],
                         "per_entry": round(l["est_cost"] / len(names)) if names else None})
        lots.sort(key=lambda l: (not l["good"], l["ends_at"] if l["good"] else l["est_unit"]))
        good = [l for l in lots if l["good"]]
        saving = max(((fair - l["unit"]) * min(l["qty"], a["missing"]) for l in good), default=0) if fair else 0
        supply = sum(l["qty"] for l in p["lots"])
        items.append({
            "item": item, "missing": a["missing"], "entries_n": len(a["entries"]),
            "entries": [nm for _, nm in a["entries"][:3]],
            "entry_list": [{"name": nm, "missing": m} for m, nm in a["entries"][:8]],
            "fair": fair, "n": p["n"], "per_day": round(p["per_day"], 1),
            "units_per_day": round(units_per_day(item)),
            "supply_qty": supply, "supply_n": len(p["lots"]), "supply_cover": supply / a["missing"],
            "plan_cost": p["cost"], "plan_covered": p["covered"], "plan_lots": p["used"],
            "cost_per_entry": round(p["cost"] / len(a["entries"])) if p["cost"] is not None else None,
            "lots": lots[:3], "lots_n": len(lots), "good_n": len(good),
            "saving": round(saving), "value": round(a["missing"] * fair) if fair else 0})
    inf = float("inf")
    item_keys = {
        "best": lambda x: (x["good_n"] == 0, -x["saving"], x["lots_n"] == 0, -x["value"]),
        "serves": lambda x: (-x["entries_n"], x["plan_cost"] if x["plan_cost"] is not None else inf),
        # so' custo de verdade: quem tem lote a venda vem antes de quem so' tem preco teorico
        "cost_asc": lambda x: (x["plan_cost"] is None, x["lots_n"] == 0, not x["plan_covered"], x["plan_cost"] or 0),
        "missing": lambda x: (-x["missing"],),
    }
    items.sort(key=item_keys[sort_items])

    return {
        "available": True, "read_at": progress.get("read_at"), "source": progress.get("source"),
        "prio": prio, "include_locked": include_locked,
        "sort_entries": sort_entries, "sort_items": sort_items,
        "summary": {"total": len(entries_all), "done": done_n, "open": len(open_entries),
                    "locked": len(locked_entries), "considered": len(chosen),
                    "deliver_now": sum(1 for e in chosen if e["deliver_now"]),
                    "items": len(items), "items_with_lots": sum(1 for i in items if i["lots_n"]),
                    "shared_items": sum(1 for i in items if i["entries_n"] > 1),
                    "good_lots": sum(i["good_n"] for i in items)},
        "entries": chosen[:cfg["codex_max_entries"]], "items": items[:cfg["codex_max_items"]],
    }
