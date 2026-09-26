"""Logica compartilhada de coleta, deteccao de oportunidades e
acompanhamento de posicoes. Usada pelo CLI (run.py) e pelo servidor."""
import json
import math
import time
from pathlib import Path

import api
import analyze
import items
import notify

HERE = Path(__file__).parent

DEFAULTS = {
    "poll_seconds": 120,
    "track_poll_seconds": 10,
    "history_days": 7,
    "items_refresh_days": 7,
    "budget_min_coins": 25,
    "budget_max_coins": 500,
    "discount_threshold": 0.25,
    "tier_good": 0.25,
    "tier_great": 0.42,
    "tier_excellent": 0.60,
    "gold_discount_threshold": 0.15,
    "gold_budget_max_coins": 2000,
    "gold_min_saving_coins": 5,
    "gold_rate_halflife_h": 12,
    "gold_rate_min_lot": 150_000_000,
    "gold_use_min_discount": 0.10,
    "gold_exp_great": 0.14,
    "gold_exp_excellent": 0.18,
    # desconto detectado (10 min antes do fim) -> desconto medio que se paga ao
    # fechar / chance de fechar >= gold_use_min_discount (medido no historico)
    "gold_curve_exp": [[0, 0], [0.10, 0.075], [0.15, 0.10], [0.20, 0.12],
                       [0.30, 0.155], [0.40, 0.185], [0.50, 0.195],
                       [0.70, 0.17], [1.0, 0.15]],
    "gold_curve_p": [[0, 0.30], [0.10, 0.45], [0.15, 0.51], [0.20, 0.56],
                     [0.25, 0.62], [0.35, 0.65], [0.50, 0.69], [0.70, 0.65],
                     [1.0, 0.58]],
    "gold_snap_window_min": 30,
    "gold_snap_keep_days": 60,
    "min_sales": 5,
    "min_sales_loose": 3,
    "min_sales_per_day": 1.0,
    "attr_weights": {
        "1": 2, "4": 3, "5": 1, "6": 2, "9": 1, "10": 2,
        "18": 2, "19": 1.5, "25": 1, "26": 1.5
    },
    "attr_level_factor": 0.15,
    "attr_level_mult": {"10": {"4": 1.5, "5": 1.65, "6": 2.4, "7": 3.5, "8": 3.8,
                               "9": 4.5, "10": 6.0, "11": 8.0, "12": 10.0}},
    "gem_gold": 15_000_000,
    "exp_upgrade_gems": {"2": 4, "3": 7, "4": 10, "5": 14, "6": 17, "7": 20, "8": 33,
                         "9": 45, "10": 55, "11": 85, "12": 115},
    "craft_days": 30,
    "craft_min_upgraded": 3,
    "craft_min_profit": 15,
    "craft_min_p": 0.6,
    "attr_model_ids": [10, 4, 6, 19, 5, 9, 1, 18, 26],
    "attr_model_days": 14,
    "attr_model_min_group": 3,
    "basket_widen_days": [14, 30],
    "attr_model_ridge": 1.0,
    "attr_model_iters": 15,
    "attr_model_hours": 1,
    "gear_fair_calib": 0.70,
    "boss_token_name": "boss token",
    "boss_tokens_per_addon": 500,
    "boss_addon_coins": 50,
    "boss_min_margin": 0.2,
    "boss_leftover_credit": 1.0,
    "boss_recent_n": 20,
    "boss_min_ev": 5,
    "boss_show_n": 10,
    "attr_adj_min": 0.6,
    "attr_adj_max": 1.8,
    "attr_score_strong": 6.0,
    "min_bids": 0,
    "min_seconds_left": 90,
    "alert_window_seconds": 600,
    "flash_min_rarity": 4,
    "flash_min_gap_coins": 20,
    "flash_max_seconds": 60,
    "watch_alert_seconds": 300,
    "flip_horizon_hours": 8,
    "oversupply_pressure": 3.5,
    "flip_max_pressure": 2.0,
    "flip_min_liquidity": 1.0,
    "sell_list_hours": [14, 15, 16, 17],
    "trend_min_recent": 4,
    "trend_down": -0.10,
    "trend_up": 0.10,
    "backfill_pages": 200,
    "refresh_pages": 8,
    "rake_pct": 0.10,
    "listing_fee_gold": 5_000_000,
    "deduct_listing_fee": True,
    "watchlist": [],
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "http_port": 8787,
    "db_path": str(HERE / "market.db"),
}


def load_config():
    cfg = dict(DEFAULTS)
    path = HERE / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


def collect(cfg, store, verbose=False):
    """Puxa vendas novas do historico e o snapshot de leiloes ativos."""
    first = store.get_setting("backfilled") != "1"
    max_pages = cfg["backfill_pages"] if first else cfg["refresh_pages"]
    inserted = 0
    for pg in range(1, max_pages + 1):
        data = api.history(page=pg, per_page=100)
        rows = data.get("rows", [])
        if not rows:
            break
        page_new = sum(store.add_sale(row, analyze.normalize(row)) for row in rows)
        inserted += page_new
        store.commit()
        if pg >= math.ceil(data.get("total", 0) / 100):
            break
        if not first and page_new == 0:
            break                       # pagina inteira ja conhecida: alcancou
        time.sleep(0.4)
    if first:
        store.set_setting("backfilled", "1")

    active = list(api.browse_all())
    _update_gold_median(cfg, store)
    if verbose:
        tail = "  (backfill inicial completo)" if first else ""
        print(f"[collect] +{inserted} vendas | {len(active)} leiloes ativos{tail}")
    return active


def _update_gold_median(cfg, store):
    since = analyze.now_ms() - cfg["history_days"] * 86_400_000
    st = analyze.stats(store.gold_sales_all(since))   # so' pra converter a taxa de listagem
    if st:
        store.set_setting("gold_median", st["median"])


def gold_fee_coins(cfg, store):
    """Taxa de listagem (gold) convertida em coins pela cotacao atual."""
    if not cfg["deduct_listing_fee"]:
        return 0
    rate = float(store.get_setting("gold_median", 0) or 0)  # coins por 1M gold
    return round(rate * cfg["listing_fee_gold"] / 1_000_000)


_SNAP_LAST = {}     # auction_id -> (preco, lances, balde de 5 min): so' grava mudanca


def snap_gold(cfg, store, active):
    """Retrato dos leiloes de gold nos ultimos minutos: preco, lances e tempo
    restante ao longo do tempo. Junto com o preco final (sales), e' o dado pra
    aprender a chance de um leilao fechar abaixo de um teto."""
    now = analyze.now_ms()
    win = cfg["gold_snap_window_min"] * 60_000
    rows, live = [], set()
    for r in active:
        if r.get("type") != "gold" or r["endsAt"] - now > win:
            continue
        live.add(r["id"])
        state = (r["currentPrice"], r.get("bidCount", 0), (r["endsAt"] - now) // 300_000)
        if _SNAP_LAST.get(r["id"]) != state:
            _SNAP_LAST[r["id"]] = state
            rows.append((r["id"], now, r["currentPrice"], r.get("bidCount", 0),
                         r["endsAt"], r.get("goldAmount")))
    for aid in [a for a in _SNAP_LAST if a not in live]:
        del _SNAP_LAST[aid]
    if rows:
        store.add_gold_snaps(rows)
    store.prune_gold_snaps(now - cfg["gold_snap_keep_days"] * 86_400_000)


def run_scan(cfg, store, active=None, notify_new=True):
    """Coleta -> encontra oportunidades -> grava/atualiza -> notifica."""
    if active is None:
        active = collect(cfg, store)
    snap_gold(cfg, store, active)
    opps = analyze.find_opportunities(active, store, cfg)
    for o in opps:
        store.upsert_opportunity(o)
        store.log_opportunity(o)              # registra a 1a deteccao (INSERT OR IGNORE)
    by_id = {o["auction_id"]: o for o in opps}
    store.prune_opportunities(by_id.keys(), analyze.now_ms())
    _close_finished_logs(cfg, store)

    if notify_new:
        keep = _char_filter(cfg, store)
        for row in store.unnotified_opportunities():
            o = by_id.get(row["auction_id"])
            if not o or (keep is not None and not keep(o)):
                continue
            notify.send(cfg, "OPORTUNIDADE\n" + notify.format_opp(o))
            store.mark_notified(row["auction_id"])
    return opps


def _close_finished_logs(cfg, store):
    """Fecha analises cujo leilao ja terminou: pega o preco final das vendas
    (auction.history) ou marca como expirado se nao vendeu."""
    now = analyze.now_ms()
    for log in store.pending_opp_logs(now - 300_000):     # 5 min de folga
        sale = store.get_sale(log["auction_id"])
        if sale:
            fp, bids = sale["final_price"], sale["bid_count"]
            store.close_opp_log(log["auction_id"], fp, bids,
                                analyze.close_outcome(fp, bids, log))
        elif now - (log["ends_at"] or 0) > 3_600_000:      # 1h sem aparecer = expirou
            store.close_opp_log(log["auction_id"], None, None, "expired")
    store.prune_opp_log(now - 60 * 86_400_000)              # guarda 60 dias


def _char_filter(cfg, store):
    """Retorna uma funcao opp->bool se o modo 'personagens' estiver ativo."""
    if store.get_setting("filter_mode", "all") != "chars":
        return None
    chars = [dict(c) for c in store.list_characters() if c["active"]]
    table = items.get(cfg["items_refresh_days"])
    return lambda o: o["kind"] != "gold" and items.fits(o["name"], chars, table)


def refresh_opportunities(cfg, store):
    """Poll rapido: atualiza preco/lances das oportunidades ja detectadas
    consultando cada leilao individualmente (barato)."""
    now = analyze.now_ms()
    for r in store.list_opportunities(active_only=True):
        try:
            row = api.item(r["auction_id"])
        except RuntimeError:              # tRPC: anuncio nao encontrado -> sumiu
            store.drop_opportunity(r["auction_id"])
            continue
        except Exception:  # noqa: BLE001 - rede/timeout: tenta de novo no proximo ciclo
            continue
        if row.get("status") != "active" or (row.get("endsAt") or 0) <= now:
            log = store.get_opp_log(r["auction_id"])
            if log and log["closed_at"] is None:
                bids = row.get("bidCount")
                fp = row.get("currentPrice") if (bids or 0) > 0 else None
                store.close_opp_log(r["auction_id"], fp, bids,
                                    analyze.close_outcome(fp, bids, log))
            store.drop_opportunity(r["auction_id"])
            continue

        nz = analyze.normalize(row)
        unit, fair = nz["unit_price"], r["median"]
        if not unit or not fair:
            continue
        discount = 1 - unit / fair
        if discount < 0:                       # ja passou o preco justo
            store.drop_opportunity(r["auction_id"])
            continue

        is_gold = r["kind"] == "gold"
        thr = cfg["gold_discount_threshold"] if is_gold else cfg["discount_threshold"]
        lot = row["currentPrice"] / unit
        o = {**dict(r),
             "current_price": row["currentPrice"], "unit_price": unit,
             "bid_count": row.get("bidCount", 0), "ends_at": row["endsAt"],
             "discount": discount,
             "est_saving": round((fair - unit) * lot),
             "suggested_max": round(fair * (1 - (cfg["gold_use_min_discount"]
                                                  if is_gold else thr)) * lot)}
        o["tier"] = analyze.quality_tier(o, cfg)
        store.upsert_opportunity(o)


def refresh_watches(cfg, store):
    """Poll rapido dos leiloes que o usuario adicionou pra acompanhar: preco
    ao vivo + recalcula o preco justo (mais venda no historico pode destravar
    uma estimativa que faltava quando voce adicionou)."""
    now = analyze.now_ms()
    table = items.get(cfg["items_refresh_days"])
    for w in store.active_watch():
        try:
            row = api.item(w["auction_id"])
        except RuntimeError:
            store.update_watch(w["auction_id"], status="removido")
            continue
        except Exception:  # noqa: BLE001
            continue
        fields = {"current_price": row.get("currentPrice"),
                  "bid_count": row.get("bidCount"),
                  "ends_at": row.get("endsAt")}
        nz = analyze.normalize(row)
        val = analyze.value_for_watch(nz, store, cfg, table, now)
        if val:
            lot = (row.get("currentPrice", 0) / nz["unit_price"]) if nz.get("unit_price") else 1
            thr = (cfg["gold_discount_threshold"] if w["kind"] == "gold"
                   else cfg["discount_threshold"])
            fields.update(fair=val["fair"], fair_lot=round(val["fair"] * lot),
                         suggested_max=round(val["fair"] * (1 - thr) * lot),
                         est=int(val["est"]), basis=val["basis"], n=val["n"])
        if row.get("status") != "active" or (row.get("endsAt") or 0) <= now:
            fields["status"] = row.get("status") or "encerrado"
        elif (not w["alerted"]
              and (row.get("endsAt", now) - now) / 1000 <= cfg["watch_alert_seconds"]):
            fields["alerted"] = 1
            mins = round(cfg["watch_alert_seconds"] / 60)
            notify.send(cfg, f"ACOMPANHANDO - faltam ~{mins} min\n"
                        f"{w['name']}  #{w['auction_id']}\n"
                        f"  preco: {row.get('currentPrice')}  lances: {row.get('bidCount')}\n"
                        + (f"  preco justo: {w['fair_lot']:.0f}  revender ate: {w['suggested_max']}\n"
                           if w["fair_lot"] else "")
                        + "  https://baiakidle.com/#/market")
        store.update_watch(w["auction_id"], **fields)


def bounty_label(b):
    """Descricao curta de uma busca salva (nome + filtros) pros avisos."""
    bits = [b["name"]] if (b["name"] or "").strip() else []
    bits += [x for x in (b["kinds"] or "").split(",") if x]
    bits += [x for x in (b["vocs"] or "").split(",") if x]
    bits += [analyze.ATTR_NAME.get(int(x), x) for x in (b["attrs"] or "").split(",")
             if x.strip().isdigit()]
    return " ".join(bits) or "busca"


def refresh_bounties(cfg, store, active):
    """Cruza as buscas salvas com o snapshot ativo e avisa ofertas boas novas."""
    if not active:
        return
    since = analyze.now_ms() - cfg["history_days"] * 86_400_000
    now = analyze.now_ms()
    table = items.get(cfg["items_refresh_days"])
    for b in store.active_bounties():
        matches, _ = analyze.match_bounty(b, active, store, cfg, since, table)
        good = [m for m in matches if m["good"]]
        seen = set(json.loads(b["notified_ids"] or "[]"))
        live = {m["auction_id"] for m in matches}
        fields = {"last_checked": now}
        if good:
            fields["last_hit_at"] = now
        fresh = sorted((m for m in good if m["auction_id"] not in seen),
                       key=lambda m: -m["discount"])
        for m in fresh[:5]:                     # filtro largo nao vira spam
            notify.send(cfg, f"OFERTA ENCONTRADA - {bounty_label(b)}\n"
                        f"  {m['name']}  #{m['auction_id']}\n"
                        f"  preco: {m['current_price']}  ({m['discount'] * 100:.0f}% abaixo)"
                        f"  lances: {m['bid_count']}\n"
                        f"  revender ate: {m['suggested_max']}  usar ate: {m['fair_lot']:.0f}"
                        f"  ({m['verdict']})\n"
                        f"  encerra em {notify.fmt_secs(m['secs_left'])}\n"
                        f"  https://baiakidle.com/#/market")
        if len(fresh) > 5:
            print(f"[bounty] {bounty_label(b)}: +{len(fresh) - 5} ofertas boas (veja o painel)")
        new_seen = (seen & live) | {m["auction_id"] for m in good}
        fields["notified_ids"] = json.dumps(sorted(new_seen))
        store.update_bounty(b["id"], **fields)


def refresh_positions(cfg, store):
    """Fast poll: atualiza o estado do leilao de cada posicao aberta."""
    now = analyze.now_ms()
    for pos in store.open_auction_positions():
        try:
            row = api.item(pos["auction_id"])
        except Exception as e:  # noqa: BLE001
            print(f"[pos {pos['id']}] {e}")
            continue
        fields = {
            "last_auction_price": row.get("currentPrice"),
            "last_bid_count": row.get("bidCount"),
            "auction_ends_at": row.get("endsAt"),
        }
        if row.get("status") != "active":
            fields["auction_status"] = row.get("status") or "ended"
        elif row.get("endsAt", now) < now:
            fields["auction_status"] = "ended"
        store.update_position(pos["id"], **fields)
