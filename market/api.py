"""Cliente da API tRPC publica do market do Baiak Idle (somente leitura, GET)."""
import json
import math
import time
import urllib.parse
import urllib.request

BASE = "https://baiakidle.com/api/trpc/"
UA = "baiak-market-alert/1.0 (rastreador pessoal de precos; somente leitura)"


def _call(proc, inp, timeout=20):
    query = urllib.parse.urlencode({
        "batch": "1",
        "input": json.dumps({"0": inp}, separators=(",", ":")),
    })
    req = urllib.request.Request(
        f"{BASE}{proc}?{query}",
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    entry = payload[0]
    if "error" in entry:
        raise RuntimeError(f"{proc}: {entry['error'].get('message')}")
    return entry["result"]["data"]


def config():
    """Taxas e limites do leilao (rakePct, feeAmount, minStartPrice, ...)."""
    return _call("auction.config", {})


def item(auction_id):
    """Estado atual de um unico leilao (preco, lances, status, endsAt)."""
    return _call("auction.item", {"id": int(auction_id)}, timeout=8)


def history(page=1, per_page=100, q=None, type=None):
    """Vendas concluidas (status 'delivered'). Janela deslizante de ~7 dias."""
    inp = {"page": page, "perPage": per_page}
    if q:
        inp["q"] = q
    if type:
        inp["type"] = type
    return _call("auction.history", inp)


def browse(page=1, per_page=100, sort="ends_asc", **filters):
    """Leiloes ativos. filtros: type, q, kind, voc, classe, tier, bids."""
    inp = {"page": page, "perPage": per_page, "sort": sort}
    inp.update({k: v for k, v in filters.items() if v is not None})
    return _call("auction.browse", inp)


def browse_all(per_page=100, sleep=0.4):
    """Itera por todos os leiloes ativos, paginando."""
    page = 1
    while True:
        data = browse(page=page, per_page=per_page)
        rows = data.get("rows", [])
        for row in rows:
            yield row
        if not rows or page >= math.ceil(data.get("total", 0) / per_page):
            break
        page += 1
        time.sleep(sleep)
