"""Saida de alertas: sempre console; opcionalmente Telegram (bot API)."""
import urllib.parse
import urllib.request

from analyze import RARITY_PT


def fmt_secs(s):
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def format_opp(o):
    head = f"[{o['tier'].upper()}] {o['name']}"
    extra = ""
    if o["kind"] == "gold":
        price_line = (f"  {o['current_price']} coins   "
                      f"atual {o['unit_price'] * 100:.1f} coins/100M   "
                      f"taxa {o['median'] * 100:.1f} coins/100M")
        extra += f"\n  teto de lance p/ usar: {o['suggested_max']} coins"
    elif o["kind"] == "gear":
        head += f" [{RARITY_PT.get(o.get('rarity'), '?')}]"
        price_line = f"  preco atual: {o['current_price']} coins ({o['discount'] * 100:.0f}% abaixo)"
        if o.get("attrs_text"):
            extra += f"\n  atributos: {o['attrs_text']} (score {o.get('attr_score', 0)})"
        extra += (f"\n  revender ate: {o['suggested_max']} (empata {o.get('breakeven', '?')})"
                  f"   usar ate: {o['median']:.0f} (base: {o.get('basis')})")
    else:
        price_line = (f"  preco atual: {o['current_price']} coins ({o['discount'] * 100:.0f}% abaixo)"
                      f"   unidade {o['unit_price']:.3g} / media {o['median']:.3g}")
        extra += f"\n  revender ate: {o['suggested_max']} (empata {o.get('breakeven', '?')})"
    return (
        f"{head}  ->  #{o['auction_id']}\n"
        f"{price_line}{extra}\n"
        f"  economia est.: {o['est_saving']} coins   liquidez: "
        f"{o['sales_per_day']:.1f}/dia (n={o['n']})\n"
        f"  lances: {o['bid_count']}   encerra em {fmt_secs(o['secs_left'])}\n"
        f"  https://baiakidle.com/#/market"
    )


def send(cfg, text):
    print(text)
    token = cfg.get("telegram_bot_token")
    chat = cfg.get("telegram_chat_id")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10
        )
    except Exception as e:  # noqa: BLE001 - alerta nao pode derrubar o loop
        print(f"[telegram falhou] {e}")
