"""Tabela estatica de itens do jogo (slot / nivel exigido / vocacoes),
extraida do bundle JS do site e cacheada em items.json."""
import json
import re
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / "items.json"
SITE = "https://baiakidle.com"
UA = "baiak-market-alert/1.0 (rastreador pessoal; somente leitura)"

_MEM = None


def _fetch_bundle():
    req = urllib.request.Request(SITE + "/", headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    m = re.search(r"assets/index-[A-Za-z0-9_-]+\.js", html)
    if not m:
        raise RuntimeError("bundle nao encontrado no HTML")
    url = f"{SITE}/{m.group(0)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _match_brace(text, start):
    """Indice logo apos o '}' que fecha o '{' em text[start]."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise RuntimeError("chave nao balanceada")


def _extract_classes(js):
    # o nome da variavel minificada muda a cada build do site (era d0, virou
    # J0); acha o mapa pela assinatura do conteudo em vez do nome
    m = re.search(r'const \w+=new Map\(\[\["[^"]+",\d\]', js)
    if not m:
        return {}
    i = m.start()
    obj = js[i:js.find("])", i) + 2]
    return {mm.group(1): int(mm.group(2))
            for mm in re.finditer(r'\["([^"]+)",(\d)\]', obj)}


def _split_top_level(obj):
    """Divide um objeto JS (sem as chaves externas) nos pares top-level
    'chave:{...}', pulando conteudo aninhado (skills/absorb/imb de cada item).
    A chave vem entre aspas ("naga axe") ou, quando e' um identificador
    valido de uma palavra so (soulhexer), sem aspas - a minificacao remove
    aspas desnecessarias e um regex so' de chave-com-aspas perde esses itens."""
    entries = []
    i, n = 0, len(obj)
    while i < n:
        while i < n and obj[i] in ", \n\t":
            i += 1
        if i >= n:
            break
        if obj[i] == '"':
            j = i + 1
            while obj[j] != '"' or obj[j - 1] == "\\":
                j += 1
            key, i = obj[i + 1:j], j + 1
        else:
            j = i
            while j < n and obj[j] not in ":,":
                j += 1
            key, i = obj[i:j], j
        if i >= n or obj[i] != ":":
            i += 1
            continue
        i += 1
        if i >= n or obj[i] != "{":
            depth = 0                          # valor nao e' objeto: pula ate a virgula top-level
            while i < n and not (depth == 0 and obj[i] == ","):
                if obj[i] in "{[":
                    depth += 1
                elif obj[i] in "}]":
                    depth -= 1
                i += 1
            continue
        body_end = _match_brace(obj, i) - 1
        entries.append((key, obj[i + 1:body_end]))
        i = body_end + 1
    return entries


# estatisticas base do item (ataque/defesa/armadura/combate/etc), pra exibir
# nos detalhes expandidos do painel
_NUM_FIELDS = ("atk", "def", "arm", "elementAtk", "range", "hitChance",
               "hpRegen", "mpRegen", "durationSec", "wandMin", "wandMax",
               "manaShot", "critDmg", "critChance", "moveSpeed", "lifeLeech",
               "manaLeech", "charges", "reflect", "maxHitChance", "cleave",
               "mShieldFlat", "mShieldPct")
_STR_FIELDS = ("wt", "elementType", "ammoType")
_DICT_FIELDS = ("skills", "absorb", "magicEl")


def _num(s):
    try:
        return int(s)
    except ValueError:
        return float(s)


def _extract(js):
    anchor = js.find('"naga axe":{id:')
    if anchor < 0:
        raise RuntimeError("dicionario de itens nao encontrado")
    start = js.rfind('={"', 0, anchor) + 1
    obj = js[start:_match_brace(js, start)]

    items = {}
    for name, body in _split_top_level(obj):
        info = {}
        idm = re.search(r"(?:^|,)id:(\d+)", body)
        slot = re.search(r'slot:"([^"]+)"', body)
        lvl = re.search(r"(?:^|,)level:(\d+)", body)
        vocs = re.search(r"vocs:\[([^\]]*)\]", body)
        if idm:
            # id do objeto no jogo - usado pra montar a URL do icone
            # (api/things/object/<id>.png, a mesma que o site usa no market)
            info["id"] = int(idm.group(1))
        if slot:
            info["slot"] = slot.group(1)
        if lvl:
            info["level"] = int(lvl.group(1))
        if vocs:
            info["vocs"] = re.findall(r'"([a-z]+)"', vocs.group(1))

        for key in _NUM_FIELDS:
            m = re.search(rf"(?:^|,){key}:(-?[\d.]+)", body)
            if m:
                info[key] = _num(m.group(1))
        for key in _STR_FIELDS:
            m = re.search(rf'{key}:"([^"]+)"', body)
            if m:
                info[key] = m.group(1)
        if "twoHanded:!0" in body:
            info["twoHanded"] = True
        for key in _DICT_FIELDS:
            m = re.search(rf"{key}:\{{([^}}]*)\}}", body)
            if m:
                # ex.: absorb pode ter resistencia NEGATIVA (fraqueza), tipo
                # magma coat: {fire:8,ice:-8}
                info[key] = {k: _num(v) for k, v in
                            re.findall(r"(\w+):(-?[\d.]+)", m.group(1))}
        imb = re.search(r"imb:\{slots:(\d+)(?:,cats:\[([^\]]*)\])?", body)
        if imb:
            info["imb_slots"] = int(imb.group(1))
            cats = re.findall(r'"([^"]+)"', imb.group(2) or "")
            if cats:
                info["imb_cats"] = cats

        items[name] = info

    for name, cls in _extract_classes(js).items():
        if name in items:
            items[name]["class"] = cls
    return items


def load(max_age_days=7, force=False):
    if not force and CACHE.exists():
        if time.time() - CACHE.stat().st_mtime < max_age_days * 86400:
            try:
                return json.loads(CACHE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    try:
        data = _extract(_fetch_bundle())
        CACHE.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        print(f"[items] {len(data)} itens atualizados")
        return data
    except Exception as e:  # noqa: BLE001
        print(f"[items] falha ao atualizar: {e}")
        if CACHE.exists():
            return json.loads(CACHE.read_text(encoding="utf-8"))
        return {}


def get(max_age_days=7):
    global _MEM
    if _MEM is None:
        _MEM = load(max_age_days)
    return _MEM


EQUIP_SLOTS = {"weapon", "armor", "helmet", "shield", "amulet", "ring",
               "legs", "boots"}


def fits(name, chars, table):
    """True se `name` for equipamento que algum personagem de `chars`
    ({vocation, level}) consegue usar."""
    info = table.get(name)
    if not info or info.get("slot") not in EQUIP_SLOTS:
        return False
    req_vocs = info.get("vocs")
    req_lvl = info.get("level", 0)
    for c in chars:
        if req_vocs and c["vocation"] not in req_vocs:
            continue
        if (c["level"] or 0) < req_lvl:
            continue
        return True
    return False


if __name__ == "__main__":
    t = load(force=True)
    print(f"{len(t)} itens")
    for n in ("naga axe", "alicorn headguard", "platinum amulet", "life ring"):
        print(f"  {n}: {t.get(n)}")
