"""Normalizacao de precos, avaliacao por raridade/atributos e deteccao de
oportunidades."""
import bisect
import math
import re
import statistics
import time
from collections import Counter

ATTR_NAME = {
    1: "Weapon Attack", 2: "Defense", 3: "Armor", 4: "Onslaught",
    5: "Crit Chance", 6: "Crit Damage", 7: "Life Leech", 8: "Mana Leech",
    9: "Loot", 10: "Exp", 11: "Protect All", 12: "Protect Fire",
    13: "Protect Earth", 14: "Protect Energy", 15: "Protect Ice",
    16: "Protect Holy", 17: "Protect Death", 18: "Attack Speed",
    19: "Spell Damage", 20: "Spell Healing", 21: "Max HP", 22: "Max Mana",
    23: "HP Regen", 24: "MP Regen", 25: "Execute", 26: "Frenzy",
    27: "Fire Damage", 28: "Earth Damage", 29: "Energy Damage", 30: "Ice Damage",
    31: "Holy Damage", 32: "Death Damage",
}
RARITY_PT = {0: "Comum", 1: "Incomum", 2: "Raro", 3: "Épico",
             4: "Lendário", 5: "Mítico"}
RARITY_COLOR = {0: "#cfd2d8", 1: "#57b85a", 2: "#4a90e8", 3: "#a05be0",
                4: "#e5a13a", 5: "#e0555a"}

# bonus por nivel de cada atributo (do bundle F0); flat = nao e' %
ATTR_BONUS = {
    1: 1, 2: 2, 3: 1, 4: .5, 5: .35, 6: 1.5, 7: .4, 8: .4, 9: 1, 10: 1,
    11: .17, 12: .5, 13: .5, 14: .5, 15: .5, 16: .5, 17: .5, 18: .5, 19: .5,
    20: .5, 21: .5, 22: .5, 23: .08, 24: .08, 25: .25, 26: .25, 27: .5,
    28: .5, 29: .5, 30: .5, 31: .5, 32: .5,
}
ATTR_FLAT = {2, 3}   # Defense e Armor sao valores absolutos

# bonus da Forja (ftier) por slot: coeficientes t*f^2 + i*f + r
FORGE_SLOT_ATTR = {"weapon": "Onslaught", "helmet": "Momentum", "armor": "Ruse",
                   "legs": "Transcendence", "boots": "Amplification"}
FORGE_COEF = {"Onslaught": (.05, .4, .05), "Momentum": (.05, 1.9, .05),
              "Ruse": (.0307576, .440697, .026),
              "Transcendence": (.0127, .107, .0073),
              "Amplification": (.4, 1.7, .4)}


def forge_pct(slot, ftier):
    attr = FORGE_SLOT_ATTR.get(slot)
    if not attr or not ftier:
        return None
    t, i, r = FORGE_COEF[attr]
    return t * ftier * ftier + i * ftier + r


def forge_text(slot, ftier):
    if not ftier:
        return ""
    attr = FORGE_SLOT_ATTR.get(slot)
    pct = forge_pct(slot, ftier)
    return f"Forja T{ftier}" + (f" (+{pct:.2g}% {attr})" if pct else "")


def now_ms():
    return int(time.time() * 1000)


# gold nao e' linear por 100M: lotes pequenos ficam no piso (~25 coins/100M),
# lotes medios giram mais barato por unidade. Compara so dentro da mesma faixa.
GOLD_BANDS = {"p": "≤150M", "m": "150M-1B", "g": ">1B"}


def gold_band(gold_amount):
    if gold_amount < 150_000_000:
        return "p"
    if gold_amount >= 1_000_000_000:
        return "g"
    return "m"


def normalize(row):
    """Converte um row da API numa chave estavel + preco por unidade.

    - gold  -> preco por 1.000.000 de gold (fungivel, chave unica 'gold')
    - stack -> preco por unidade (empilhavel; varia so pelo nome)
    - gear  -> preco por lote, chave = nome + raridade + imbuements + upLevel
    """
    price = row["currentPrice"]
    if row["type"] == "gold":
        g = row.get("goldAmount") or 0
        band = gold_band(g)
        units = g / 1_000_000
        return {"key": f"gold:{band}", "kind": "gold", "name": "gold", "tier": None,
                "qty": None, "gold_amount": g or None, "up": 0, "band": band,
                "attrs": [], "unit_price": price / units if units else None}

    it = row.get("item") or {}
    name = it.get("name", "?")
    qty = it.get("qty")
    if qty:
        return {"key": f"stack:{name}", "kind": "stack", "name": name,
                "tier": it.get("tier"), "qty": qty, "gold_amount": None,
                "up": 0, "attrs": [], "unit_price": price / qty}

    tier = it.get("tier", 0)
    attrs = sorted(((a["id"], a["level"]) for a in (it.get("attrs") or [])))
    asig = ",".join(f"{i}:{l}" for i, l in attrs)
    up = it.get("upLevel", 0)
    ftier = it.get("ftier", 0)
    return {"key": f"gear:{name}|t{tier}|{asig}|u{up}|f{ftier}", "kind": "gear",
            "name": name, "tier": tier, "qty": None, "gold_amount": None,
            "up": up, "ftier": ftier, "attrs": attrs, "unit_price": float(price)}


def supply_key(nz):
    """Chave de concorrencia: o que um comprador trata como substituivel."""
    if nz["kind"] == "gear":
        return f"g:{nz['name']}|t{nz['tier']}"
    return nz["key"]                       # stack:nome  |  gold:faixa


def count_supply(active_rows):
    c = Counter()
    for row in active_rows:
        try:
            c[supply_key(normalize(row))] += 1
        except Exception:  # noqa: BLE001
            pass
    return c


def supply_windows(active_rows):
    """{supply_key: [endsAt ordenado]} - pra saber QUANDO o estoque fecha."""
    out = {}
    for row in active_rows:
        try:
            out.setdefault(supply_key(normalize(row)), []).append(row["endsAt"])
        except Exception:  # noqa: BLE001
            pass
    for v in out.values():
        v.sort()
    return out


def parse_attrs(key):
    """Extrai [(id, level), ...] da chave de um equipamento."""
    parts = key.split("|")
    if len(parts) < 3 or not parts[2]:
        return []
    out = []
    for p in parts[2].split(","):
        i, l = p.split(":")
        out.append((int(i), int(l)))
    return out


def tier_from_key(key):
    """Extrai a raridade (tier) da chave 'gear:nome|t{tier}|...'."""
    m = re.search(r"\|t(\d+)\|", key or "")
    return int(m.group(1)) if m else None


def attr_score(attrs, cfg, flat=False):
    """Valor da build: soma dos pesos, com um empurrao suave por nivel
    (flat=True: so' presença - o nivel vale via level_mult)."""
    weights = cfg["attr_weights"]
    lf = 0.0 if flat else cfg["attr_level_factor"]
    total = 0.0
    for i, lvl in attrs:
        w = weights.get(str(i), 0)
        if w:
            total += w * (1 + lf * (lvl - 1))
    return round(total, 2)


def level_mult(attrs, cfg):
    """Multiplicador de preco pelos NIVEIS dos atributos (medido no historico:
    so' o Exp - id 10 - encarece de verdade; nivel 1-3 ~1x, 4 ~1.5x, 6 ~2.4x,
    7+ ~3.5x ou mais). Tabela em cfg['attr_level_mult'] = {id: {nivel: mult}};
    niveis acima do maior listado usam o maior."""
    tab = cfg.get("attr_level_mult") or {}
    m = 1.0
    for i, lvl in attrs:
        t = tab.get(str(i))
        if not t or lvl <= 1:
            continue
        ks = sorted(int(k) for k in t)
        use = [k for k in ks if k <= lvl]
        if use:
            m *= t[str(use[-1])]
    return m


EXP_ID = 10


def upgrade_econ(nz, fair, store, cfg, since):
    """Comprar o item JA upado (Exp Lv.N) ou comprar o nivel 1 e upar voce
    mesmo? Custo de upar = gemas esperadas (cfg exp_upgrade_gems, acumulado por
    nivel) x gem_gold, convertido em coins pela taxa fresca do gold. Retorna
    None se nao ha Exp upado, taxa de gold ou base nivel 1 pra comparar."""
    if nz["kind"] != "gear":
        return None
    lvl = max((l for i, l in nz["attrs"] if i == EXP_ID), default=1)
    tab = cfg.get("exp_upgrade_gems") or {}
    ks = [k for k in sorted(int(k) for k in tab) if k <= lvl]
    if lvl <= 1 or not ks:
        return None
    gr = gold_rate(store, cfg)
    if not gr:
        return None
    gems = tab[str(ks[-1])]
    cost = gems * cfg["gem_gold"] / 1e8 * gr["rate"]
    key1 = re.sub(r"(?<![0-9])%d:%d(?=[,|])" % (EXP_ID, lvl), "%d:1" % EXP_ID, nz["key"])
    nz1 = dict(nz, key=key1, attrs=[(i, 1 if i == EXP_ID else l) for i, l in nz["attrs"]])
    v1 = value_item(nz1, store, cfg, since)
    if not v1:
        return None
    diy = v1["fair"] + cost
    return {"lvl": lvl, "gems": gems, "cost": round(cost), "base": round(v1["fair"]),
            "diy": round(diy), "ready": round(fair), "save": round(diy - fair),
            "pick": "pronto" if fair <= diy else "upar"}


def _exp_lvl(attrs):
    return max((l for i, l in attrs if i == EXP_ID), default=0)


def _sell_market(ends, sell_at, per_day, cfg):
    """Concorrencia na hora em que o SEU anuncio fecharia (sell_at): anuncios
    iguais (mesmo nome + raridade) ja ativos que fecham numa janela de
    flip_horizon_hours em volta, contra a demanda esperada nela (media de
    vendas/dia do item). Sem rivais: vender logo; muitos: esperar, com a
    proxima janela (ate 24h depois) de menos concorrencia."""
    half = cfg["flip_horizon_hours"] * 1_800_000
    demand = per_day * cfg["flip_horizon_hours"] / 24

    def rivals(t):
        return sum(1 for e in ends if t - half <= e <= t + half)
    r = rivals(sell_at)
    press = r / max(demand, 0.3)
    if r == 0:
        label, state = "sem concorrência - vender logo", "good"
    elif press <= 1.2:
        label, state = "bom momento", "good"
    elif press <= cfg["flip_max_pressure"]:
        label, state = "ok", "mid"
    else:
        label, state = "mercado cheio - esperar", "hold"
    alt_at = alt_r = None
    if state != "good":
        best = min(((rivals(sell_at + h * 3_600_000), h) for h in range(2, 25, 2)))
        if best[0] < r:
            alt_r, alt_at = best[0], sell_at + best[1] * 3_600_000
    return {"sell_at": sell_at, "rivals": r, "per_day": round(per_day, 1),
            "demand": round(demand, 1), "pressure": round(press, 1),
            "sell_label": label, "sell_state": state, "alt_at": alt_at, "alt_rivals": alt_r}


def _craft_reject(name, tier, opts, offers_raw, g, base, base_p, cfg, now, cheap):
    """Item de nivel 1 com Exp no mercado que NAO passou nos filtros, com o
    motivo e os numeros do melhor nivel-alvo (pra calibrar os filtros)."""
    d = {"name": name, "tier": tier, "base": round(base) if base else None,
         "n_base": len(base_p), "n_up": sum(1 for l, _ in g if l >= 4),
         "proj": round(cheap), "profit": None, "p_win": None, "buy_max": None,
         "lvl": None, "gems": None, "sale": None, "n_pool": None,
         "offers": [{"auction_id": row["id"], "price": cur, "proj": round(proj),
                     "attrs_text": decode_attrs(nz["attrs"]), "bid_count": row.get("bidCount", 0),
                     "secs_left": round((row["endsAt"] - now) / 1000), "ends_at": row["endsAt"]}
                    for proj, cur, early, row, nz in offers_raw]}
    if not opts:
        d["reason"] = (f"amostra: {d['n_up']} venda(s) com Exp 4+ "
                       f"(precisa de {cfg['craft_min_upgraded']} nos níveis vizinhos)")
        return d
    o = max(opts, key=lambda x: x["profit0"])
    d.update(lvl=o["lvl"], gems=o["gems"], sale=round(o["sale"]), n_pool=len(o["pool"]),
             buy_max=round(o["buy_max"]), profit=round(o["profit0"]),
             p_win=round(o["p_win"], 2))
    if o["profit0"] < cfg["craft_min_profit"]:
        d["reason"] = f"lucro {round(o['profit0'])} < mínimo {cfg['craft_min_profit']}"
    else:
        d["reason"] = f"chance {round(o['p_win'] * 100)}% < mínimo {round(cfg['craft_min_p'] * 100)}%"
    return d


_TYPE_REFS = {"sig": None, "data": None}


def type_refs(store, cfg, table, now=None):
    """Cache horario de _type_refs (varre todas as vendas de equipamento -
    caro pra chamar toda hora)."""
    now = now or now_ms()
    sig = now // (cfg["attr_model_hours"] * 3_600_000)
    if _TYPE_REFS["sig"] != sig:
        since = now - cfg["history_days"] * 86_400_000
        _TYPE_REFS.update(sig=sig, data=_type_refs(store, table, since))
    return _TYPE_REFS["data"]


def value_for_watch(nz, store, cfg, table, now=None):
    """Preco justo pra 'Acompanhando': a mesma escada de value_item, e se nao
    ha' historico da variante nem da cesta (item raro, ou pouca venda ainda),
    cai pra estimativa por tipo (mesmo tipo+raridade, igual ao 'Procurar por
    oferta' faz com achados sem historico) - marcada como 'est' pro Painel
    mostrar com ≈. So' pra equipamento sem refino/forja. None se nada serve."""
    now = now or now_ms()
    since = now - cfg["history_days"] * 86_400_000
    val = value_item(nz, store, cfg, since)
    if val:
        return {"fair": val["fair"], "n": val["n"], "basis": val["basis"], "est": False}
    if nz["kind"] == "gear" and not nz["up"] and not nz.get("ftier"):
        info = table.get(nz["name"]) or {}
        est = _type_estimate(type_refs(store, cfg, table, now), info, nz)
        if est:
            fair, n = est
            return {"fair": fair, "n": n, "basis": "tipo", "est": True}
    return None


def craft_table(store, cfg, active_rows, now, fee):
    """Upar pra REVENDER, so' do que esta no mercado AGORA: leiloes ativos de
    item com Exp no nivel 1 (so' da' pra upar quem ja tem o atributo). Uma
    linha por (nome, raridade), com o melhor nivel-alvo L e os demais como
    alternativa. Venda esperada de L = mediana das vendas com Exp em L-1..L+1
    (as gemas dao resultado incerto, entao os vizinhos entram); custo = gemas
    esperadas na taxa fresca do gold. 'Comprar ate' = o maior preco que ainda
    deixa craft_min_profit: venda*(1-rake) - taxa - gemas - craft_min_profit.

    PRECO DE COMPRA: todo leilao comeca perto do piso e sobe ate fechar, entao
    com mais de alert_window_seconds pela frente o preco atual nao vale; usa-se
    o fechamento tipico do nivel 1 com Exp (mediana das vendas), se maior.
    Cada leilao traz o lucro mediano, o pior e o melhor (vendas observadas) e o
    'se nao render' (as gemas nao passam do nivel 3: vende como nivel 1)."""
    gr = gold_rate(store, cfg)
    if not gr:
        return {"rows": [], "others": [], "in_market": 0}
    days = cfg["craft_days"]
    gem_coin = cfg["gem_gold"] / 1e8 * gr["rate"]
    tab = {int(k): v for k, v in cfg["exp_upgrade_gems"].items() if int(k) >= 4}
    live = {}                                   # leiloes ativos de nivel 1 com Exp
    for row in active_rows:
        nz = normalize(row)
        if (nz["kind"] == "gear" and not nz["up"] and not nz.get("ftier")
                and _exp_lvl(nz["attrs"]) == 1
                and not any(l > 1 for _, l in nz["attrs"])):
            live.setdefault((nz["name"], nz["tier"]), []).append((row, nz))
    groups = {}
    for r in store.gear_sales_keys(now - days * 86_400_000):
        if (r["name"], r["tier"]) not in live:
            continue
        at = parse_attrs(r["key"])
        lvl = _exp_lvl(at)
        nup = sum(1 for _, l in at if l > 1)
        if lvl and (nup == 0 or (nup == 1 and lvl > 1)):
            groups.setdefault((r["name"], r["tier"]), []).append((lvl, r["final_price"]))
    rake, out, others = cfg["rake_pct"], [], []
    windows = supply_windows(active_rows)
    for (name, tier), rows in live.items():
        g = groups.get((name, tier), [])
        gst = stats(store.gear_sales_by_tier(name, tier, 0, now - days * 86_400_000), now)
        per_day = gst["per_day"] if gst else 0.0
        ends = windows.get(f"g:{name}|t{tier}", [])
        base_p = [p for l, p in g if l == 1]
        base = statistics.median(base_p) if base_p else None
        offers_raw = []
        for row, nz in rows:
            cur = row["currentPrice"]
            secs = (row["endsAt"] - now) / 1000
            early = secs > cfg["alert_window_seconds"]
            proj = max(cur, base) if (early and base) else cur
            offers_raw.append((proj, cur, early, row, nz))
        offers_raw.sort(key=lambda x: x[0])
        offers_raw = offers_raw[:3]
        cheap = offers_raw[0][0]
        opts = []
        for L, gems in tab.items():
            pool = [p for l, p in g if L - 1 <= l <= L + 1]
            if len(pool) < cfg["craft_min_upgraded"]:
                continue                                     # amostra nos niveis L-1..L+1
            cost = gems * gem_coin
            sale = statistics.median(pool)
            nets = [p * (1 - rake) - fee - cheap - cost for p in pool]
            opts.append({"lvl": L, "gems": gems, "cost": cost, "sale": sale, "pool": pool,
                         "buy_max": sale * (1 - rake) - fee - cost - cfg["craft_min_profit"],
                         "profit0": statistics.median(nets),
                         "p_win": sum(1 for n in nets if n > 0) / len(nets)})
        ok_opts = [o for o in opts if o["profit0"] >= cfg["craft_min_profit"]
                   and o["p_win"] >= cfg["craft_min_p"]]
        if not ok_opts:
            others.append(_craft_reject(name, tier, opts, offers_raw, g, base, base_p,
                                        cfg, now, cheap))
            continue
        best = max(ok_opts, key=lambda o: o["profit0"])
        offers = []
        for proj, cur, early, row, nz in offers_raw:
            net = lambda p: p * (1 - rake) - fee - proj - best["cost"]
            offers.append({
                "auction_id": row["id"], "price": cur, "proj": round(proj),
                "attrs_text": decode_attrs(nz["attrs"]),
                "proj_known": bool(base) or not early,
                "secs_left": round((row["endsAt"] - now) / 1000),
                "ends_at": row["endsAt"], "bid_count": row.get("bidCount", 0),
                "profit": round(net(best["sale"])),
                "worst": round(net(min(best["pool"]))),
                "best": round(net(max(best["pool"]))),
                "fail": round(net(base)) if base else None,
                "ok": proj <= best["buy_max"],
                **_sell_market(ends, row["endsAt"] + 6 * 3_600_000, per_day, cfg)})
        out.append({
            "name": name, "tier": tier, "lvl": best["lvl"], "gems": best["gems"],
            "cost": round(best["cost"]), "sale": round(best["sale"]),
            "sale_min": round(min(best["pool"])), "sale_max": round(max(best["pool"])),
            "n_pool": len(best["pool"]), "per_day": round(len(best["pool"]) / days, 2),
            "buy_max": round(best["buy_max"]), "profit": offers[0]["profit"],
            "p_win": round(best["p_win"], 2),
            "base": round(base) if base else None,
            "n_base": len(base_p), "offers": offers,
            "alts": [{"lvl": o["lvl"], "gems": o["gems"], "buy_max": round(o["buy_max"]),
                      "sale": round(o["sale"]), "n": len(o["pool"])}
                     for o in opts if o is not best]})
    out.sort(key=lambda x: -x["profit"])
    others.sort(key=lambda x: (x["profit"] is None, -(x["profit"] or 0)))
    return {"rows": out, "others": others, "in_market": len(live)}


def boss_monitor(store, cfg, active_rows, now):
    """Boss token x addon casket: boss_tokens_per_addon (500) tokens compram 1
    addon que custa boss_addon_coins (50) coins na loja, entao cada token vale
    addon/per_addon coins DE USO (sem revenda). Um lote de boss token e' bom se
    fechar abaixo desse valor. Cada leilao ativo traz o custo por 500 tokens, o
    valor do lote e o teto de lance = valor x (1 - boss_min_margin). O leilao
    cedo comeca no piso e sobe, e o desfecho e' bimodal (ou ninguem disputa e
    fecha no piso, ou vira briga e passa do valor), entao a mediana engana: o que
    importa e' a CHANCE de fechar ate o teto e o lucro esperado de quem da lance
    so' ate o teto (se passar, larga e nao perde nada). Isso sai dos ultimos
    fechamentos de lotes parecidos (o lote nunca fecha abaixo do preco de agora).
    O mercado ja se ajustou uma vez (o
    custo de 500 tokens foi de ~18 pra ~60 coins no dia da atualizacao), entao o
    resumo mostra se a arbitragem esta aberta, apertada ou fechada. So' os lotes
    que compensam (lucro esperado >= boss_min_ev) sao listados, os boss_show_n
    que encerram primeiro."""
    name = cfg["boss_token_name"].lower()
    per_addon, addon = cfg["boss_tokens_per_addon"], cfg["boss_addon_coins"]
    ctok = addon / per_addon
    margin, credit = cfg["boss_min_margin"], cfg["boss_leftover_credit"]

    def lot_value(q):                 # addons inteiros valem cheio; a sobra, boss_leftover_credit
        whole = q // per_addon
        return whole * addon + (q - whole * per_addon) * ctok * credit

    sales = [r for r in store.sales_for_name(name, now - 14 * 86_400_000)
             if r["kind"] == "stack" and (r["name"] or "").lower() == name and r["qty"]]
    recent = sales[:cfg["boss_recent_n"]]                      # ja vem do mais novo pro mais antigo
    tok = sorted(r["unit_price"] for r in recent)
    med = statistics.median(tok) if tok else None
    per500 = med * per_addon if med is not None else None
    pct_below = (sum(1 for r in recent if r["unit_price"] * per_addon < addon) / len(recent)
                 if recent else None)
    state = None
    if per500 is not None:
        state = "open" if per500 < addon * 0.8 else "tight" if per500 < addon else "closed"

    daily = {}
    for r in sales:
        d = time.strftime("%d/%m", time.localtime(r["ends_at"] / 1000))
        daily.setdefault(d, []).append(r)
    daily_rows = [{"day": d, "per500": round(statistics.median(x["unit_price"] for x in rs) * per_addon, 1),
                   "pct_below": round(sum(1 for x in rs if x["unit_price"] * per_addon < addon) / len(rs), 2),
                   "n": len(rs), "bids": round(statistics.mean((x["bid_count"] or 0) for x in rs), 1)}
                  for d, rs in daily.items()]
    daily_rows = daily_rows[:8]

    rows = []
    for row in active_rows:
        it = row.get("item") or {}
        q = it.get("qty")
        if row.get("type") != "item" or not q or (it.get("name") or "").lower() != name:
            continue
        cur, secs = row["currentPrice"], (row["endsAt"] - now) / 1000
        similar = sorted(r["unit_price"] for r in recent if 0.5 * q <= r["qty"] <= 2 * q)
        sim = statistics.median(similar) if len(similar) >= 5 else med
        early = secs > cfg["alert_window_seconds"]
        proj = max(cur, sim * q) if (early and sim is not None) else cur
        val, teto = lot_value(q), lot_value(q) * (1 - margin)
        pool = [r for r in recent if 0.5 * q <= r["qty"] <= 2 * q]
        pool = pool if len(pool) >= 5 else recent
        if early and pool:
            finals = [max(r["unit_price"] * q, cur) for r in pool]   # preco so' sobe
        else:                                                        # perto do fim o preco ja e' real
            finals = [cur]
        wins = [f for f in finals if f <= teto]
        p_ok = len(wins) / len(finals)
        win_profit = statistics.mean(val - f - 1 for f in wins) if wins else None
        ev = p_ok * win_profit if wins else 0.0
        st = ("no" if cur > teto else "good" if ev >= cfg["boss_min_ev"] else "bid")
        rows.append({"auction_id": row["id"], "qty": q, "price": cur, "bids": row.get("bidCount", 0),
                     "secs_left": round(secs), "ends_at": row["endsAt"],
                     "per500": round(cur / q * per_addon, 1), "value": round(val),
                     "proj": round(proj), "teto": round(teto), "p_ok": round(p_ok, 2),
                     "win_profit": round(win_profit) if win_profit is not None else None,
                     "ev": round(ev, 1), "n_pool": len(finals), "state": st})
    good = sorted((r for r in rows if r["state"] == "good"), key=lambda r: r["ends_at"])
    return {"rows": good[:cfg["boss_show_n"]], "n_active": len(rows), "daily": daily_rows,
            "cfg": {"per_addon": per_addon, "addon": addon, "margin": margin, "credit": credit},
            "summary": {"per500": round(per500, 1) if per500 is not None else None,
                        "per500_p25": round(tok[len(tok) // 4] * per_addon, 1) if tok else None,
                        "pct_below": round(pct_below, 2) if pct_below is not None else None,
                        "n_recent": len(recent), "state": state,
                        "good": len(good)}}


def decode_attrs(attrs):
    out = []
    for i, lvl in attrs:
        val = lvl * ATTR_BONUS.get(i, 0)
        pct = "" if i in ATTR_FLAT else "%"
        out.append(f"{ATTR_NAME.get(i, i)} +{val:.2g}{pct} (Lv.{lvl})")
    return ", ".join(out)


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def stats(rows, now=None):
    """rows: sequencia com unit_price / ends_at. Retorna None se vazio."""
    now = now or now_ms()
    pairs = [(r["unit_price"], r["ends_at"]) for r in rows
             if r["unit_price"] is not None]
    if not pairs:
        return None
    prices = sorted(p for p, _ in pairs)
    times = [t for _, t in pairs if t]
    span_days = max((max(times) - min(times)) / 86_400_000, 1.0) if times else 1.0
    cut = now - 2 * 86_400_000
    recent = sorted(p for p, t in pairs if t and t >= cut)
    older = sorted(p for p, t in pairs if t and t < cut)
    return {
        "n": len(prices),
        "per_day": len(prices) / span_days,
        "median": statistics.median(prices),
        "recent_median": statistics.median(recent) if recent else None,
        "recent_n": len(recent),
        "older_median": statistics.median(older) if older else None,
        "p25": _percentile(prices, 25),
        "p10": _percentile(prices, 10),
        "min": prices[0],
        "max": prices[-1],
    }


def _fair_with_trend(st, cfg):
    """Preco justo protegido contra tendencia: o menor entre a mediana da
    semana e a mediana dos ultimos 2 dias - nao persegue alta, protege na
    queda. trend = mediana recente (2d) vs mediana do resto da janela, ou
    None se nao ha vendas recentes suficientes."""
    trailing = st["median"]
    rm, om = st.get("recent_median"), st.get("older_median")
    if (rm is None or not om or not trailing
            or st.get("recent_n", 0) < cfg["trend_min_recent"]):
        return trailing, None
    return min(trailing, rm), round(rm / om - 1, 3)


# ---------------------------------------------------------------- gold
# Gold e' um mercado ancorado numa "taxa" unica (coins por 100M de gold) que
# muda por regimes. Acima de ~150M o preco por 100M nao depende do tamanho;
# so' lote pequeno sofre o piso de 25 coins. Modelo: taxa fresca (mediana
# exponencial das vendas recentes) e preco esperado = max(25, taxa*lote/100M).
_GOLD_CACHE = {"at": 0, "store": None, "data": None}


def _wmedian(pairs):
    """pairs: [(valor, peso)] -> mediana ponderada."""
    pairs = sorted(pairs)
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def _rate_at(times, vals, ts, hl_h):
    """Taxa em ts: mediana exponencial (meia-vida hl_h) das vendas ate ts."""
    hi = bisect.bisect_right(times, ts)
    lo = bisect.bisect_left(times, ts - hl_h * 6 * 3_600_000)
    if hi - lo < 25:
        return None
    return _wmedian([(vals[i], 0.5 ** ((ts - times[i]) / 3_600_000 / hl_h))
                     for i in range(lo, hi)])


def _gold_series(store, cfg, now, days):
    """(tempos, coins/100M) das vendas de gold em lotes grandes, em ordem."""
    times, vals = [], []
    for r in store.gold_sales_window(now - int(days * 86_400_000)):
        if (r["gold_amount"] or 0) > cfg["gold_rate_min_lot"]:
            times.append(r["ends_at"])
            vals.append(r["unit_price"] * 100)
    return times, vals


def gold_rate(store, cfg, now=None):
    """Taxa fresca do gold + tendencia (vs 48h atras). None se falta dado."""
    now = now or now_ms()
    c = _GOLD_CACHE
    if c["store"] is store and now - c["at"] < 60_000:
        return c["data"]
    hl = cfg["gold_rate_halflife_h"]
    times, vals = _gold_series(store, cfg, now, hl * 6 / 24 + 2)
    rate = _rate_at(times, vals, now, hl)
    data = None
    if rate is not None:
        past = _rate_at(times, vals, now - 48 * 3_600_000, hl)
        data = {"rate": rate,
                "trend": round(rate / past - 1, 3) if past else None,
                "per_day": float(sum(1 for t in times if t >= now - 86_400_000)),
                "n": bisect.bisect_right(times, now)
                     - bisect.bisect_left(times, now - hl * 6 * 3_600_000)}
    c.update(at=now, store=store, data=data)
    return data


def gold_expected(rate, gold_amount):
    """Preco total esperado de um lote na taxa de mercado (respeita o piso)."""
    return max(FLOOR_PRICE, rate * gold_amount / 1e8)


def is_anchor(per100):
    """Preco redondo do vendedor (20 ou 25 por 100M): tende a nao subir."""
    return abs(per100 - 20) < 0.3 or abs(per100 - 25) < 0.3


def gold_curve(x, knots):
    """Interpolacao linear por partes em [[x, y], ...] (x crescente)."""
    if x <= knots[0][0]:
        return knots[0][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return knots[-1][1]


def gold_market(store, cfg, now=None):
    """Momento do mercado de gold: taxa, variacao, posicao nos ultimos 14 dias,
    volatilidade e a serie (pro grafico). None se falta dado."""
    now = now or now_ms()
    hl = cfg["gold_rate_halflife_h"]
    days = 14
    times, vals = _gold_series(store, cfg, now, days + hl * 6 / 24)
    step = 2 * 3_600_000
    grid = list(range(now - days * 86_400_000, now, step)) + [now]
    pts = [(ts, _rate_at(times, vals, ts, hl)) for ts in grid]
    pts = [(ts, r) for ts, r in pts if r is not None]
    if len(pts) < 24:
        return None
    rates = [r for _, r in pts]
    tss = [ts for ts, _ in pts]
    cur = rates[-1]

    def ago(h):
        return rates[min(bisect.bisect_left(tss, now - h * 3_600_000), len(rates) - 1)]

    ch = [math.log(rates[i + 12] / rates[i]) for i in range(len(rates) - 12)]
    pct = round(100 * sum(1 for r in rates if r <= cur) / len(rates))
    ch24 = round(cur / ago(24) - 1, 3)
    ch7 = round(cur / ago(24 * 7) - 1, 3)
    if pct <= 25:
        state, label = (("falling", "barato, mas ainda caindo") if ch24 <= -0.03
                        else ("good", "bom momento pra comprar"))
    elif pct >= 75:
        state, label = "high", "caro agora"
    else:
        state, label = "normal", "normal"
    return {"rate": round(cur, 2), "change24": ch24, "change7d": ch7, "pct": pct,
            "lo": round(min(rates), 2), "hi": round(max(rates), 2),
            "vol24": round(statistics.pstdev(ch), 3) if len(ch) > 2 else None,
            "state": state, "label": label,
            "series": [[ts, round(r, 2)] for ts, r in pts[::3] + pts[-1:]]}


# --------------------------------------------------------------- valor dos atributos
# Regressao hedonica com PISO (Tobit): preco = efeito do (nome, raridade) x
# multiplicadores dos atributos. Quase metade das vendas fecha no piso de 25
# coins (preco real abaixo disso e' invisivel), entao os que fecharam no piso
# entram como "censurados" (EM). Ajuste em Python puro, com cache por
# attr_model_hours. Validado fora da amostra: acerta o nivel do Exp
# (Lv4..8: 39/50/80/177/321 previsto vs 36/51/77/176/300 real) e ganha da
# mediana simples e do score antigo nos itens com atributo premium.
_ATTR_MODEL = {"sig": None, "m": None}
_FLOOR_LOG = math.log(26.0)


def _model_feats(attrs, ids):
    """Indices (esparsos) das colunas ativas: atributos do modelo + degraus do
    nivel do Exp (4+, 6+, 8+)."""
    d = dict(attrs)
    on = [k for k, i in enumerate(ids) if i in d]
    e = d.get(EXP_ID, 0)
    base = len(ids)
    for k, lv in enumerate((4, 6, 8)):
        if e >= lv:
            on.append(base + k)
    return on


def _invert(a):
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        d = m[c][c]
        m[c] = [v / d for v in m[c]]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    return [row[n:] for row in m]


def _phi_ratio(a):
    """phi(a)/Phi(a) (razao inversa de Mills, censura a esquerda)."""
    cdf = 0.5 * (1 + math.erf(a / math.sqrt(2)))
    return math.exp(-a * a / 2) / math.sqrt(2 * math.pi) / max(cdf, 1e-9)


def _fit_attr_model(store, cfg, now):
    ids = list(cfg["attr_model_ids"])
    p = len(ids) + 3
    by_grp = {}
    for r in store.gear_sales_keys(now - cfg["attr_model_days"] * 86_400_000):
        by_grp.setdefault((r["name"], r["tier"]), []).append(r)
    names, data = [], []
    for g, rs in by_grp.items():
        if len(rs) < cfg["attr_model_min_group"]:
            continue
        gi = len(names)
        names.append(g)
        for r in rs:
            data.append((gi, _model_feats(parse_attrs(r["key"]), ids),
                         math.log(r["final_price"]), r["final_price"] <= 26))
    G, n = len(names), len(data)
    if G < 5 or n < 200:
        return None
    cnt = [0] * G
    xs = [[0.0] * p for _ in range(G)]
    xtx = [[0.0] * p for _ in range(p)]
    feat_n = [0] * p
    for gi, on, _y, _c in data:
        cnt[gi] += 1
        for a in on:
            xs[gi][a] += 1
            feat_n[a] += 1
            for b in on:
                xtx[a][b] += 1
    xbar = [[xs[g][k] / cnt[g] for k in range(p)] for g in range(G)]
    for g in range(G):
        for a in range(p):
            for b in range(p):
                xtx[a][b] -= cnt[g] * xbar[g][a] * xbar[g][b]
    for k in range(p):
        xtx[k][k] += cfg["attr_model_ridge"]
    minv = _invert(xtx)
    ys = [d[2] for d in data]
    beta, sig, mu = [0.0] * p, 1.0, None
    for _ in range(cfg["attr_model_iters"]):
        if mu is None:
            yy = ys
        else:                                   # censurados: E[y | y < piso]
            yy = [mu[i] - sig * _phi_ratio((_FLOOR_LOG - mu[i]) / sig) if data[i][3] else ys[i]
                  for i in range(n)]
        gsum = [0.0] * G
        for i, d in enumerate(data):
            gsum[d[0]] += yy[i]
        gm = [gsum[g] / cnt[g] for g in range(G)]
        xty = [0.0] * p
        for i, d in enumerate(data):
            yd = yy[i] - gm[d[0]]
            for a in d[1]:
                xty[a] += yd
        beta = [sum(minv[a][b] * xty[b] for b in range(p)) for a in range(p)]
        xbb = [sum(xbar[g][k] * beta[k] for k in range(p)) for g in range(G)]
        mu, rss = [], 0.0
        for i, d in enumerate(data):
            m_i = gm[d[0]] + sum(beta[a] for a in d[1]) - xbb[d[0]]
            mu.append(m_i)
            rss += (yy[i] - m_i) ** 2
        sig = max(math.sqrt(rss / n), 0.3)
    labels = [ATTR_NAME.get(i, str(i)) for i in ids] + ["Exp Lv.4+", "Exp Lv.6+", "Exp Lv.8+"]
    return {"ids": ids, "beta": beta, "labels": labels, "feat_n": feat_n, "n": n,
            "groups": G, "sigma": sig, "fitted_at": now,
            "gx": {names[g]: gm[g] - xbb[g] for g in range(G)}}


def attr_model(store, cfg, now=None):
    now = now or now_ms()
    sig = now // (cfg["attr_model_hours"] * 3_600_000)
    if _ATTR_MODEL["sig"] != sig:
        _ATTR_MODEL.update(sig=sig, m=_fit_attr_model(store, cfg, now))
    return _ATTR_MODEL["m"]


def attr_model_price(nz, store, cfg):
    """Preco esperado do item (mediana) pelo modelo, ou None se o grupo
    (nome, raridade) nao tem historico suficiente."""
    m = attr_model(store, cfg)
    gx = m["gx"].get((nz["name"], nz["tier"])) if m else None
    if gx is None:
        return None
    return max(25.0, math.exp(gx + sum(m["beta"][a] for a in _model_feats(nz["attrs"], m["ids"]))))


def attr_multipliers(store, cfg):
    """Tabela pro painel: multiplicador de cada atributo (e degraus do Exp)."""
    m = attr_model(store, cfg)
    if not m:
        return None
    rows = [{"name": m["labels"][k], "mult": round(math.exp(b), 2), "n": m["feat_n"][k]}
            for k, b in enumerate(m["beta"])]
    return {"rows": rows, "n": m["n"], "groups": m["groups"], "fitted_at": m["fitted_at"]}


def appraise(nz, store, cfg, now=None, fee=1.0):
    """Avaliador: quanto vale um equipamento pelas especificacoes dele
    (raridade, atributos, nivel do Exp), decomposto passo a passo.
    preco = base do (nome, raridade) sem atributos x multiplicador de cada
    atributo x degraus do nivel do Exp (modelo Tobit de attr_model). Devolve o
    valor estimado (mediana), a faixa provavel (p10-p90 pela dispersao do
    modelo), a referencia conservadora (p25: o que se costuma conseguir de
    fato ao revender - o preco 'justo' dos alertas ficava ~1,4x acima) e as
    vendas mais parecidas. None se nao ha historico do (nome, raridade)."""
    now = now or now_ms()
    m = attr_model(store, cfg, now)
    gx = m["gx"].get((nz["name"], nz["tier"])) if m else None
    if gx is None:
        return None
    ids, beta, sig = m["ids"], m["beta"], m["sigma"]
    latent = math.exp(gx)
    steps = [{"label": "Item base (sem atributos)", "mult": None, "price": round(max(25.0, latent), 1)}]
    warns = []
    exp_lvl = 0
    for i, lvl in sorted(nz["attrs"], key=lambda a: (a[0] != EXP_ID, a[0])):
        name = ATTR_NAME.get(i, str(i))
        if i in ids:
            mult = math.exp(beta[ids.index(i)])
            latent *= mult
            steps.append({"label": f"{name} (Lv.{lvl})" if i != EXP_ID else "Exp (tem o atributo)",
                          "mult": round(mult, 2), "price": round(max(25.0, latent), 1)})
            if i == EXP_ID:
                exp_lvl = lvl
                for k, lv in enumerate((4, 6, 8)):
                    if lvl >= lv:
                        mult = math.exp(beta[len(ids) + k])
                        latent *= mult
                        steps.append({"label": f"Exp no nível {lv} ou mais (Lv.{lvl})",
                                      "mult": round(mult, 2), "price": round(max(25.0, latent), 1)})
        else:
            steps.append({"label": f"{name} (Lv.{lvl})", "mult": 1.0,
                          "price": round(max(25.0, latent), 1), "note": "sem prêmio medido"})
    est = max(25.0, latent)
    z10, z25 = 1.2816, 0.6745
    out = {"est": round(est), "p10": round(max(25.0, est * math.exp(-z10 * sig))),
           "p25": round(max(25.0, est * math.exp(-z25 * sig))),
           "p90": round(est * math.exp(z10 * sig)), "sigma": round(sig, 2),
           "steps": steps, "system_fair": None, "system_basis": None}
    # o que o painel mostra hoje (escada: exata -> cesta -> modelo)
    val = value_item(nz, store, cfg, now - cfg["history_days"] * 86_400_000)
    if val:
        out["system_fair"], out["system_basis"] = round(val["fair"]), val["basis"]
    out["resell_max"] = round(out["p25"] * (1 - cfg["rake_pct"]) - fee)
    # vendas parecidas: mesmo nome + raridade, ordenadas por distancia
    since = now - 30 * 86_400_000
    prem = {i for i, _ in nz["attrs"] if i in (EXP_ID, 4, 6, 19)}
    comps = []
    grp = store.gear_sales_by_tier(nz["name"], nz["tier"], 0, since)
    for r in grp:
        at = parse_attrs(r["key"])
        ps = {i for i, _ in at if i in (EXP_ID, 4, 6, 19)}
        dist = abs(_exp_lvl(at) - exp_lvl) + 2 * len(prem ^ ps)
        comps.append((dist, -r["ends_at"], r, at))
    comps.sort(key=lambda c: (c[0], c[1]))
    out["group_n"] = len(grp)
    out["comparables"] = [{"price": r["unit_price"], "ends_at": r["ends_at"],
                           "attrs_text": decode_attrs(at), "dist": d}
                          for d, _, r, at in comps[:6]]
    out["exact_n"] = sum(1 for r in grp if r["key"] == nz["key"])
    # avisos de confianca
    if len(grp) < 15:
        warns.append(f"poucas vendas desse item e raridade ({len(grp)} em 30 dias): a base é incerta")
    if exp_lvl >= 4:
        top = len(ids) + (2 if exp_lvl >= 8 else 1 if exp_lvl >= 6 else 0)
        if m["feat_n"][top] < 150:
            warns.append(f"poucas vendas com Exp nesse nível ({m['feat_n'][top]} no modelo): extrapolação")
    if any(i != EXP_ID and lvl >= 4 for i, lvl in nz["attrs"]):
        warns.append("atributos (fora o Exp) em nível alto: o efeito do nível deles não foi confirmado "
                     "fora da amostra, então não entra na conta - a faixa provável cobre isso")
    if nz.get("up") or nz.get("ftier"):
        warns.append("item refinado/forjado: o valor do refino/forja não está incluído")
    out["warnings"] = warns
    return out


def value_item(nz, store, cfg, since):
    """Estima o preco justo (mediana), p25, liquidez e a base usada.

    Equipamento usa uma escada: variante exata -> mesma raridade (ajustada
    pelo score de atributos) -> desiste. Stack usa a chave exata; gold usa a
    taxa fresca de mercado (gold_rate). Retorna dict ou None se nao ha
    historico suficiente.
    """
    if nz["kind"] == "gold":
        gr = gold_rate(store, cfg)
        g = nz.get("gold_amount") or 0
        if not gr or not g:
            return None
        fair = gold_expected(gr["rate"], g) / (g / 1e6)   # por 1M, ja com o piso
        return {"fair": fair, "p25": fair * 0.9, "per_day": gr["per_day"],
                "n": gr["n"], "basis": "taxa", "adj": 1.0, "trend": gr["trend"]}

    if nz["kind"] != "gear":
        st = stats(store.sales_for_key(nz["key"], since))
        if not st or st["n"] < cfg["min_sales"]:
            return None
        fair, trend = _fair_with_trend(st, cfg)
        return {"fair": fair, "p25": st["p25"], "per_day": st["per_day"],
                "n": st["n"], "basis": "exata", "adj": 1.0, "trend": trend}

    exact = stats(store.sales_for_key(nz["key"], since))
    if exact and exact["n"] >= cfg["min_sales"]:
        fair, trend = _fair_with_trend(exact, cfg)
        return {"fair": fair, "p25": exact["p25"],
                "per_day": exact["per_day"], "n": exact["n"],
                "basis": "exata", "adj": 1.0, "trend": trend}

    if nz["up"] > 0 or nz.get("ftier", 0) > 0:
        return None  # refinado/forjado: nao da pra avaliar pela variante base

    basket = store.gear_sales_by_tier(nz["name"], nz["tier"], 0, since)
    bst = stats(basket)
    if not bst or bst["n"] < cfg["min_sales_loose"]:
        # item raro: tenta janelas cada vez mais largas (cfg basket_widen_days,
        # ex. 14 e 30 dias) antes de desistir - sem isso, um item com pouca
        # venda ficava sem preco justo pra sempre (nem entrava no "raridade").
        # Validado por backtest: mais cobertura, sem piorar quem ja tinha preco.
        for wide_days in cfg["basket_widen_days"]:
            since_wide = now_ms() - wide_days * 86_400_000
            if since_wide >= since:
                continue
            basket = store.gear_sales_by_tier(nz["name"], nz["tier"], 0, since_wide)
            bst = stats(basket)
            if bst and bst["n"] >= cfg["min_sales_loose"]:
                break
    if not bst or bst["n"] < cfg["min_sales_loose"]:
        return None

    bscores = sorted(attr_score(parse_attrs(r["key"]), cfg, flat=True) for r in basket)
    bmed = statistics.median(bscores) if bscores else 0
    iscore = attr_score(nz["attrs"], cfg, flat=True)
    ratio = iscore / bmed if bmed > 0 else 1.0
    adj = min(max(ratio, cfg["attr_adj_min"]), cfg["attr_adj_max"])
    # nivel dos atributos (Exp 4%, 10%...): relativo ao nivel tipico da cesta
    bmult = statistics.median(level_mult(parse_attrs(r["key"]), cfg) for r in basket)
    adj *= min(max(level_mult(nz["attrs"], cfg) / bmult, 0.3), 12.0)
    fair, trend = _fair_with_trend(bst, cfg)
    mp = attr_model_price(nz, store, cfg)
    if mp is not None:           # modelo de atributos (com piso); mantem a tendencia recente
        adj = min(max(mp / bst["median"], 0.4), 15.0)
    return {"fair": fair * adj, "p25": bst["p25"] * adj,
            "per_day": bst["per_day"], "n": bst["n"], "basis": "raridade",
            "adj": adj, "trend": trend}


def find_opportunities(active_rows, store, cfg):
    """Filtra leiloes ativos baratos vs. valor estimado e liquidos."""
    since = now_ms() - cfg["history_days"] * 86_400_000
    watch = [w.lower() for w in cfg.get("watchlist", [])]
    windows = supply_windows(active_rows)
    out = []
    for row in active_rows:
        nz = normalize(row)
        unit = nz["unit_price"]
        if unit is None:
            continue
        if watch and not any(w in nz["name"].lower() for w in watch):
            continue

        val = value_item(nz, store, cfg, since)
        if not val or val["per_day"] < cfg["min_sales_per_day"]:
            continue

        kind = nz["kind"]
        is_gold = kind == "gold"
        # calibracao: o "justo" dos alertas de equipamento ficava ~1,4x acima do que
        # o item vendia nos dias seguintes (o barato costuma ser barato por um motivo)
        calib = cfg["gear_fair_calib"] if kind == "gear" else 1.0
        fair = val["fair"] * calib
        discount = 1 - unit / fair if fair else 0
        thr = cfg["gold_discount_threshold"] if is_gold else cfg["discount_threshold"]
        if discount < thr:
            continue

        total = row["currentPrice"]
        budget_max = cfg["gold_budget_max_coins"] if is_gold else cfg["budget_max_coins"]
        if not (cfg["budget_min_coins"] <= total <= budget_max):
            continue

        if row.get("bidCount", 0) < cfg["min_bids"]:
            continue

        lot = total / unit if unit else 1
        est_saving = round((fair - unit) * lot)
        if is_gold and est_saving < cfg["gold_min_saving_coins"]:
            continue

        # teto de lance: gold e' pra USAR (>= gold_use_min_discount abaixo da
        # taxa); equipamento/lote, o desconto minimo de oportunidade
        suggested_max = round(fair * (1 - (cfg["gold_use_min_discount"] if is_gold
                                           else thr)) * lot)
        # teto pra nao perder revendendo (revenda a preco justo, menos o rake)
        breakeven = round(fair * (1 - cfg["rake_pct"]) * lot)

        # oportunidade "excelente" de ultima hora: alta raridade + margem grande.
        # essas ficam na lista ate o fim (ignoram o piso de min_seconds_left).
        flash_item = (kind == "gear" and (nz["tier"] or 0) >= cfg["flash_min_rarity"]
                      and suggested_max - total >= cfg["flash_min_gap_coins"])

        secs_left = (row["endsAt"] - now_ms()) / 1000
        if secs_left > cfg["alert_window_seconds"]:
            continue
        if secs_left < cfg["min_seconds_left"] and not flash_item:
            continue

        # concorrencia: leiloes duram ~6h, entao quando voce comprar e
        # relistar, o seu leilao vai competir com tudo que fecha na mesma
        # janela (~flip_horizon_hours). vendedores vs compradores nessa janela.
        ends = windows.get(supply_key(nz), [])
        now = now_ms()
        win_ms = cfg["flip_horizon_hours"] * 3_600_000
        others = max(len(ends) - 1, 0)
        closing_win = max(sum(1 for e in ends if e - now <= win_ms) - 1, 0)
        demand_win = val["per_day"] * cfg["flip_horizon_hours"] / 24
        supply_pressure = round(closing_win / max(demand_win, 0.3), 1)
        closing_6h = max(sum(1 for e in ends if e - now <= 6 * 3_600_000) - 1, 0)
        days_supply = round(others / max(val["per_day"], 0.1), 1)  # ainda util informar

        # veredito: comprar pra REVENDER (giro vale) ou so pra USAR.
        # preco caindo = nao e' flip (facao caindo); ainda pode valer pra uso.
        trend = val.get("trend")
        falling = (trend or 0) <= cfg["trend_down"]
        flip_ok = (supply_pressure <= cfg["flip_max_pressure"]
                   and val["per_day"] >= cfg["flip_min_liquidity"]
                   and not falling)
        verdict = "revender" if (flip_ok and not is_gold) else "usar"   # gold: so' uso

        o = {
            "auction_id": row["id"], "key": nz["key"], "name": nz["name"],
            "kind": kind, "unit_price": unit, "current_price": total,
            "median": fair, "p25": val["p25"] * calib, "discount": discount,
            "suggested_max": suggested_max, "breakeven": breakeven,
            "est_saving": est_saving, "trend": trend,
            "sales_per_day": val["per_day"], "n": val["n"], "basis": val["basis"],
            "supply": others, "days_supply": days_supply,
            "supply_pressure": supply_pressure, "closing_6h": max(closing_6h, 0),
            "verdict": verdict, "flash_item": flash_item,
            "secs_left": secs_left, "ends_at": row["endsAt"],
            "bid_count": row.get("bidCount", 0),
        }
        if kind == "gear":
            o["rarity"] = nz["tier"]
            o["up"] = nz["up"]
            o["ftier"] = nz.get("ftier", 0)
            o["attr_score"] = attr_score(nz["attrs"], cfg)
            o["attrs_text"] = decode_attrs(nz["attrs"])
        o["tier"] = quality_tier(o, cfg)
        out.append(o)
    out.sort(key=lambda o: (_TIER_RANK[o["tier"]],
                            o.get("verdict") == "revender", o["discount"]),
             reverse=True)
    return out


_TIER_RANK = {"boa": 0, "muito boa": 1, "excelente": 2}
_BUMP = {"boa": "muito boa", "muito boa": "excelente", "excelente": "excelente"}


def _discount_tier(d, cfg):
    if d >= cfg["tier_excellent"]:
        return "excelente"
    if d >= cfg["tier_great"]:
        return "muito boa"
    return "boa"


FLOOR_PRICE = 25   # minStartPrice do leilao


def close_outcome(final_price, final_bids, log_row):
    """Classifica como um leilao monitorado fechou vs. o que esperavamos."""
    if final_price is None:
        return "expired"                       # expirou sem venda
    if final_price <= FLOOR_PRICE and (final_bids or 0) <= 1:
        return "floor"                         # arrematado no piso, sem disputa
    fair_lot = log_row["fair_lot"] or (log_row["fair"] or 0)
    if final_price <= (log_row["suggested_max"] or 0):
        return "flip"                          # fechou barato: dava pra revender
    if final_price <= fair_lot:
        return "use"                           # so' valia pra uso proprio
    return "expensive"                         # fechou caro: nao valia


_DEMOTE = {"excelente": "muito boa", "muito boa": "boa", "boa": "boa"}


def quality_tier(o, cfg):
    """Rotulo: boa / muito boa / excelente."""
    if o["kind"] == "gold":
        # pelo desconto que se espera PAGAR ao fechar (o preco sobe ate la),
        # nao pelo desconto que se ve agora
        exp = gold_curve(o["discount"], cfg["gold_curve_exp"])
        if exp >= cfg["gold_exp_excellent"]:
            base = "excelente"
        elif exp >= cfg["gold_exp_great"]:
            base = "muito boa"
        else:
            base = "boa"
        # preco do gold tambem cai (a faixa toda, nao so' o lote) - mesma
        # protecao contra tendencia que o equipamento ja tem
        if (o.get("trend") or 0) <= cfg["trend_down"]:
            base = _DEMOTE[base]
        return base

    base = _discount_tier(o["discount"], cfg)
    if o["kind"] == "gear":
        sc = o.get("attr_score", 0)
        if o.get("rarity", 0) >= 2 and sc == 0:
            base = "boa"                    # Raro+ com build sem valor: cautela
        elif sc >= cfg["attr_score_strong"]:
            base = _BUMP[base]              # build forte: sobe um nivel
    # mercado cheio (mais vendedores que compradores ate fechar): desce um nivel
    if o.get("supply_pressure", 0) > cfg["oversupply_pressure"]:
        base = _DEMOTE[base]
    # preco em queda: o "desconto" e' parte real, desce um nivel
    if (o.get("trend") or 0) <= cfg["trend_down"]:
        base = _DEMOTE[base]
    return base


def sell_verdict(fair, per_day, pressure, trend, cfg):
    """Vale anunciar agora ou segurar? -> (rotulo, estado)."""
    if fair <= 26:
        return "~25 (no piso)", "floor"          # ja no minimo, tanto faz
    if (trend or 0) <= cfg["trend_down"]:
        return "vender logo - preco caindo", "good"
    if per_day < 0.5:
        return "pouca procura", "slow"
    if pressure <= 1.2:
        return "bom momento", "good"
    if pressure <= cfg["flip_max_pressure"]:
        return "ok", "mid"
    return "segurar - mercado cheio", "hold"


def start_price(bids_median, p25, median):
    """Preco inicial sugerido pra anunciar: se o mercado costuma dar lance
    pode comecar mais baixo (p25); senao vende no inicial (mediana)."""
    if bids_median >= 2:
        return max(25, round(p25)), "o mercado costuma dar lance - pode comecar mais baixo"
    return max(25, round(median)), "costuma vender no preco inicial - nao comece abaixo do valor"


def next_list_window(now, hours):
    """(agora?, quando) - proxima janela (hora local) pra ANUNCIAR de modo que
    o leilao (>= 6h) feche no horario quente."""
    t = time.localtime(now / 1000)
    if t.tm_hour in hours:
        return True, now
    day = 0 if t.tm_hour < min(hours) else 1
    nxt = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + day, min(hours), 0, 0, 0, 0, -1))
    return False, int(nxt * 1000)


def nz_from_gear_key(key):
    """Reconstroi o essencial de normalize() a partir da chave gravada
    'gear:nome|t{tier}|{attrs}|u{up}|f{ftier}'."""
    parts = key.split("|")
    return {"key": key, "kind": "gear", "name": parts[0].split(":", 1)[1],
            "tier": int(parts[1][1:]), "qty": None, "gold_amount": None,
            "up": int(parts[3][1:]), "ftier": int(parts[4][1:]) if len(parts) > 4 else 0,
            "attrs": parse_attrs(key), "unit_price": None}


def item_type(info):
    """Tipo como no filtro do market: weapon1h/weapon2h/helmet/armor/legs/
    boots/shield/amulet/ring/trinket (sem slot = trinket)."""
    slot = (info or {}).get("slot")
    if slot == "weapon":
        return "weapon2h" if info.get("twoHanded") else "weapon1h"
    return slot or "trinket"


def _csv_set(text, conv):
    return {conv(x) for x in (text or "").split(",") if x.strip()}


def bounty_filters(bounty):
    """Filtros de uma busca salva, ja convertidos em conjuntos."""
    return {"term": (bounty["name"] or "").strip().lower(),
            "rarities": _csv_set(bounty["rarities"], int),
            "kinds": _csv_set(bounty["kinds"], str),
            "vocs": _csv_set(bounty["vocs"], str),
            "classes": _csv_set(bounty["classes"], int),
            "attrs": _csv_set(bounty["attrs"], int)}


def _bounty_ok(f, nz, table):
    """A oferta bate com os filtros? Raridade/tipo/vocacao/classe/atributos so'
    fazem sentido em equipamento."""
    if f["term"] and f["term"] not in (nz["name"] or "").lower():
        return False
    gear_only = (f["rarities"] or f["kinds"] or f["vocs"] or f["classes"]
                 or f["attrs"])
    if gear_only and nz["kind"] != "gear":
        return False
    if f["rarities"] and (nz["tier"] or 0) not in f["rarities"]:
        return False
    if f["attrs"] and not f["attrs"] <= {i for i, _ in nz["attrs"]}:
        return False
    if f["kinds"] or f["vocs"] or f["classes"]:
        info = (table or {}).get(nz["name"])
        if f["kinds"] and item_type(info) not in f["kinds"]:
            return False
        if f["classes"] and (info or {}).get("class") not in f["classes"]:
            return False
        if f["vocs"]:
            if info is None:
                return False
            iv = info.get("vocs")            # sem lista = qualquer vocacao usa
            if iv and not f["vocs"] & set(iv):
                return False
    return True


def _type_refs(store, table, since):
    """{(tipo, raridade): [(ids de atributo, preco)]} das vendas recentes -
    referencia 'por tipo' pra anuncio sem historico do proprio item."""
    idx = {}
    for r in store.gear_sales_keys(since):
        info = table.get(r["name"])
        if info:
            idx.setdefault((item_type(info), r["tier"]), []).append(
                (frozenset(i for i, _ in parse_attrs(r["key"])), r["final_price"]))
    return idx


def _type_estimate(refs, info, nz, min_n=8):
    """Preco tipico de equipamento do mesmo tipo e raridade (de preferencia com
    os mesmos atributos). None se ha pouca venda. E' estimativa grosseira."""
    ref = refs.get((item_type(info), nz["tier"]), [])
    want = frozenset(i for i, _ in nz["attrs"])
    pool = [p for a, p in ref if want and want <= a]
    if len(pool) < 5:
        pool = [p for _, p in ref]
    return (statistics.median(pool), len(pool)) if len(pool) >= min_n else None


def match_bounty(bounty, active_rows, store, cfg, since, table=None,
                 limit=40, cap=150):
    """Ofertas ativas que batem com uma busca salva (nome, raridade, tipo,
    vocacao, classe, atributos - todos opcionais e combinaveis), cada uma
    avaliada como em find_opportunities mas sem os filtros de orcamento e de
    janela de tempo. Retorna (ofertas mostradas, total que bateu)."""
    f = bounty_filters(bounty)
    if not any(f.values()):
        return [], 0
    rake = cfg["rake_pct"]
    windows = supply_windows(active_rows)
    now = now_ms()
    cands = []
    for row in active_rows:
        nz = normalize(row)
        if _bounty_ok(f, nz, table):
            cands.append((row, nz))
    total = len(cands)
    cands.sort(key=lambda c: c[0]["endsAt"])
    out, refs = [], None
    for row, nz in cands[:cap]:
        unit, total_price = nz["unit_price"], row["currentPrice"]
        it = row.get("item") or {}
        m = {
            "auction_id": row["id"], "name": nz["name"], "kind": nz["kind"],
            "rarity": nz["tier"] if nz["kind"] == "gear" else None,
            "current_price": total_price, "unit_price": unit,
            "bid_count": row.get("bidCount", 0), "ends_at": row["endsAt"],
            "secs_left": round((row["endsAt"] - now) / 1000),
            "qty": it.get("qty"),
            "gold_millions": round((row.get("goldAmount") or 0) / 1e6) or None,
            "attrs_text": decode_attrs(nz["attrs"]) if nz["kind"] == "gear" else None,
            "has_hist": False, "good": False, "verdict": None, "tier": None,
            "trend": None,
        }
        val = value_item(nz, store, cfg, since) if unit else None
        if (not val and table and nz["kind"] == "gear" and not nz["up"]
                and not nz.get("ftier")):
            if refs is None:
                refs = _type_refs(store, table, since)
            est = _type_estimate(refs, table.get(nz["name"]) or {}, nz)
            if est:                # sem historico do item: so' referencia (nao vira "boa")
                fair, n = est
                m.update({
                    "has_hist": True, "est": True, "fair": fair,
                    "fair_lot": round(fair), "n": n, "basis": "tipo",
                    "suggested_max": round(fair * (1 - cfg["discount_threshold"])),
                    "breakeven": round(fair * (1 - rake)),
                    "discount": 1 - unit / fair})
        if val:
            fair = val["fair"]
            lot = total_price / unit if unit else 1
            discount = 1 - unit / fair if fair else 0
            is_gold = nz["kind"] == "gold"
            thr = (cfg["gold_discount_threshold"] if is_gold
                   else cfg["discount_threshold"])
            ends = windows.get(supply_key(nz), [])
            win_ms = cfg["flip_horizon_hours"] * 3_600_000
            closing_win = max(sum(1 for e in ends if e - now <= win_ms) - 1, 0)
            demand_win = val["per_day"] * cfg["flip_horizon_hours"] / 24
            pressure = round(closing_win / max(demand_win, 0.3), 1)
            trend = val.get("trend")
            flip_ok = (pressure <= cfg["flip_max_pressure"]
                       and val["per_day"] >= cfg["flip_min_liquidity"]
                       and (trend or 0) > cfg["trend_down"])
            m.update({
                "has_hist": True, "fair": fair, "fair_lot": round(fair * lot),
                "suggested_max": round(fair * (1 - (cfg["gold_use_min_discount"]
                                                     if is_gold else thr)) * lot),
                "breakeven": round(fair * (1 - rake) * lot),
                "upg": upgrade_econ(nz, fair, store, cfg, since) if nz["kind"] == "gear" else None,
                "discount": discount, "n": val["n"], "basis": val["basis"],
                "sales_per_day": round(val["per_day"], 2),
                "supply_pressure": pressure, "trend": trend,
                "verdict": "revender" if (flip_ok and not is_gold) else "usar",
                "good": discount >= thr,
            })
            # pra USAR, nao vale pagar mais do que custaria upar voce mesmo
            m["use_max"] = round(min(fair, m["upg"]["diy"]) * lot) if m["upg"] else round(fair * lot)
            if m["good"]:
                m["tier"] = quality_tier({
                    "kind": nz["kind"], "discount": discount,
                    "est_saving": round((fair - unit) * lot),
                    "rarity": nz["tier"] if nz["kind"] == "gear" else 0,
                    "attr_score": (attr_score(nz["attrs"], cfg)
                                   if nz["kind"] == "gear" else 0),
                    "supply_pressure": pressure, "trend": trend,
                }, cfg)
        out.append(m)
    out.sort(key=lambda x: (not x["good"], x["secs_left"]))
    return out[:limit], total
