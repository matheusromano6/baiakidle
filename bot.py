import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

VERSION = "4.12.5"

# Cada "perfil" e um navegador diferente (Chrome ou Opera) - permite rodar 2
# instancias do bot ao mesmo tempo, cada uma numa conta/navegador diferente
# (ex: VPN so numa delas), sem uma pisar na porta/perfil/config da outra. O
# perfil "chrome" usa os mesmos nomes de arquivo de sempre (sem sufixo) pra
# nao quebrar instalacoes existentes; "opera" usa arquivos proprios.
BROWSER_PROFILES = {
    "chrome": {
        "label": "Google Chrome",
        "cdp_port": 9222,
        "profile_dir_name": "chrome_profile",
        "routines_filename": "routines.json",
        "settings_filename": "settings.json",
    },
    "opera": {
        "label": "Opera",
        "cdp_port": 9223,
        "profile_dir_name": "opera_profile",
        "routines_filename": "routines_opera.json",
        "settings_filename": "settings_opera.json",
    },
}
CURRENT_PROFILE = "chrome"  # navegador ativo nesta sessao - trocado via set_active_profile()


def set_active_profile(profile):
    global CURRENT_PROFILE
    if profile not in BROWSER_PROFILES:
        raise ValueError(f"Perfil de navegador desconhecido: {profile!r}")
    CURRENT_PROFILE = profile


def cdp_port():
    return BROWSER_PROFILES[CURRENT_PROFILE]["cdp_port"]


def cdp_url():
    return f"http://localhost:{cdp_port()}"


GAME_URL = "https://baiakidle.com/jogar/"
GAME_URL_PATTERN = "baiakidle"
BROWSER_LAUNCH_TIMEOUT_SECONDS = 20
CONNECT_GAME_PAGE_TIMEOUT_SECONDS = 20

DEFAULT_STEP_TIMEOUT_SECONDS = 3
REPEAT_CLICK_DELAY_SECONDS = 0.5  # folga entre cliques repetidos (passos "repetir enquanto aparecer")
MAX_REPEAT_CLICKS = 20  # limite de seguranca (quantidade) para passos "repetir enquanto aparecer"
MAX_REPEAT_SECONDS = 15  # limite de seguranca (tempo) para passos "repetir enquanto aparecer"

TICK_SECONDS = 1  # granularidade do loop principal / cadencia do gatilho "automatico"
RESTART_DELAY_SECONDS = 10  # espera antes de tentar reconectar apos perder a conexao com o navegador
HUNT_CHECK_INTERVAL_SECONDS = 30  # de quanto em quanto tempo 'ensure_active_hunt' confere se ha hunt ativa
# O jogo (provavelmente os anuncios) vaza memoria com o tempo - horas de sessao
# continua acumulam centenas de milhares de listeners/documentos "fantasma" e o
# processo do Chrome pode passar de 2GB numa unica aba. Um reload periodico
# devolve a aba ao consumo de memoria de uma sessao recem-aberta.
PAGE_RELOAD_INTERVAL_SECONDS = 1 * 3600  # de quanto em quanto tempo recarrega a pagina do jogo pra liberar memoria
# Tempo que o Mercado/leilao pode ficar aberto antes do bot fechar sozinho
# (ver dismiss_blocking_overlays) - da pro usuario usar ele na hora sem ser
# expulso; so fecha se ficar aberto continuamente mais que isso (sinal de
# que foi esquecido/travado, nao alguem navegando nele agora).
AUCTION_MODAL_GRACE_SECONDS = 2 * 60
# O jogo reaproveita o mesmo botao de confirmacao pra varias acoes (vender,
# entregar codex, desbloquear com gold...). Se o texto do botao no momento do
# clique contiver qualquer uma dessas palavras, o clique e recusado - nunca
# queremos confirmar um gasto de recurso sem intencao explicita do usuario.
DANGEROUS_CONFIRM_KEYWORDS = ("desbloquear", "comprar", "compra", "unlock", "buy", "purchase")

BOSS_FIGHT_MAX_SECONDS = 600  # limite de seguranca esperando a barra de vida do chefe sumir
BOSS_COOLDOWN_SECONDS = 13 * 3600  # chute de seguranca se nao conseguir ler o cooldown real
BOSS_BACKOFF_MARGIN_SECONDS = 60  # antecipa um pouco a proxima consulta (o jogo arredonda o texto do cooldown)
BOSS_RETRY_SECONDS = 60  # se o horario calculado chegar e AINDA nao tiver ninguem pronto, passa a tentar nesse ritmo
# (era 5min - CONFIRMADO no log real como causa de um "buraco cego" de ate 5min
# bem no momento mais valioso: a estimativa calculada podia vencer segundos ANTES
# da recarga de meia-noite baterem de verdade, entrar no modo de retentativa e so
# tentar de novo minutos depois dos chefes ja estarem prontos no jogo.)

# Memoria compartilhada entre o passo que manda pro treino e o que volta a cacar:
# guarda qual hunt estava ativa antes de treinar, pra saber aonde voltar depois.
TRAINING_MEMORY = {"waiting": False, "hunt_name": None}

# Desde quando o Mercado/leilao esta continuamente aberto (ver
# dismiss_blocking_overlays + AUCTION_MODAL_GRACE_SECONDS) - None quando
# fechado.
AUCTION_MODAL_MEMORY = {"first_seen_open": None}

# Guarda ate quando vale a pena consultar os chefes de novo. Quando a lista
# inteira marcada pelo usuario fica esgotada (nenhum pronto), nao faz sentido
# ficar consultando a cada poucos segundos por 13h - espera perto do horario
# em que o primeiro deve voltar a ficar pronto. Se esse horario chegar e AINDA
# assim nao tiver ninguem pronto (cooldown mudou, leitura errada, etc.), passa
# a tentar a cada BOSS_RETRY_SECONDS em vez de confiar cegamente numa nova
# estimativa longa que pode repetir o mesmo problema.
BOSS_MEMORY = {"next_check": 0.0, "display_next_check": 0.0, "missed_estimate": False}

# Amuleto trocado pro Stone Skin (ver equip_boss_amulet) fica ate o FIM de
# toda a sequencia de chefes prontos, nao so' do chefe que precisou dele -
# chefes que nao precisam de 'stone_skin' no meio da sequencia simplesmente
# nao mexem no amuleto (nem trocam, nem revertem). 'changed' vazio = amuleto
# ja no padrao (nada trocado); preenchido = precisa reverter pro que guarda
# aqui assim que a sequencia acabar (ou for interrompida).
BOSS_AMULET_MEMORY = {"changed": {}}

# Placar de vitorias por chefe ({nome: kills}), lido direto do Bosstiary
# (Cyclopedia > Bosstiary - o proprio jogo ja conta certinho, nao precisa o
# bot adivinhar se um combate foi vitoria ou derrota). Usado pra mostrar na
# tela de escolha de chefes, ajudando o usuario a decidir manter ou tirar um
# chefe da lista pela eficiencia real.
BOSS_KILLS_MEMORY = {}

# Lembra a ultima hunt que ja tivemos as entradas de Codex favoritadas, pra nao
# ficar repetindo a busca/favoritar toda vez - so refaz quando a hunt muda.
FAVORITE_MEMORY = {"last_hunt": None}

# Lembra se ja avisamos (som) que a hunt favoritada completou 100% no Codex -
# so avisa uma vez por hunt, nao fica tocando repetido enquanto ela continua
# em 100% nos ciclos seguintes.
COMPLETION_MEMORY = {"notified_hunt": None}

# Lista de "conquistas" recentes (task de guild entregue, build atualizada,
# Codex completo) pra GUI listar num painel de Atividades Recentes - em vez
# de um alerta bloqueante pedindo OK. Cada item: {"message", "timestamp"}.
ACTIVITY_MEMORY = {"items": []}
MAX_ACTIVITY_ITEMS = 50


def record_activity(message):
    """Registra uma 'conquista' (task entregue, build atualizada, Codex
    completo) na lista que a GUI exibe no painel de Atividades Recentes."""
    ACTIVITY_MEMORY["items"].append({"message": message, "timestamp": time.time()})
    if len(ACTIVITY_MEMORY["items"]) > MAX_ACTIVITY_ITEMS:
        del ACTIVITY_MEMORY["items"][:-MAX_ACTIVITY_ITEMS]

# Guarda o horario (time.time()) do ultimo clique bem-sucedido de cada rotulo
# de passo (ex: "Vender tudo") - a GUI le isso pra mostrar status tipo
# "ultima venda: ha 3min", sem precisar reler o log inteiro.
LAST_ACTION_MEMORY = {}

# Flag global (persistida em settings.json) que liga/desliga o som de
# conquista tocado por 'play_achievement_sound()' - tanto pro Codex quanto
# pras tasks da guild.
SOUND_MEMORY = {"enabled": True}

# Memoria da rotina de Tasks da Guild: 'previous_hunt' guarda a hunt que
# estava ativa antes do bot trocar pra caçar uma task (pra poder voltar
# sozinho quando todas as tasks selecionadas estiverem concluidas/entregues);
# 'grinding' marca se essa troca ja aconteceu nesta sessao. 'grinding_since'
# (monotonic) marca desde quando - usado como trava de seguranca (ver
# GUILD_TASK_GRINDING_MAX_SECONDS/ensure_active_hunt): 'grinding' so vira
# False de novo quando a volta pra hunt padrao realmente da certo, entao uma
# falha persistente nessa volta (ex: erro pontual de clique) deixava a flag
# presa e bloqueava TAMBEM o mecanismo generico de 'garantir hunt ativa' -
# CONFIRMADO como causa real de personagem ficar parado na cidade sem ser
# redirecionado, mesmo com hunt padrao configurada.
GUILD_TASK_MEMORY = {"previous_hunt": None, "grinding": False, "grinding_since": None}
GUILD_TASK_GRINDING_MAX_SECONDS = 20 * 60

# Memoria da rotina de Bestiary: 'last_hunt' evita ficar reabrindo o Cyclopedia
# toda hora - so refaz a marcacao de rastreio quando a hunt muda de verdade.
# 'monsters' guarda os monstros dessa hunt (lidos da lista de Hunts) e
# 'already_complete' quais deles ja estavam 100% no Bestiary (nao precisam de
# contador ao vivo pra saber que estao prontos).
BESTIARY_MEMORY = {"last_hunt": None, "monsters": [], "already_complete": set()}

# Flag global (persistida em settings.json) que liga/desliga o avanco
# automatico pra proxima hunt da lista quando Codex E Bestiary da hunt atual
# estiverem 100%. Comeca desligada de proposito - trocar de hunt pode exigir
# trocar runas/magias, o que o bot ainda nao faz sozinho.
ADVANCE_MEMORY = {"enabled": False}

# ids de rotina que devem rodar AGORA no proximo tick do loop principal,
# ignorando o intervalo normal dela - a GUI usa isso quando uma configuracao
# muda enquanto o bot ja esta rodando (ex: Build Automatica) e a mudanca deve
# valer na hora, sem esperar o proximo intervalo (que pode ser de varios
# minutos).
FORCE_RUN_NOW = set()

# Evita tentar avancar de novo (repetidamente) a partir da mesma hunt - so
# tenta uma vez ate a hunt realmente mudar.
HUNT_PROGRESS_MEMORY = {"advanced_from": None}

# Pergunta de confirmacao pendente pro avanco de hunt (so usada quando
# ADVANCE_MEMORY esta ligado): o bot preenche 'current_hunt'/'next_hunt' e
# deixa 'answer' em None; a GUI (rodando na thread principal) fica de olho
# nisso e mostra um popup 'Ir para <next_hunt>?' - True/False em 'answer' e a
# resposta do usuario. So o AVANCO fica esperando - chefes, tasks de guild e
# venda continuam normais enquanto isso.
HUNT_ADVANCE_CONFIRM = {"current_hunt": None, "next_hunt": None, "answer": None}

# Guarda a ultima hunt que realmente estava ativa (nome nao-vazio lido de
# '#wave-title'). Se o personagem ficar sem hunt por qualquer motivo
# transitorio (chefe, troca de painel, erro de leitura pontual...), o bot
# tenta voltar pra ESSA hunt primeiro - so cai pra 'primeira disponivel da
# lista' se nunca esteve em hunt nenhuma (ex: primeira vez rodando o bot).
LAST_KNOWN_HUNT_MEMORY = {"name": None}

# Hunt 'padrao' escolhida pelo usuario (persistida em settings.json, via
# HuntPicker na GUI) - quando definida, tem prioridade sobre
# LAST_KNOWN_HUNT_MEMORY/GUILD_TASK_MEMORY['previous_hunt']: ao terminar
# tasks de guild ou ficar sem hunt ativa (ex: logo apos um chefe), o bot vai
# sempre pra ESSA hunt, em vez de tentar adivinhar "a de antes". Vazia =
# mantem o comportamento antigo (volta pra ultima hunt conhecida).
DEFAULT_HUNT_MEMORY = {"name": ""}


def resource_dir():
    """Pasta do executavel (.exe no Windows, .app no Mac) ou do script, para
    achar routines.json/settings.json ao lado dele.

    No Mac, 'sys.executable' de um app empacotado fica DENTRO do pacote
    (Algo.app/Contents/MacOS/Algo) - usar isso direto faria o bot procurar
    (e criar) routines.json escondido la dentro, em vez de ao lado do .app
    como o usuario espera (visivel no Finder, sobrevive a um build novo).
    Sobe 3 niveis (MacOS -> Contents -> Algo.app -> pasta que contem o .app)
    nesse caso."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if sys.platform == "darwin" and os.path.basename(exe_dir) == "MacOS":
            return os.path.dirname(os.path.dirname(os.path.dirname(exe_dir)))
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Pasta onde o bot GRAVA seus proprios dados (routines/settings/perfil
    do navegador/logs) - separada de onde o executavel/app esta instalado.

    No Mac, arrastar o app pra /Applications (o normal) deixa 'resource_dir()'
    apontando pra la - e um usuario comum NAO tem permissao de escrita
    nessa pasta sem autenticar como administrador. CONFIRMADO como causa
    real do app 'nao abrir' (crashava tentando criar routines.json ali,
    sem terminal nenhum pra mostrar o erro) e so funcionar rodando via
    Terminal com sudo. Usa a pasta padrao do macOS pra dados de app por
    usuario (~/Library/Application Support/<nome>), sempre gravavel sem
    precisar de admin. Windows e modo dev continuam usando a pasta do
    proprio executavel/script, como sempre foi (nunca teve esse problema
    la - o instalador nao manda o usuario pra uma pasta protegida)."""
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/BaiakIdleBot")
        os.makedirs(base, exist_ok=True)
        return base
    return resource_dir()


def profile_dir():
    return os.path.join(data_dir(), BROWSER_PROFILES[CURRENT_PROFILE]["profile_dir_name"])


def find_browser_executable(profile=None):
    """Procura o executavel do navegador (Chrome ou Opera, conforme o perfil
    ativo) nos locais usuais de cada sistema operacional."""
    profile = profile or CURRENT_PROFILE
    system = platform.system()

    if profile == "opera":
        # Opera "normal" e Opera GX sao instalacoes separadas (pastas e
        # executaveis diferentes) - procura as duas, ambas funcionam igual
        # via linha de comando (mesmos flags, e Chromium por baixo).
        if system == "Windows":
            candidates = [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\launcher.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\launcher.exe"),
                r"C:\Program Files\Opera\opera.exe",
                r"C:\Program Files\Opera GX\opera.exe",
                r"C:\Program Files (x86)\Opera\opera.exe",
                r"C:\Program Files (x86)\Opera GX\opera.exe",
            ]
        elif system == "Darwin":
            candidates = [
                "/Applications/Opera.app/Contents/MacOS/Opera",
                os.path.expanduser("~/Applications/Opera.app/Contents/MacOS/Opera"),
                "/Applications/Opera GX.app/Contents/MacOS/Opera GX",
                os.path.expanduser("~/Applications/Opera GX.app/Contents/MacOS/Opera GX"),
            ]
        else:
            candidates = ["/usr/bin/opera", "/usr/bin/opera-gx"]
    else:
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


def is_debug_port_open():
    try:
        urllib.request.urlopen(f"{cdp_url()}/json/version", timeout=1)
        return True
    except (urllib.error.URLError, OSError):
        return False


def mark_chrome_profile_clean(profile_dir):
    """Edita a 'Preferences' do perfil dedicado do Chrome marcando a ultima
    sessao como fechada normalmente. Sem isso, depois de qualquer fechamento
    abrupto (processo morto, PC dormindo/reiniciando, etc.) o Chrome mostra
    um dialogo/infobar de 'Restaurar paginas?' na proxima abertura - que pode
    atrasar ou travar a conexao via depuracao remota e, pra quem esta vendo a
    janela, parece uma tela em branco ate alguem clicar em algo manualmente."""
    prefs_path = os.path.join(profile_dir, "Default", "Preferences")
    if not os.path.exists(prefs_path):
        return  # perfil novinho, nunca teve sessao - nada a marcar
    try:
        with open(prefs_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        profile = data.setdefault("profile", {})
        profile["exit_type"] = "Normal"
        profile["exit_code"] = 0
        with open(prefs_path, "w", encoding="utf-8") as file:
            json.dump(data, file)
    except Exception:
        pass  # so um cuidado a mais - nao vale travar o launch por causa disso


def launch_browser(log=print):
    """Abre o navegador do perfil ativo (CURRENT_PROFILE: Chrome ou Opera) ja
    apontado pro jogo, com depuracao remota ligada.

    Usa um perfil proprio (profile_dir()) porque o navegador recusa abrir a
    porta de depuracao no perfil padrao por seguranca. Retorna True se, ao
    final, a porta de depuracao esta respondendo (ja estivesse aberta ou nao).
    """
    label = BROWSER_PROFILES[CURRENT_PROFILE]["label"]
    if is_debug_port_open():
        log(f"{label} ja esta com a depuracao remota ativa.")
        return True

    browser_path = find_browser_executable()
    if not browser_path:
        log(
            f"Nao encontrei o {label} instalado automaticamente. Abra manualmente com "
            f"--remote-debugging-port={cdp_port()} --user-data-dir=<pasta dedicada> {GAME_URL}"
        )
        return False

    target_dir = profile_dir()
    os.makedirs(target_dir, exist_ok=True)
    mark_chrome_profile_clean(target_dir)
    log(f"Abrindo o {label} ({browser_path})...")
    subprocess.Popen(
        [
            browser_path,
            f"--remote-debugging-port={cdp_port()}",
            f"--user-data-dir={target_dir}",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            GAME_URL,
        ]
    )

    deadline = time.monotonic() + BROWSER_LAUNCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_debug_port_open():
            log(f"{label} pronto.")
            return True
        time.sleep(0.5)

    log(f"O {label} demorou demais para abrir a porta de depuracao.")
    return False


# Raridades por 'data-tier' (atributo real do HTML do jogo). Numero -> nome.
# Confirmado ao vivo: bate com as cores --rar-N do CSS do proprio jogo.
RARITY_TIERS = [
    {"name": "Incomum (verde)", "tier": 1, "enabled": True},
    {"name": "Raro (azul)", "tier": 2, "enabled": True},
    {"name": "Epico (roxo/rosa)", "tier": 3, "enabled": True},
    {"name": "Lendario (amarelo)", "tier": 4, "enabled": True},
    {"name": "Mitico (vermelho)", "tier": 5, "enabled": True},
]

# Os 32 atributos de raridade do jogo (wiki: baiakidle.com/#/wiki/tiers-raridade).
# So aparecem em itens tier >= 1 (Uncommon+), no tooltip, numa secao separada
# de 'Bonuses' - cada linha e tipo 'Mana Leech +0.4% (Lv.1)'. O usuario pode
# marcar quais valem a pena guardar mesmo que o item seja de uma raridade que
# ele nao escolheu pra separar por cor (ex: quer guardar qualquer item com
# 'Exp', seja Uncommon ou Mythical). 'min_level' e o nivel minimo do atributo
# no item pra contar (Lv.1 ja conta se min_level=1).
RARITY_ATTRIBUTES = [
    {"name": "Weapon Attack", "enabled": False, "min_level": 1},
    {"name": "Crit Chance", "enabled": False, "min_level": 1},
    {"name": "Crit Damage", "enabled": False, "min_level": 1},
    {"name": "Onslaught", "enabled": False, "min_level": 1},
    {"name": "Attack Speed", "enabled": False, "min_level": 1},
    {"name": "Frenzy", "enabled": False, "min_level": 1},
    {"name": "Execute", "enabled": False, "min_level": 1},
    {"name": "Spell Damage", "enabled": False, "min_level": 1},
    {"name": "Fire Damage", "enabled": False, "min_level": 1},
    {"name": "Earth Damage", "enabled": False, "min_level": 1},
    {"name": "Energy Damage", "enabled": False, "min_level": 1},
    {"name": "Ice Damage", "enabled": False, "min_level": 1},
    {"name": "Holy Damage", "enabled": False, "min_level": 1},
    {"name": "Death Damage", "enabled": False, "min_level": 1},
    {"name": "Defense", "enabled": False, "min_level": 1},
    {"name": "Armor", "enabled": False, "min_level": 1},
    {"name": "Protect Fire", "enabled": False, "min_level": 1},
    {"name": "Protect Earth", "enabled": False, "min_level": 1},
    {"name": "Protect Energy", "enabled": False, "min_level": 1},
    {"name": "Protect Ice", "enabled": False, "min_level": 1},
    {"name": "Protect Holy", "enabled": False, "min_level": 1},
    {"name": "Protect Death", "enabled": False, "min_level": 1},
    {"name": "Protect All", "enabled": False, "min_level": 1},
    {"name": "Life Leech", "enabled": False, "min_level": 1},
    {"name": "Mana Leech", "enabled": False, "min_level": 1},
    {"name": "Max HP", "enabled": False, "min_level": 1},
    {"name": "Max Mana", "enabled": False, "min_level": 1},
    {"name": "HP Regen", "enabled": False, "min_level": 1},
    {"name": "MP Regen", "enabled": False, "min_level": 1},
    {"name": "Spell Healing", "enabled": False, "min_level": 1},
    {"name": "Loot", "enabled": True, "min_level": 1},
    {"name": "Exp", "enabled": True, "min_level": 1},
]

# Niveis de dificuldade das Tasks da Guild (Social > Guild > Tasks). O usuario
# escolhe quais niveis o bot deve aceitar/realizar, igual a lista de chefes.
GUILD_TASK_DIFFICULTIES = [
    {"key": "facil", "label": "Fácil", "card_class": "gwt-easy", "enabled": True},
    {"key": "media", "label": "Média", "card_class": "gwt-medium", "enabled": True},
    {"key": "dificil", "label": "Difícil", "card_class": "gwt-hard", "enabled": True},
]

# Config por vocacao da build automatica (arvore de talentos). Cada vocacao e
# independente porque cada personagem da conta pode querer um foco diferente.
# 'mode'/'focus' usam os mesmos values do site otimizador (baiakidle-build-
# optimizer.pages.dev): mode em dps/tank/offtank/pvp, focus em
# elemental_weapon/physical. 'min_points': so reaplica quando o personagem tiver
# pelo menos esse tanto de pontos disponiveis pra nao ficar reimportando a
# mesma build a cada 1 ponto novo (o usuario escolhe o intervalo).
DEFAULT_BUILD_CONFIGS = [
    {"vocation": "Knight", "enabled": False, "mode": "dps", "focus": "", "secondary_focus": "", "tertiary_focus": "", "force_xp": False, "force_loot": False, "min_points": 5},
    {"vocation": "Paladin", "enabled": False, "mode": "dps", "focus": "", "secondary_focus": "", "tertiary_focus": "", "force_xp": False, "force_loot": False, "min_points": 5},
    {"vocation": "Sorcerer", "enabled": False, "mode": "dps", "focus": "", "secondary_focus": "", "tertiary_focus": "", "force_xp": False, "force_loot": False, "min_points": 5},
    {"vocation": "Druid", "enabled": False, "mode": "dps", "focus": "", "secondary_focus": "", "tertiary_focus": "", "force_xp": False, "force_loot": False, "min_points": 5},
    {"vocation": "Monk", "enabled": False, "mode": "dps", "focus": "", "secondary_focus": "", "tertiary_focus": "", "force_xp": False, "force_loot": False, "min_points": 5},
]

DEFAULT_BOSSES = [
    {"name": 'Shadowpelt', "level": 5, "enabled": False},
    {"name": 'The Blazing Rose', "level": 5, "enabled": False},
    {"name": 'Darkfang', "level": 15, "enabled": False},
    {"name": 'Bloodback', "level": 15, "enabled": False},
    {"name": 'The Lily of Night', "level": 15, "enabled": False},
    {"name": 'The Diamond Blossom', "level": 15, "enabled": False},
    {"name": 'Black Vixen', "level": 20, "enabled": False},
    {"name": 'Sharpclaw', "level": 20, "enabled": False},
    {"name": 'Leiden', "level": 25, "enabled": False},
    {"name": 'Utua Stone Sting', "level": 35, "enabled": False},
    {"name": 'Brokul', "level": 50, "enabled": False},
    {"name": 'Amenef the Burning', "level": 50, "enabled": False},
    {"name": 'Solid Frozen Horror', "level": 50, "enabled": False},
    {"name": 'Ahau', "level": 60, "enabled": False},
    {"name": 'Irgix The Flimsy', "level": 60, "enabled": False},
    {"name": 'Unaz the Mean', "level": 60, "enabled": False},
    {"name": 'Vok the Freakish', "level": 60, "enabled": False},
    {"name": 'Tanjis', "level": 60, "enabled": False},
    {"name": 'Kusuma', "level": 60, "enabled": False},
    {"name": 'Brain Head', "level": 70, "enabled": False},
    {"name": 'Neferi the Spy', "level": 70, "enabled": False},
    {"name": 'Sister Hetai', "level": 70, "enabled": False},
    {"name": 'Rakesh Moonfang', "level": 70, "enabled": False},
    {"name": 'Scarlett Etzel', "level": 80, "enabled": False},
    {"name": 'The Time Guardian', "level": 80, "enabled": False},
    {"name": 'Dragonking Zyrtarch', "level": 80, "enabled": False},
    {"name": 'Lloyd', "level": 80, "enabled": False},
    {"name": 'Mounted Thorn Knight', "level": 80, "enabled": False},
    {"name": 'Foreshock', "level": 80, "enabled": False},
    {"name": 'Sir Nictros', "level": 80, "enabled": False},
    {"name": 'The Moonsnow Magnolia', "level": 80, "enabled": False},
    {"name": 'Megasylvan Yselda', "level": 90, "enabled": False},
    {"name": 'Obujos', "level": 90, "enabled": False},
    {"name": 'Drume', "level": 90, "enabled": False},
    {"name": 'Vladrukh', "level": 90, "enabled": False},
    {"name": 'Timira the Many-Headed', "level": 90, "enabled": False},
    {"name": 'Grand Master Oberon', "level": 100, "enabled": False},
    {"name": 'Ratmiral Blackwhiskers', "level": 100, "enabled": False},
    {"name": 'Faceless Bane', "level": 100, "enabled": False},
    {"name": 'Lady Tenebris', "level": 100, "enabled": False},
    {"name": 'Duke Krule', "level": 100, "enabled": False},
    {"name": 'Rupture', "level": 100, "enabled": False},
    {"name": 'Anomaly', "level": 100, "enabled": False},
    {"name": 'Ghulosh', "level": 110, "enabled": False},
    {"name": 'Lokathmor', "level": 110, "enabled": False},
    {"name": 'Mazzinor', "level": 110, "enabled": False},
    {"name": 'The Dread Maiden', "level": 110, "enabled": False},
    {"name": 'Count Vlarkorth', "level": 110, "enabled": False},
    {"name": 'The Brainstealer', "level": 110, "enabled": False},
    {"name": 'Earl Osam', "level": 110, "enabled": False},
    {"name": 'Lord Azaram', "level": 110, "enabled": False},
    {"name": 'Alptramun', "level": 110, "enabled": False},
    {"name": 'Jaul', "level": 120, "enabled": False},
    {"name": 'Shulgrax', "level": 130, "enabled": False},
    {"name": 'Dragon Pack', "level": 130, "enabled": False},
    {"name": 'Court Warlock', "level": 130, "enabled": False},
    {"name": 'Outburst', "level": 130, "enabled": False},
    {"name": 'Razzagorn', "level": 130, "enabled": False},
    {"name": 'Tarbaz', "level": 130, "enabled": False},
    {"name": 'Arbaziloth', "level": 130, "enabled": False},
    {"name": 'Gorzindel', "level": 130, "enabled": False},
    {"name": 'The Monster', "level": 130, "enabled": False},
    {"name": 'Prince Drazzak', "level": 140, "enabled": False},
    {"name": 'Lord Retro', "level": 140, "enabled": False},
    {"name": 'Magma Bubble', "level": 140, "enabled": False},
    {"name": 'Ragiaz', "level": 140, "enabled": False},
    {"name": 'Plagirath', "level": 140, "enabled": False},
    {"name": 'Urmahlullu the Immaculate', "level": 140, "enabled": False},
    {"name": 'The Fear Feaster', "level": 150, "enabled": False},
    {"name": 'The Unwelcome', "level": 150, "enabled": False},
    {"name": 'The Primal Menace', "level": 150, "enabled": False},
    {"name": 'The Pale Worm', "level": 150, "enabled": False},
    {"name": 'The Rootkraken', "level": 150, "enabled": False},
    {"name": 'Eradicator', "level": 160, "enabled": False},
    {"name": 'World Devourer', "level": 160, "enabled": False},
    {"name": 'King Zelos', "level": 160, "enabled": False},
    {"name": 'The Scourge of Oblivion', "level": 180, "enabled": False},
    {"name": 'Goshnar\'s Cruelty', "level": 190, "enabled": False},
    {"name": 'The Last Lore Keeper', "level": 200, "enabled": False},
    {"name": 'Goshnar\'s Greed', "level": 200, "enabled": False},
    {"name": 'The Nightmare Beast', "level": 210, "enabled": False},
    {"name": 'Mitmah Vanguard', "level": 210, "enabled": False},
    {"name": 'Ichgahal', "level": 210, "enabled": False},
    {"name": 'Goshnar\'s Hatred', "level": 220, "enabled": False},
    {"name": 'Goshnar\'s Malice', "level": 220, "enabled": False},
    {"name": 'Goshnar\'s Spite', "level": 220, "enabled": False},
    {"name": 'Bonelord\'s Phylactery', "level": 230, "enabled": False},
    {"name": 'Ferumbras Mortal Shell', "level": 230, "enabled": False},
    {"name": 'Fatal Bug', "level": 230, "enabled": False},
    {"name": 'The Gravedigger', "level": 230, "enabled": False},
    {"name": 'Ice Horror', "level": 230, "enabled": False},
    {"name": 'Eldritch Dragon Lord', "level": 240, "enabled": False},
    {"name": 'Chagorz', "level": 240, "enabled": False},
    {"name": 'Murcion', "level": 240, "enabled": False},
    {"name": 'Vemiath', "level": 240, "enabled": False},
    {"name": 'Goshnar\'s Megalomania', "level": 240, "enabled": False},
    {"name": 'Bakragore', "level": 340, "enabled": False},
    {"name": 'Mimar Haffar', "level": 360, "enabled": False},
    {"name": 'Maior Domus', "level": 360, "enabled": False},
    {"name": 'Phosphorus', "level": 400, "enabled": False},
]

def routines_path():
    return os.path.join(data_dir(), BROWSER_PROFILES[CURRENT_PROFILE]["routines_filename"])


# As rotinas padrao usam passos 'dom_click'/'dom_tier_sort': interagem com a
# propria pagina (por id/atributo do HTML), nao com print de tela. Isso as deixa
# identicas em qualquer monitor/resolucao/zoom, sem precisar calibrar nada.
DEFAULT_ROUTINES = [
    {
        "id": "enfrentar_chefes",
        "name": "Enfrentar Chefes",
        "enabled": False,
        "skip_when_training": True,
        "trigger": {"mode": "interval", "seconds": 300},
        "steps": [
            {
                "type": "dom_boss_fight",
                "open_selector": "#wave-title",
                "boss_menu_selector": '.tp-opt[data-tp="boss"]',
                "ready_selector": ".pick-leanbtn.ready",
                "row_selector": ".boss-cell",
                "name_selector": ".boss-cell-name",
                "go_selector": ".boss-fight",
                "bosses": [dict(b) for b in DEFAULT_BOSSES],
            },
        ],
    },
    {
        # As tasks da guild resetam toda vez que o jogo libera novas (Diarias,
        # todo dia as 00:00) - em vez de agendar um horario fixo (ex: 00:30),
        # esta rotina fica de olho continuamente (a cada 30s) e ja aceita
        # assim que aparecer uma nova. Roda DEPOIS de 'Enfrentar Chefes' nesta
        # lista de propósito: como as rotinas de cada ciclo rodam em ordem e o
        # combate de chefe e sincrono (so libera a vez quando termina), chefes
        # sempre saem na frente quando os dois coincidem no mesmo ciclo.
        "id": "tarefas_guild",
        "name": "Tarefas da Guild",
        "enabled": False,
        "skip_when_training": True,
        # 'active_seconds': enquanto uma task estiver sendo realizada
        # (GUILD_TASK_MEMORY['grinding']), roda bem mais rapido - o intervalo
        # de 1800s (30min) e so pra quando nao ha nada pendente, pra nao ficar
        # abrindo o painel da guild a toa e atrapalhando a entrega/venda.
        "trigger": {"mode": "interval", "seconds": 1800, "active_seconds": 60},
        "steps": [
            {
                "type": "dom_guild_tasks",
                "social_selector": "#tab-social",
                "guild_selector": "#tab-guild",
                "tasks_tab_selector": '.gw-tab[data-lb="Tasks"]',
                "open_selector": "#wave-title",
                "hunts_selector": '.tp-opt[data-tp="hunts"]',
                "row_selector": ".stage-row",
                "name_selector": ".stage-name-line b",
                "mobs_selector": ".stage-mobs",
                "go_selector": ".stage-go",
                "difficulties": [dict(d) for d in GUILD_TASK_DIFFICULTIES],
            },
        ],
    },
    {
        "id": "bestiary_hunt",
        "name": "Bestiary da Hunt Atual",
        "enabled": False,
        "skip_when_training": True,
        "trigger": {"mode": "interval", "seconds": 60},
        "steps": [
            {
                "type": "dom_hunt_bestiary",
                "open_selector": "#wave-title",
                "hunts_selector": '.tp-opt[data-tp="hunts"]',
                "row_selector": ".stage-row",
                "name_selector": ".stage-name-line b",
                "go_selector": ".stage-go",
                "hunt_details_button_selector": ".stage-details",
                "hunt_details_modal_selector": "#hunt-details-modal",
                "hunt_details_close_selector": "#hunt-details-modal-close",
                "hunt_details_card_selector": ".hd-card",
                "hunt_details_card_name_selector": ".hd-card-name",
                "hunt_details_card_kills_selector": ".hd-card-kills",
                "hunt_details_track_selector": ".cyc-track",
                "bestiary_overlay_row_selector": "#bestiarytrack-overlay .bsk-row",
                "bestiary_overlay_name_selector": ".gtk-hunt-name",
                "bestiary_overlay_count_selector": ".gtk-count",
            },
        ],
    },
    {
        # entrega o Codex ANTES de vender - senao um item que servia pro Codex
        # pode ser vendido antes da entrega rodar (Entregar Codex sozinho era
        # bem mais lento que Vender Loot, entao a venda sempre ganhava na
        # frente). Fazendo os dois juntos, na mesma rotina, isso nao acontece.
        "id": "vender_loot",
        "name": "Entregar Codex e Vender Loot",
        "enabled": True,
        "skip_when_training": True,
        "gate_selector": "#sell-all",
        # e a rotina mais "pesada" (varios cliques/telas em sequencia -
        # Progressao > Codex > checkboxes > Entregar x2 > Fechar > Vender) -
        # loot/codex nao estragam esperando alguns segundos, entao 1s era
        # caro demais rodando o tempo todo. 3s corta boa parte do custo sem
        # perda real de responsividade.
        "trigger": {"mode": "interval", "seconds": 3},
        "steps": [
            {"type": "dom_click", "selector": "#tab-progressao", "label": "Progressao", "timeout": 3},
            {"type": "dom_click", "selector": "#tab-codex", "label": "Codex", "timeout": 3},
            {
                # GARANTE que o campo de busca do Codex esteja vazio antes de
                # entregar - texto residual (ex: de uma execucao anterior
                # interrompida no meio) filtra a lista e esconde entradas
                # que deveriam ser entregues, e a venda roda em seguida sem
                # ter entregado o que devia.
                "type": "dom_clear_search",
                "selector": ".cx-search",
            },
            {
                # ordena por 'Mais completo primeiro' - prioriza terminar
                # entradas quase completas em vez de espalhar 1 item em
                # varias entradas diferentes ao mesmo tempo.
                "type": "dom_ensure_select",
                "selector": ".cx-sort",
                "value": "fill-desc",
                "label": "Ordem do Codex (mais completo primeiro)",
            },
            {
                # favorita no Codex as entradas da hunt que estamos jogando agora,
                # pra entregar elas primeiro (com prioridade) e nao gastar tempo
                # com itens de outras hunts antes.
                "type": "dom_favorite_hunt",
                "hunt_selector": "#wave-title",
            },
            {
                "type": "dom_ensure_checked",
                "selector": 'label.cx-check:has-text("Entregáveis") input[type="checkbox"]',
                "label": "Entregaveis",
            },
            {
                "type": "dom_ensure_checked",
                "selector": 'label.cx-check:has-text("Esconder concluídos") input[type="checkbox"]',
                "label": "Esconder concluidos",
            },
            {
                "type": "dom_ensure_checked",
                "selector": 'label.cx-check:has-text("Esconder bloqueadas") input[type="checkbox"]',
                "label": "Esconder bloqueadas",
            },
            {
                "type": "dom_ensure_active",
                "selector": '.codex-side .codex-tab:has-text("Favoritos")',
                "label": "Menu Favoritos",
            },
            {
                "type": "dom_click",
                "selector": ".cx-give:not([disabled]):visible",
                "label": "Entregar (favoritos)",
                "repeat": True,
                "confirm_selector": "#confirm-yes",
                "timeout": 3,
                "force": True,
            },
            {
                # fica de olho se a hunt favoritada completou 100% no Codex e
                # toca um som de aviso - ja aproveita que a aba Favoritos esta
                # aberta nesse ponto, nao precisa navegar de novo.
                "type": "dom_watch_favorite",
            },
            {
                "type": "dom_ensure_active",
                "selector": '.codex-side .codex-tab:has-text("Todas")',
                "label": "Menu Todas",
            },
            {
                "type": "dom_click",
                "selector": ".cx-give:not([disabled]):visible",
                "label": "Entregar",
                "repeat": True,
                "confirm_selector": "#confirm-yes",
                "timeout": 3,
                "force": True,
            },
            {"type": "dom_click", "selector": "#codex-modal-close", "label": "Fechar Codex", "timeout": 3},
            {
                "type": "dom_click",
                "selector": "#sell-all",
                "label": "Vender tudo",
                "confirm_selector": "#confirm-yes",
                "timeout": 3,
                "skip_if_disabled": True,
            },
        ],
    },
    {
        "id": "separar_loot",
        "name": "Separar Loot",
        "enabled": False,
        "skip_when_training": True,
        # 'automatico' cai no TICK_SECONDS do loop principal (1s) - rodava o
        # tempo todo, quase sempre so pra concluir "nada pra mover". Loot
        # parado na pouch por 2s a mais nao faz diferenca nenhuma.
        "trigger": {"mode": "interval", "seconds": 2},
        "steps": [
            {
                "type": "dom_tier_sort",
                "match_mode": "rarity_only",
                "tiers": [dict(t) for t in RARITY_TIERS],
                "attributes": [dict(a) for a in RARITY_ATTRIBUTES],
            },
        ],
    },
    {
        "id": "enviar_treino",
        "name": "Enviar para Treino",
        "enabled": False,
        "trigger": {"mode": "interval", "seconds": 2},
        "steps": [
            {
                "type": "dom_threshold_click",
                "watch_selector": "#stamina-pct",
                "threshold": 20,
                "open_selector": "#wave-title",
                "option_selector": '.tp-opt[data-tp="exercise"]',
                "label": "Treino online",
                "remember_selector": "#wave-title",
            },
        ],
    },
    {
        "id": "voltar_cacar",
        "name": "Voltar a Cacar",
        "enabled": False,
        "trigger": {"mode": "interval", "seconds": 600},
        "steps": [
            {
                "type": "dom_resume_hunt",
                "watch_selector": "#stamina-pct",
                "threshold": 80,
                "open_selector": "#wave-title",
                "hunts_selector": '.tp-opt[data-tp="hunts"]',
                "row_selector": ".stage-row",
                "name_selector": ".stage-name-line b",
                "go_selector": ".stage-go",
            },
        ],
    },
    {
        # automacao de MAIOR risco do bot: gasta pontos de talento de verdade
        # (desfazer custa gold no jogo). Comeca desligada de proposito, e cada
        # vocacao tambem comeca desligada dentro do passo - o usuario liga
        # explicitamente as que quiser, depois de conferir o resultado.
        "id": "auto_build",
        "name": "Build Automatica (Arvore de Talentos)",
        "enabled": False,
        "skip_when_training": True,
        # checa a cada 5min se ja tem pontos suficientes (min_points, por
        # vocacao) pra valer a pena atualizar - o gatilho real e o ponto
        # disponivel (que so sobe com level up), nao o tempo em si.
        "trigger": {"mode": "interval", "seconds": 300},
        "steps": [
            {
                "type": "dom_auto_build",
                "open_selector": "#tab-progressao",
                "tree_tab_selector": "#tab-tree",
                "char_active_selector": ".tree-char",
                "pts_selector": ".tree-chip.pts",
                "spent_selector": ".tree-chip:not(.pts)",
                "import_btn_selector": ".tree-code-btn.import",
                "import_input_selector": ".tree-code-in",
                "load_btn_selector": ".tree-code-row .tree-code-btn",
                "msg_selector": ".tree-code-msg",
                "confirm_body_selector": "#confirm-modal-body",
                "confirm_yes_selector": "#confirm-yes",
                "configs": [dict(c) for c in DEFAULT_BUILD_CONFIGS],
            },
        ],
    },
]


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "rotina"


def _ensure_mandatory_vender_loot_steps(routines):
    """Migra um 'routines.json' salvo por uma versao anterior pra sempre ter
    os passos 'limpar busca do Codex' e 'ordenar por mais completo primeiro'
    na rotina de venda - isso e' padrao do bot, nao uma configuracao que
    cada instalacao/PC precisa ganhar na mao (ou que uma edicao manual de
    arquivo, feita com o bot ja rodando, pode acabar apagando ao salvar por
    cima). Insere o que faltar logo apos abrir o Codex. Retorna True se
    mudou algo, pra quem chamar saber que precisa salvar de volta."""
    routine = next((r for r in routines if r.get("id") == "vender_loot"), None)
    if routine is None:
        return False
    steps = routine.setdefault("steps", [])
    codex_idx = next((i for i, s in enumerate(steps) if s.get("selector") == "#tab-codex"), None)
    if codex_idx is None:
        return False

    changed = False
    insert_at = codex_idx + 1
    if not any(s.get("type") == "dom_clear_search" for s in steps):
        steps.insert(insert_at, {"type": "dom_clear_search", "selector": ".cx-search"})
        insert_at += 1
        changed = True
    if not any(s.get("type") == "dom_ensure_select" and s.get("selector") == ".cx-sort" for s in steps):
        steps.insert(insert_at, {
            "type": "dom_ensure_select",
            "selector": ".cx-sort",
            "value": "fill-desc",
            "label": "Ordem do Codex (mais completo primeiro)",
        })
        changed = True
    return changed


def load_routines():
    path = routines_path()
    if not os.path.exists(path):
        save_routines(DEFAULT_ROUTINES)
    with open(path, "r", encoding="utf-8") as file:
        routines = json.load(file)
    if _ensure_mandatory_vender_loot_steps(routines):
        save_routines(routines)
    return routines


def save_routines(routines):
    with open(routines_path(), "w", encoding="utf-8") as file:
        json.dump(routines, file, ensure_ascii=False, indent=2)


def settings_path():
    return os.path.join(data_dir(), BROWSER_PROFILES[CURRENT_PROFILE]["settings_filename"])


DEFAULT_SETTINGS = {"sound_enabled": True, "auto_advance_hunt": False, "default_hunt": "", "hunts_cache": []}


def load_settings():
    path = settings_path()
    if not os.path.exists(path):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return {**DEFAULT_SETTINGS, **data}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    with open(settings_path(), "w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=2)


def connect_game_page(playwright):
    """Conecta no Chrome (porta de depuracao) e acha a aba do jogo pela URL.

    'launch_browser' so espera a porta de depuracao RESPONDER, nao a
    navegacao pra URL do jogo terminar - num perfil novo/primeira abertura
    isso pode demorar alguns segundos a mais (o Chrome ainda esta de fato
    carregando a aba inicial). Sem retry aqui, essa corrida fazia a conexao
    falhar ('aba do jogo nao encontrada') mesmo com o Chrome funcionando
    perfeitamente - so precisava de mais um instante."""
    browser = playwright.chromium.connect_over_cdp(cdp_url())

    deadline = time.monotonic() + CONNECT_GAME_PAGE_TIMEOUT_SECONDS
    while True:
        for context in browser.contexts:
            for page in context.pages:
                if GAME_URL_PATTERN in page.url:
                    return page
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    label = BROWSER_PROFILES[CURRENT_PROFILE]["label"]
    raise RuntimeError(
        f"Aba do jogo nao encontrada (procurando '{GAME_URL_PATTERN}' na URL). "
        f"Abra o jogo no {label} iniciado com --remote-debugging-port={cdp_port()}."
    )


def execute_dom_click_step(page, step, stop_event, log):
    """Passo tipo 'dom_click': clica num elemento da propria pagina por seletor CSS,
    em vez de reconhecer imagem. Nao depende de resolucao de tela/zoom - o elemento
    e o mesmo em qualquer monitor. Suporta 'repeat' (clica enquanto o elemento
    existir, ex: varios itens do codex prontos pra entregar) e 'confirm_selector'
    (clica um segundo elemento logo depois, ex: o botao de confirmar do popup)."""
    selector = step["selector"]
    label = step.get("label", selector)
    confirm_selector = step.get("confirm_selector")
    timeout_ms = step.get("timeout", DEFAULT_STEP_TIMEOUT_SECONDS) * 1000
    # 'force': ignora a checagem de 'estavel' do Playwright (util pra botoes com
    # animacao de destaque - ex: brilho no item pronto pra entregar - que nunca
    # param de se mexer visualmente, mas continuam clicaveis de verdade).
    force = step.get("force", False)

    if step.get("skip_if_disabled"):
        # Verificacao barata (sem esperar/tentar clicar) se o elemento esta
        # desabilitado agora (ex: botao de vender em cooldown). Permite rodar
        # esse passo com um intervalo curto sem travar o agendador nem
        # poluir o log enquanto o cooldown nao acabou.
        try:
            is_disabled = page.eval_on_selector(selector, "el => !!el.disabled")
        except Exception:
            is_disabled = False
        if is_disabled:
            return True

    def click_confirm():
        if not confirm_selector:
            return
        # O jogo reaproveita o MESMO botao (#confirm-yes) pra confirmar varias
        # acoes diferentes (vender, entregar codex, desbloquear com gold...).
        # Antes de clicar, le o texto atual do botao e recusa se parecer uma
        # acao que gasta recurso (ex: "Desbloquear", "Comprar") - clicar as
        # cegas aqui poderia confirmar a acao errada por engano.
        try:
            confirm_el = page.wait_for_selector(confirm_selector, timeout=timeout_ms, state="visible")
        except Exception:
            log("  Confirmacao nao apareceu.")
            return
        text = (confirm_el.text_content() or "").strip()
        if any(word in text.lower() for word in DANGEROUS_CONFIRM_KEYWORDS):
            log(f"  BLOQUEADO por seguranca: botao de confirmacao diz '{text}' - nao e a acao esperada, nao clicado.")
            return
        try:
            confirm_el.click(timeout=timeout_ms)
            log("  Confirmado.")
        except Exception:
            log("  Confirmacao nao apareceu.")

    if step.get("repeat"):
        clicks = 0
        deadline = time.monotonic() + MAX_REPEAT_SECONDS
        while clicks < MAX_REPEAT_CLICKS and time.monotonic() < deadline and not stop_event.is_set():
            if page.query_selector(selector) is None:
                break
            log(f"  Clicando '{label}' ({clicks + 1})...")
            try:
                # clica pelo seletor (nao por uma referencia ja capturada): a pagina
                # recria os elementos a cada atualizacao (React), entao uma
                # referencia guardada de antes fica "presa" a um no que some.
                page.click(selector, timeout=timeout_ms, force=force)
            except Exception as error:
                log(f"  Erro ao clicar: {error}")
                break
            clicks += 1
            click_confirm()
            stop_event.wait(REPEAT_CLICK_DELAY_SECONDS)
        log(f"'{label}': {clicks} clique(s).")
        if clicks:
            LAST_ACTION_MEMORY[label] = time.time()
        return True

    try:
        page.click(selector, timeout=timeout_ms, force=force)
    except Exception:
        log(f"'{label}' nao encontrado.")
        return False
    log(f"'{label}' clicado.")
    LAST_ACTION_MEMORY[label] = time.time()
    click_confirm()
    return True


def execute_dom_ensure_checked_step(page, step, log):
    """Passo tipo 'dom_ensure_checked': garante que uma checkbox esteja marcada
    (ex: filtros 'Entregaveis' / 'Esconder concluidos' do Codex). So clica se
    ainda nao estiver marcada - nao desmarca se ja estiver certa."""
    selector = step["selector"]
    label = step.get("label", selector)
    try:
        checkbox = page.query_selector(selector)
        if checkbox is None:
            log(f"  Checkbox '{label}' nao encontrada.")
            return False
        if checkbox.is_checked():
            return True
        # alguns checkboxes customizados escondem o <input> real (tamanho quase
        # zero, opacidade 0) e mostram um <span> estilizado no lugar - clicar
        # direto no input nao funciona nesse caso. Clica no <label> pai, que e
        # onde o usuario realmente clicaria (funciona tambem se nao houver
        # <label> pai - ai clica no proprio input, igual antes).
        target_handle = checkbox.evaluate_handle("el => el.closest('label') || el")
        clickable = target_handle.as_element() or checkbox
        clickable.click(timeout=3000)
        log(f"  '{label}' marcada.")
    except Exception as error:
        log(f"  Erro ao marcar '{label}': {error}")
        return False
    return True


def execute_dom_ensure_active_step(page, step, log):
    """Passo tipo 'dom_ensure_active': garante que um botao de alternancia (ex:
    aba 'Todas' do Codex, filtro 'Prontos' dos Chefes) esteja marcado como
    ativo (classe CSS, por padrao 'on'). So clica se ainda nao estiver."""
    selector = step["selector"]
    active_class = step.get("active_class", "on")
    label = step.get("label", selector)
    try:
        el = page.query_selector(selector)
        if el is None:
            log(f"  '{label}' nao encontrado.")
            return False
        current_class = el.get_attribute("class") or ""
        if active_class in current_class.split():
            return True
        el.click(timeout=3000)
        log(f"  '{label}' ativado.")
    except Exception as error:
        log(f"  Erro ao ativar '{label}': {error}")
        return False
    return True


def execute_dom_ensure_select_step(page, step, log):
    """Passo tipo 'dom_ensure_select': garante que um <select> esteja num
    valor especifico (ex: ordenar o Codex por 'Mais completo primeiro' antes
    de entregar - entradas quase completas primeiro, entao terminam de
    verdade em vez de ficar so espalhando 1 item em varias). So mexe se
    ainda nao estiver nesse valor."""
    selector = step["selector"]
    value = step["value"]
    label = step.get("label", selector)
    try:
        el = page.query_selector(selector)
        if el is None:
            log(f"  '{label}' nao encontrado.")
            return False
        if el.input_value() == value:
            return True
        el.select_option(value, timeout=3000)
        log(f"  '{label}' ajustado.")
    except Exception as error:
        log(f"  Erro ao ajustar '{label}': {error}")
        return False
    return True


def is_codex_level_one(entry_name, hunt_name):
    """As entradas do Codex de uma hunt com varios estagios seguem o padrao
    'Dominio: <Hunt> I', 'II', 'III'... - o nivel 1 e a que termina com
    '<Hunt>' (hunt sem estagios) ou '<Hunt> I' (primeiro estagio). Uma vez que
    o nivel 1 e entregue, ele SOME da lista e so sobra 'II' (ou mais) - nesse
    caso essa funcao retorna False, pra nao tratar o estagio avancado como se
    fosse 'o Codex principal' da hunt."""
    lower_name = entry_name.strip().lower()
    lower_hunt = hunt_name.strip().lower()
    if not lower_hunt:
        return False
    return lower_name.endswith(lower_hunt) or lower_name.endswith(f"{lower_hunt} i")


def execute_dom_clear_search_step(page, step, log):
    """Passo tipo 'dom_clear_search': limpa um campo de busca/filtro (ex:
    '.cx-search' no Codex) se tiver algo digitado. GARANTE que nenhum texto
    residual (ex: de uma execucao anterior de 'dom_favorite_hunt'
    interrompida no meio, antes de chegar a limpar sozinha) fique filtrando
    a lista - CONFIRMADO como causa real de 'Entregar' nao achar entradas
    que deveriam aparecer (e a venda rodar em seguida sem ter entregado o
    que devia). Silencioso de proposito (sem log de erro) - o campo pode
    nao existir ainda nessa tela."""
    selector = step["selector"]
    try:
        field = page.query_selector(selector)
        if field is not None and (field.input_value() or ""):
            field.fill("", timeout=3000)
            page.wait_for_timeout(200)
    except Exception:
        pass
    return True


def execute_dom_favorite_hunt_step(page, step, log):
    """Passo tipo 'dom_favorite_hunt': no Codex, aba Hunts, favorita (estrela)
    E liga o Auto Collect ('.cx-ac', botao 'AC' - entrega essa entrada
    SOZINHO, sem depender do bot passar pela aba Favoritos) APENAS na entrada
    de NIVEL 1 da hunt atual (ex: hunt 'Behemoth' marca 'Dominio: Behemoth I',
    nao 'II'/'III' - ve 'is_codex_level_one'). Estagios mais avancados sao
    desfavoritados/desmarcados se estiverem (residuo de antes desta regra) -
    continuam sendo entregues normalmente pela aba 'Todas', so nao viram o
    alvo do monitoramento/som de conquista, que deve representar sempre o
    nivel 1 completo.

    Quando a hunt MUDA, antes de marcar a nova, tira o favorito/AC que
    sobrou da hunt ANTERIOR (busca pelo nome antigo e desmarca o que achar) -
    sem isso, favoritos de hunts velhas iam se acumulando, deixando a aba
    Favoritos cada vez maior (e mais lenta de checar) em vez de ficar so com
    o Codex da hunt atual. Assim a entrega prioriza sempre o que e da hunt
    que estamos jogando agora, em vez de gastar tempo em itens de outras
    hunts. So refaz quando a hunt muda de verdade (ve FAVORITE_MEMORY)."""
    hunt_selector = step["hunt_selector"]
    try:
        hunt_name = (page.eval_on_selector(hunt_selector, "el => el.textContent") or "").strip()
    except Exception:
        return True
    if not hunt_name:
        return True

    if FAVORITE_MEMORY.get("last_hunt") == hunt_name:
        return True  # ja favoritamos as entradas dessa hunt, nao precisa de novo

    category_selector = step.get("category_selector", '.codex-side .codex-tab:has-text("Hunts")')
    search_selector = step.get("search_selector", ".cx-search")
    entry_selector = step.get("entry_selector", ".cx-entry")
    entry_name_selector = step.get("entry_name_selector", ".cx-entry-name")
    star_selector = step.get("star_selector", ".cx-star")
    ac_selector = step.get("ac_selector", ".cx-ac")

    try:
        page.click(category_selector, timeout=3000)
    except Exception as error:
        log(f"  Erro ao abrir Hunts no Codex: {error}")
        return False

    previous_hunt = FAVORITE_MEMORY.get("last_hunt")
    if previous_hunt and previous_hunt != hunt_name:
        try:
            page.fill(search_selector, previous_hunt, timeout=3000)
            time.sleep(0.4)
            cleared = 0
            for entry in page.query_selector_all(entry_selector):
                name_el = entry.query_selector(entry_name_selector)
                name = (name_el.text_content() or "") if name_el else ""
                if previous_hunt.lower() not in name.lower():
                    continue
                star = entry.query_selector(star_selector)
                if star is not None and star.get_attribute("aria-pressed") == "true":
                    star.click(timeout=2000)
                    cleared += 1
                ac = entry.query_selector(ac_selector)
                if ac is not None and ac.get_attribute("aria-pressed") == "true":
                    ac.click(timeout=2000)
            if cleared:
                log(f"  {cleared} entrada(s) de '{previous_hunt}' desfavoritada(s)/tiradas do Auto Collect (trocou de hunt).")
        except Exception as error:
            log(f"  Erro ao limpar favoritos antigos de '{previous_hunt}': {error}")

    try:
        page.fill(search_selector, hunt_name, timeout=3000)
        time.sleep(0.4)
    except Exception as error:
        log(f"  Erro ao buscar '{hunt_name}' no Codex: {error}")
        return False

    favorited = 0
    unfavorited = 0
    found_level_one = False
    for entry in page.query_selector_all(entry_selector):
        name_el = entry.query_selector(entry_name_selector)
        name = (name_el.text_content() or "") if name_el else ""
        if hunt_name.lower() not in name.lower():
            continue
        star = entry.query_selector(star_selector)
        if star is None:
            continue
        is_starred = star.get_attribute("aria-pressed") == "true"
        level_one = is_codex_level_one(name, hunt_name)
        if level_one:
            found_level_one = True
        try:
            if level_one and not is_starred:
                star.click(timeout=2000)
                favorited += 1
            elif not level_one and is_starred:
                star.click(timeout=2000)
                unfavorited += 1
        except Exception as error:
            log(f"  Erro ao (des)favoritar entrada de '{hunt_name}': {error}")

        ac = entry.query_selector(ac_selector)
        if ac is not None:
            is_ac = ac.get_attribute("aria-pressed") == "true"
            try:
                if level_one and not is_ac:
                    ac.click(timeout=2000)
                elif not level_one and is_ac:
                    ac.click(timeout=2000)
            except Exception as error:
                log(f"  Erro ao ajustar Auto Collect de '{hunt_name}': {error}")

    if favorited:
        log(f"  {favorited} entrada(s) de '{hunt_name}' favoritada(s) e no Auto Collect (nivel 1).")
    if unfavorited:
        log(f"  {unfavorited} entrada(s) de nivel avancado de '{hunt_name}' desfavoritada(s) (entrega so pela aba Todas).")

    if not found_level_one and COMPLETION_MEMORY.get("notified_hunt") != hunt_name:
        # nao ha entrada de nivel 1 dessa hunt na busca - ou ja foi completada
        # e entregue antes do bot chegar a monitorar (ex: hunt reaproveitada),
        # ou essa hunt nao tem Codex proprio. Em qualquer um dos casos nao ha
        # nada pendente do Codex pra essa hunt - sem isso, o avanco automatico
        # de hunt e a troca pra tasks de guild ficavam travados pra sempre
        # esperando um 'Codex completo' que nunca ia disparar.
        log(f"  Sem entrada de nivel 1 de '{hunt_name}' no Codex - considerando o Codex dessa hunt resolvido.")
        COMPLETION_MEMORY["notified_hunt"] = hunt_name

    try:
        page.fill(search_selector, "", timeout=3000)
    except Exception:
        pass

    FAVORITE_MEMORY["last_hunt"] = hunt_name
    return True


def play_achievement_sound():
    """Toca a fanfarra classica do Windows ('Ta-Da', ja vem instalada por
    padrao) - som de vitoria de verdade, nao so um bipe. Se por algum motivo
    o arquivo nao existir nesse Windows, cai pra uma sequencia de bipes como
    reserva. So funciona no Windows - em outro SO, so nao faz nada. Respeita
    a flag global SOUND_MEMORY (usuario pode desligar o alerta sonoro na UI)."""
    if not SOUND_MEMORY.get("enabled", True):
        return
    if sys.platform != "win32":
        return
    try:
        import winsound

        tada_path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media", "tada.wav")
        if os.path.isfile(tada_path):
            winsound.PlaySound(tada_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        for frequency, duration_ms in ((523, 120), (659, 120), (784, 120), (1047, 220)):
            winsound.Beep(frequency, duration_ms)
    except Exception:
        pass


def execute_dom_watch_favorite_step(page, step, log):
    """Passo tipo 'dom_watch_favorite': fica de olho na entrada de NIVEL 1 do
    Codex da hunt atual (a mesma que 'dom_favorite_hunt' favorita - ve
    'is_codex_level_one') e, quando ela chegar a 100% de progresso, toca um
    som de aviso - assim da pra saber a hora de trocar de hunt sem ficar
    checando a tela manualmente. Se so sobrar um estagio avancado (II, III...)
    na busca - porque o nivel 1 ja foi entregue antes - nao considera isso
    'Codex completo': esse aviso deve representar sempre o nivel 1, nao
    qualquer estagio que aparecer sozinho depois dele. So avisa uma vez por
    hunt (nao fica tocando repetido)."""
    hunt_name = FAVORITE_MEMORY.get("last_hunt")
    if not hunt_name:
        return True  # nenhuma hunt favoritada ainda pra acompanhar

    if COMPLETION_MEMORY.get("notified_hunt") == hunt_name:
        return True  # ja avisou essa hunt, nao repete

    entry_selector = step.get("entry_selector", ".cx-entry")
    entry_name_selector = step.get("entry_name_selector", ".cx-entry-name")
    bar_selector = step.get("bar_selector", ".cx-bar-fill")

    for entry in page.query_selector_all(entry_selector):
        name_el = entry.query_selector(entry_name_selector)
        name = (name_el.text_content() or "").strip() if name_el else ""
        if not is_codex_level_one(name, hunt_name):
            continue
        bar = entry.query_selector(bar_selector)
        if bar is None:
            continue
        style = bar.get_attribute("style") or ""
        match = re.search(r"width:\s*([\d.]+)%", style)
        if not match:
            continue
        percent = float(match.group(1))
        if percent >= 99.5:
            log(f"  '{name}' completou 100% no Codex! Hora de trocar de hunt.")
            play_achievement_sound()
            COMPLETION_MEMORY["notified_hunt"] = hunt_name
            record_activity(f"Codex completo: '{name}' - pronto pra trocar de hunt.")
        break

    return True


ITEM_ATTR_PATTERN = re.compile(r'class="tt-attr">([^<(]+?)\s+[+-][\d.,]+%?\s*\(Lv\.(\d+)\)')


def parse_item_attributes(tiphtml):
    """Le os atributos de RARIDADE de um item (secao 'Attributes:' do tooltip,
    formato 'Nome +X% (Lv.N)') - devolve lista de (nome, nivel). So conta o que
    tem '(Lv.N)' no texto - os 'Bonuses' fixos do item (ex: protecao elemental
    base do slot, sempre presentes) nao tem esse sufixo e ficam de fora."""
    return [(name.strip(), int(level)) for name, level in ITEM_ATTR_PATTERN.findall(tiphtml or "")]


def execute_dom_tier_sort_step(page, step, stop_event, log):
    """Passo tipo 'dom_tier_sort': move pro backpack (shift+clique) todo item da
    Loot Pouch que bater com o criterio escolhido em 'match_mode':
    - 'rarity_only' (padrao): raridade (atributo 'data-tier') entre as ativadas.
    - 'attribute_only': tem algum dos atributos escolhidos (ex: 'Exp', 'Loot')
      no nivel minimo configurado, INDEPENDENTE da raridade.
    - 'rarity_and_attribute': raridade ativada E pelo menos 1 dos atributos
      escolhidos no nivel minimo - os dois juntos, nao um ou outro.
    Raridade e lida do atributo 'data-tier'; atributos, do tooltip
    ('data-tiphtml', ve 'parse_item_attributes')."""
    match_mode = step.get("match_mode", "rarity_only")
    enabled_tiers = {t["tier"] for t in step["tiers"] if t.get("enabled", True)}
    attr_rules = {a["name"].lower(): a.get("min_level", 1) for a in step.get("attributes", []) if a.get("enabled")}

    if match_mode == "rarity_only" and not enabled_tiers:
        log("  Nenhuma raridade ativa neste passo (modo 'Raridade'), pulando.")
        return True
    if match_mode == "attribute_only" and not attr_rules:
        log("  Nenhum atributo ativo neste passo (modo 'Atributo'), pulando.")
        return True
    if match_mode == "rarity_and_attribute" and (not enabled_tiers or not attr_rules):
        log("  Modo 'Raridade + Atributo' precisa de pelo menos 1 raridade E 1 atributo marcados, pulando.")
        return True

    grid_selector = step.get("grid_selector", "#inv-grid")
    cell_selector = step.get("cell_selector", ".cell[data-tier]")
    tiphtml_attr = step.get("tiphtml_attr", "data-tiphtml")
    full_selector = f"{grid_selector} {cell_selector}"

    processed = 0
    deadline = time.monotonic() + MAX_REPEAT_SECONDS
    while time.monotonic() < deadline and not stop_event.is_set():
        # le TODAS as celulas (tier + tooltip) numa unica chamada - a Loot
        # Pouch pode ter dezenas de itens, e ler cada uma com
        # 'cell.get_attribute(...)' era uma ida-e-volta pelo protocolo de
        # depuracao POR ATRIBUTO POR CELULA (ate 2x por item aqui, modo
        # 'Raridade + Atributo') - mesmo problema ja visto e corrigido nas
        # listas de Chefes/Hunts. Isso fazia 'Separar Loot' (que roda o
        # tempo todo) demorar segundos so pra concluir "nada pra mover".
        cells_data = page.evaluate(
            """([sel, tiphtmlAttr]) => Array.from(document.querySelectorAll(sel)).map(el => ({
                tier: el.getAttribute('data-tier'),
                tiphtml: el.getAttribute(tiphtmlAttr),
            }))""",
            [full_selector, tiphtml_attr],
        )

        target_index = None
        reason = None
        for index, cell_data in enumerate(cells_data):
            tier_attr = cell_data.get("tier")
            tier = int(tier_attr) if tier_attr is not None else None
            tier_match = tier is not None and tier in enabled_tiers

            matched_attr = None
            if match_mode != "rarity_only" and attr_rules:
                item_attrs = parse_item_attributes(cell_data.get("tiphtml"))
                matched_attr = next(
                    (name for name, level in item_attrs if name.lower() in attr_rules and level >= attr_rules[name.lower()]),
                    None,
                )

            if match_mode == "rarity_only":
                is_match = tier_match
                if is_match:
                    reason = f"raridade tier {tier}"
            elif match_mode == "attribute_only":
                is_match = matched_attr is not None
                if is_match:
                    reason = f"atributo '{matched_attr}'"
            else:  # rarity_and_attribute
                is_match = tier_match and matched_attr is not None
                if is_match:
                    reason = f"raridade tier {tier} + atributo '{matched_attr}'"

            if is_match:
                target_index = index
                break
        if target_index is None:
            break

        # so busca o ElementHandle de verdade (pra clicar) AGORA que sabemos
        # qual indice - evita pegar um handle por celula so pra descartar a
        # maioria. Se a grade mudou entre a leitura e aqui (raro - outra
        # rotina/o proprio jogo re-renderizou), so tenta de novo no proximo
        # ciclo em vez de arriscar clicar na celula errada.
        cells = page.query_selector_all(full_selector)
        if target_index >= len(cells):
            break
        target = cells[target_index]

        log(f"  Movendo item ({reason}) para o backpack...")
        try:
            target.click(modifiers=["Shift"], timeout=3000)
        except Exception as error:
            log(f"  Item sumiu antes do clique, tentando o proximo ({error}).")
            continue
        processed += 1
        stop_event.wait(REPEAT_CLICK_DELAY_SECONDS)

    log(f"Separar loot: {processed} item(ns) movido(s).")
    return True


def execute_dom_threshold_click_step(page, step, log):
    """Passo tipo 'dom_threshold_click': le um numero/percentual de um elemento
    (ex: stamina) e, quando ele cai ate o limite configurado, abre um menu e
    clica numa opcao dele (ex: mandar o personagem pro treino online). So
    dispara uma vez ao CRUZAR o limite - enquanto o valor continuar baixo nao
    fica reabrindo o menu toda hora; so rearma quando o valor volta a subir
    acima do limite."""
    watch_selector = step["watch_selector"]
    threshold = step["threshold"]
    open_selector = step["open_selector"]
    option_selector = step["option_selector"]
    label = step.get("label", option_selector)

    try:
        raw_text = page.eval_on_selector(watch_selector, "el => el.textContent")
    except Exception:
        return True
    digits = "".join(ch for ch in (raw_text or "") if ch.isdigit())
    if not digits:
        return True
    value = int(digits)

    below = value <= threshold
    if not below:
        step["_below_threshold"] = False
        return True

    if step.get("_below_threshold"):
        # ja disparou desde a ultima vez que ficou acima do limite; nao repete.
        return True

    log(f"  {watch_selector} em {value} (limite {threshold}) - enviando: {label}...")
    remember_selector = step.get("remember_selector")
    if remember_selector:
        # Guarda o que estava na tela (ex: nome da hunt atual) antes de trocar
        # de modo, pra 'Voltar a cacar' saber pra onde voltar depois.
        try:
            hunt_name = page.eval_on_selector(remember_selector, "el => el.textContent")
            TRAINING_MEMORY["hunt_name"] = (hunt_name or "").strip()
            TRAINING_MEMORY["waiting"] = True
        except Exception:
            pass

    try:
        click_open_wave(page, open_selector)
        page.click(option_selector, timeout=3000)
        log(f"  '{label}' clicado.")
    except Exception as error:
        log(f"  Erro ao clicar '{label}': {error}")
        return False

    step["_below_threshold"] = True
    return True


def click_open_wave(page, open_selector, timeout=3000):
    """Clica o pill que abre o menu de Teleportes (normalmente '#wave-title').

    Em telas/resolucoes mais apertadas, o indicador de progresso da hunt
    atual ('#wave-dots', ex: 'Wave 10/10 - cacando') pode ficar sobrepondo
    esse pill e bloquear o clique normal do Playwright ('subtree intercepts
    pointer events') mesmo com o elemento visivel - confirmado num PC onde
    isso derrubava tanto 'Enfrentar Chefes' quanto a task da guild. Tenta o
    clique normal primeiro (mais seguro) e so forca (ignora a checagem de
    sobreposicao) se esse falhar - se as duas tentativas falharem, quem
    chamou continua tratando a excecao normalmente (ex: log de erro)."""
    try:
        page.click(open_selector, timeout=timeout)
    except Exception:
        page.click(open_selector, timeout=timeout, force=True)


def clear_hunt_search(page):
    """Reseta os DOIS filtros da lista de Hunts que podem deixar so uma
    fracao das fases visiveis - CONFIRMADO ao vivo como causa real de bug:
    - campo de busca ('.pick-search', 'Buscar fase ou monstro...') com texto
      residual (de uma busca anterior, manual ou de outra parte do bot).
    - categoria por 'Level recomendado' ('.sp-cat') - o jogo abre a lista ja
      filtrada pra faixa de nivel da fase ATUAL (ex: '300+'), escondendo
      qualquer fase fora dessa faixa (ex: uma task de guild bem mais baixa,
      tipo Orclops lvl 100 - nunca era achada com o filtro preso em '300+').
    Sem isso, o bot podia nao achar uma fase que existe na lista, so porque
    ela estava fora do que os filtros deixavam visivel no momento. Chama
    logo apos abrir a lista, ANTES de procurar qualquer fase por nome."""
    try:
        search_el = page.query_selector(".pick-search")
        if search_el is not None and (search_el.input_value() or ""):
            search_el.fill("", timeout=3000)
            page.wait_for_timeout(200)
    except Exception:
        pass  # campo de busca pode nao existir em todo lugar - nao trava por isso

    try:
        active_cat = page.query_selector(".sp-cat.on")
        if active_cat is not None and "Todas" not in (active_cat.text_content() or ""):
            page.click('.sp-cat:has-text("Todas")', timeout=2000)
            page.wait_for_timeout(200)
    except Exception:
        pass  # filtro de categoria pode nao existir em todo lugar - nao trava por isso


def find_and_go_to_hunt(page, hunt_name, open_selector, hunts_selector, row_selector, name_selector, go_selector, log):
    """Abre o menu de teleportes, entra em Hunts, acha a fase cujo nome bate
    exatamente com 'hunt_name' e clica em 'Cacar'. Retorna True se achou a
    fase (mesmo que ja fosse a ativa agora - botao 'Cacar' desabilitado),
    False se nao achou a fase ou deu erro ao clicar. Compartilhado entre
    'dom_resume_hunt' (volta pra hunt de antes do treino) e 'dom_guild_tasks'
    (vai pra hunt certa de uma task aceita)."""
    try:
        click_open_wave(page, open_selector)
        page.click(hunts_selector, timeout=3000)
        page.wait_for_selector(row_selector, timeout=4000)
        clear_hunt_search(page)
    except Exception as error:
        log(f"  Erro ao abrir a lista de Hunts: {error}")
        return False

    # acha o INDICE da linha certa numa unica chamada (a lista pode ter
    # varias dezenas de fases - ler o nome de cada linha separadamente, cada
    # leitura sendo uma ida-e-volta pelo protocolo de depuracao, era o que
    # deixava essa tela demorando varios segundos pra achar a fase certa).
    target_row = None
    try:
        match_index = page.evaluate(
            """([rowSel, nameSel, wanted]) => Array.from(document.querySelectorAll(rowSel)).findIndex(
                row => (row.querySelector(nameSel)?.textContent || '').trim() === wanted
            )""",
            [row_selector, name_selector, hunt_name],
        )
    except Exception:
        match_index = -1
    if match_index is not None and match_index >= 0:
        rows = page.query_selector_all(row_selector)
        if match_index < len(rows):
            target_row = rows[match_index]

    if target_row is None:
        log(f"  Nao encontrei a fase '{hunt_name}' na lista de Hunts.")
        return False

    try:
        go_button = target_row.query_selector(go_selector)
        if go_button is None or not go_button.is_visible():
            # a linha pode precisar ser expandida (clicada) antes do botao 'Cacar' aparecer.
            target_row.click(timeout=3000)
            time.sleep(0.3)
            go_button = target_row.query_selector(go_selector)
        if go_button is None:
            log(f"  Botao 'Cacar' nao encontrado na fase '{hunt_name}'.")
            return False
        if not go_button.is_enabled():
            # desabilitado normalmente significa que essa ja e a fase ativa agora.
            log(f"  'Cacar' desabilitado para '{hunt_name}' - provavelmente ja e a fase ativa.")
            return True
        go_button.click(timeout=5000)
        log(f"  'Cacar' clicado em '{hunt_name}'.")
    except Exception as error:
        log(f"  Erro ao clicar 'Cacar': {error}")
        return False

    return True


def match_hunt_by_monsters(page, monster_names, row_selector, name_selector, mobs_selector):
    """Percorre a lista de Hunts (ja aberta) e acha a fase cujos monstros
    (texto de '.stage-mobs') tem mais nomes em comum com 'monster_names' (ex:
    os monstros pedidos por uma task da guild). Retorna (nome_da_fase,
    elemento_da_linha) do melhor resultado, ou (None, None) se nenhuma fase
    tiver algum monstro em comum."""
    wanted = {name.strip().lower() for name in monster_names if name.strip()}
    if not wanted:
        return None, None

    # le nome + monstros de TODAS as linhas numa unica chamada (mesmo motivo
    # de 'find_and_go_to_hunt' - a lista pode ter varias dezenas de fases, e
    # aqui cada linha antes levava ATE 2 idas-e-voltas separadas).
    rows_data = page.evaluate(
        """([rowSel, nameSel, mobsSel]) => Array.from(document.querySelectorAll(rowSel)).map(row => ({
            name: row.querySelector(nameSel)?.textContent?.trim() || '',
            mobs: row.querySelector(mobsSel)?.textContent || '',
        }))""",
        [row_selector, name_selector, mobs_selector],
    )

    best_name, best_index, best_score = None, None, 0
    for index, row_data in enumerate(rows_data):
        mobs = {name.strip().lower() for name in row_data["mobs"].split(",") if name.strip()}
        score = len(wanted & mobs)
        if score > best_score and row_data["name"]:
            best_name, best_index, best_score = row_data["name"], index, score

    if best_index is None:
        return None, None

    rows = page.query_selector_all(row_selector)
    best_row = rows[best_index] if best_index < len(rows) else None
    return best_name, best_row


def execute_dom_resume_hunt_step(page, step, log):
    """Passo tipo 'dom_resume_hunt': quando estamos esperando retorno do treino
    (TRAINING_MEMORY['waiting']) e a stamina volta a subir acima do limite,
    abre o menu de teleportes, entra em Hunts, acha a fase com o mesmo nome
    que estava ativa antes de treinar e clica em 'Cacar'."""
    if not TRAINING_MEMORY.get("waiting"):
        return True

    watch_selector = step["watch_selector"]
    threshold = step["threshold"]

    try:
        raw_text = page.eval_on_selector(watch_selector, "el => el.textContent")
    except Exception:
        return True
    digits = "".join(ch for ch in (raw_text or "") if ch.isdigit())
    if not digits:
        return True
    value = int(digits)

    if value < threshold:
        return True

    hunt_name = TRAINING_MEMORY.get("hunt_name")
    if not hunt_name:
        log("  Stamina recuperada, mas nao ha hunt anterior memorizada - volte manualmente.")
        TRAINING_MEMORY["waiting"] = False
        return True

    log(f"  Stamina em {value}% (limite {threshold}) - voltando para '{hunt_name}'...")
    ok = find_and_go_to_hunt(
        page,
        hunt_name,
        open_selector=step["open_selector"],
        hunts_selector=step["hunts_selector"],
        row_selector=step.get("row_selector", ".stage-row"),
        name_selector=step.get("name_selector", ".stage-name-line b"),
        go_selector=step.get("go_selector", ".stage-go"),
        log=log,
    )
    if ok:
        TRAINING_MEMORY["waiting"] = False
    return ok


def parse_cooldown_text(text):
    """Converte um texto tipo '15h 58m' (ou so '58m', ou so '2h') no cooldown
    de um chefe em segundos. Retorna None se nao reconhecer o formato."""
    if not text:
        return None
    match = re.search(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", text)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours * 3600 + minutes * 60


def read_bosstiary_kills(page, log):
    """Le o placar de vitorias (kills) de cada chefe no Bosstiary (Cyclopedia >
    Bosstiary) e atualiza BOSS_KILLS_MEMORY - o jogo ja conta certinho, entao o
    bot nao precisa adivinhar se um combate foi vitoria ou derrota.

    Le tudo numa UNICA chamada (page.evaluate), nao com query_selector +
    text_content por celula (a lista tem varias dezenas de chefes - cada
    leitura separada e uma ida-e-volta pelo protocolo de depuracao; somadas,
    isso segurava essa tela aberta por vários segundos so pra ler o placar)."""
    try:
        page.click("#tab-cyclopedia", timeout=3000)
        tab_class = page.eval_on_selector('.cyc-tabbtn[data-tab="bosstiary"]', "el => el.className") or ""
        if "on" not in tab_class.split():
            page.click('.cyc-tabbtn[data-tab="bosstiary"]', timeout=3000)
        page.wait_for_selector(".cyc-bosscell", timeout=4000)
    except Exception as error:
        log(f"  Erro ao abrir Cyclopedia/Bosstiary: {error}")
        return

    cells = page.evaluate(
        """() => Array.from(document.querySelectorAll('.cyc-bosscell')).map(cell => ({
            name: cell.querySelector('.cyc-cell-name')?.textContent?.trim() || '',
            sub: cell.querySelector('.cyc-cell-sub')?.textContent?.trim() || '',
        }))"""
    )
    for cell in cells:
        match = re.search(r"([\d.]+)\s*kill", cell["sub"])
        if cell["name"] and match:
            BOSS_KILLS_MEMORY[cell["name"]] = int(match.group(1).replace(".", ""))

    try:
        page.click("#cyclopedia-modal-close", timeout=3000)
    except Exception:
        page.keyboard.press("Escape")


def run_between_fights_routines(page, stop_event, log, all_routines):
    """Chamada entre um chefe e o proximo (dentro da MESMA sequencia de
    combates). So dispara 'Entregar Codex e Vender Loot' - resolve na hora
    (nao depende de tempo de jogo, ao contrario de uma task de guild) e evita
    a Loot Pouch encher numa sequencia longa de combates. A propria rotina so
    faz algo de verdade se tiver o que vender/entregar (gate_selector
    '#sell-all'), entao chamar ela toda vez e barato quando nao ha nada
    acumulado ainda.

    Tasks de guild ficam de FORA de proposito - a prioridade e estrita
    (chefes ate o fim, DEPOIS tasks de guild, DEPOIS volta pra hunt/bestiary/
    codex), entao elas so entram na vez delas depois que os chefes
    acabarem."""
    if not all_routines:
        return
    routine = next((r for r in all_routines if r.get("id") == "vender_loot" and r.get("enabled")), None)
    if routine is None:
        return
    try:
        run_routine(page, routine, stop_event, log, all_routines=all_routines)
    except Exception as error:
        log(f"  Erro ao vender/entregar entre combates: {error}")
        recover(page, log)


def execute_dom_boss_fight_step(page, step, stop_event, log, all_routines=None):
    """Passo tipo 'dom_boss_fight': abre o menu de Chefes, garante o filtro
    'Prontos' ligado (o proprio jogo ja filtra por nivel suficiente e fora da
    recarga de 13h - nao precisamos controlar isso) e escolhe, dentre os
    marcados que estiverem prontos agora, o PRIMEIRO na ordem da lista do
    usuario (step['bosses'] - a mesma ordem que aparece no BossPicker, e que
    da pra definir importando um .txt) - nao a ordem em que o jogo mostra as
    linhas na tela. O combate e real (nao instantaneo) - espera a barra de
    vida do chefe (.bossbar) aparecer e depois sumir antes de considerar
    concluido.

    Enfrenta TODOS os chefes marcados que estiverem prontos, um atras do
    outro, na MESMA chamada - sem isso, o intervalo da rotina (30s) somado ao
    tempo real de cada combate criava um atraso artificial entre um chefe e
    o proximo, deixando outras rotinas (ex: tasks de guild) 'furarem a fila'
    entre eles mesmo com varios chefes prontos ao mesmo tempo. So retorna (e
    calcula a proxima consulta pelo cooldown) quando de fato nao sobrar
    nenhum marcado pronto.

    A ordem de prioridade e ESTRITA - chefes ate o fim, DEPOIS tasks de
    guild ate o fim, DEPOIS volta pra hunt anterior (bestiary/codex). Por
    isso, entre um chefe e o proximo (ve 'run_between_fights_routines'), so
    entra Entregar Codex/Vender Loot - e so por ser resolvido no MESMO clique
    (sem precisar de tempo de jogo, ao contrario de uma task de guild, que so
    avanca caçando de verdade) e assim evita a Loot Pouch encher numa
    sequencia longa de combates. Tasks de guild e 'Separar Loot' ficam de
    fora de proposito - so entram na vez delas depois que os chefes
    acabarem, respeitando a ordem normal das rotinas.

    Chefes marcados 'stone_skin' (BossPicker) trocam o amuleto do EK pro
    Stone Skin Amulet antes de lutar - ver BOSS_AMULET_MEMORY. A troca vale
    pra SEQUENCIA INTEIRA, nao so pro chefe que precisou dela: um chefe sem
    'stone_skin' no meio da sequencia nao mexe no amuleto (trocado ou nao).
    So volta pro que estava antes quando a sequencia termina de verdade
    (ninguem mais pronto) ou e interrompida (falha num combate)."""
    enabled_names = {b["name"] for b in step["bosses"] if b.get("enabled")}
    if not enabled_names:
        return True

    if not BOSS_KILLS_MEMORY:
        # primeira vez que esta rotina roda nesta sessao - pega o placar
        # historico completo pra ja aparecer na tela de escolha de chefes.
        read_bosstiary_kills(page, log)

    if time.monotonic() < BOSS_MEMORY["next_check"]:
        return True  # lista esgotada recentemente, ainda dentro da recarga - nao vale consultar

    open_selector = step["open_selector"]
    boss_menu_selector = step["boss_menu_selector"]
    ready_selector = step["ready_selector"]
    row_selector = step.get("row_selector", ".boss-cell")
    name_selector = step.get("name_selector", ".boss-cell-name")
    go_selector = step.get("go_selector", ".boss-fight")

    target_name = None  # garante que exista mesmo se stop_event ja estiver setado ao entrar no laco
    fought_any = False
    while not stop_event.is_set():
        try:
            click_open_wave(page, open_selector)
            page.click(boss_menu_selector, timeout=3000)
        except Exception as error:
            log(f"  Erro ao abrir a lista de Chefes: {error}")
            return False

        try:
            ready_class = page.eval_on_selector(ready_selector, "el => el.className") or ""
            if "on" not in ready_class.split():
                page.click(ready_selector, timeout=3000)
        except Exception as error:
            log(f"  Erro ao garantir o filtro 'Prontos': {error}")
            return False

        time.sleep(0.3)
        # a ORDEM da lista de chefes marcados (step['bosses']) e a prioridade de
        # luta - nao a ordem em que o jogo mostra as linhas na tela. Le quem esta
        # pronto agora, depois percorre a lista do usuario nessa ordem e pega o
        # primeiro marcado que estiver pronto (isso e o que faz importar um .txt
        # com uma ordem especifica ter efeito de verdade). Le todos os nomes
        # numa unica chamada (a lista tem varias dezenas de chefes - ler um
        # por um, cada leitura sendo uma ida-e-volta pelo protocolo de
        # depuracao, era o que deixava essa tela demorando pra sair).
        # o filtro 'Prontos' do proprio jogo e so por nivel/recarga - CONFIRMADO
        # ao vivo que ele ainda lista um chefe sem cargas restantes hoje (botao
        # 'Enfrentar' desabilitado, status 'Sem cargas'), entao exige tambem que
        # o botao da linha nao esteja desabilitado. Sem isso, o bot escolhia
        # esse chefe como alvo, o clique em 'Enfrentar' sempre falhava (timeout)
        # e a rotina inteira era interrompida e reiniciada do zero a cada ciclo -
        # travado tentando o mesmo chefe pra sempre.
        ready_names = set(
            page.evaluate(
                """([rowSel, nameSel, goSel]) => Array.from(document.querySelectorAll(rowSel)).filter(
                    row => {
                        const btn = row.querySelector(goSel);
                        return btn && !btn.disabled;
                    }
                ).map(row => row.querySelector(nameSel)?.textContent?.trim() || '').filter(Boolean)""",
                [row_selector, name_selector, go_selector],
            )
        )

        target_name = None
        target_needs_stone_skin = False
        for boss in step["bosses"]:
            if boss.get("enabled") and boss["name"] in ready_names:
                target_name = boss["name"]
                target_needs_stone_skin = bool(boss.get("stone_skin"))
                break

        if target_name is not None:
            # pra chefes marcados 'stone_skin' (BossPicker), troca o amuleto do
            # EK ANTES do combate - ve 'equip_boss_amulet'. FICA trocado ate o
            # FIM de toda a sequencia (nao reverte a cada chefe): um chefe que
            # nao precisa de 'stone_skin' no meio da sequencia simplesmente nao
            # mexe no amuleto, trocado ou nao. So' reverte quando a sequencia
            # acaba (mais abaixo) ou e interrompida (falha no combate, logo a
            # seguir) - conforme pedido, pra nao ficar abrindo/fechando o
            # Helper a cada chefe a toa.
            if target_needs_stone_skin and not BOSS_AMULET_MEMORY["changed"]:
                # a lista de chefes ('#boss-modal', aberta la em cima pra ler
                # quem esta pronto) e o Helper sao os dois modais - o jogo nao
                # deixa abrir o Helper com a lista ainda aberta por cima
                # (CONFIRMADO ao vivo: o clique na aba EK do Helper ficava
                # bloqueado pelo proprio '#boss-modal' - "Erro ao abrir
                # Helper"). Fecha a lista, troca o amuleto, reabre a lista
                # (com o filtro 'Prontos' de novo) antes de seguir pro combate.
                page.keyboard.press("Escape")
                time.sleep(0.3)
                BOSS_AMULET_MEMORY["changed"] = equip_boss_amulet(page, log)
                try:
                    click_open_wave(page, open_selector)
                    page.click(boss_menu_selector, timeout=3000)
                    ready_class = page.eval_on_selector(ready_selector, "el => el.className") or ""
                    if "on" not in ready_class.split():
                        page.click(ready_selector, timeout=3000)
                    time.sleep(0.3)
                except Exception as error:
                    log(f"  Erro ao reabrir a lista de Chefes apos trocar o amuleto: {error}")
                    return False
            fought = fight_one_boss(page, stop_event, log, target_name, row_selector, name_selector, go_selector)
            if not fought:
                if BOSS_AMULET_MEMORY["changed"]:
                    revert_boss_amulet(page, log, BOSS_AMULET_MEMORY["changed"])
                    BOSS_AMULET_MEMORY["changed"] = {}
                if fought_any:
                    read_bosstiary_kills(page, log)
                return False
            fought_any = True

            # entre um chefe e outro, da uma chance pras rotinas de proxima
            # prioridade (tasks de guild, depois vender/entregar) - sem isso,
            # uma sequencia longa de chefes prontos travava TUDO o resto ate
            # esgotar a lista inteira (task de guild ja aceita ficava esperando
            # sem ninguem ir ate ela, Loot Pouch enchendo etc).
            run_between_fights_routines(page, stop_event, log, all_routines)

            continue  # pode ter mais chefes prontos - checa de novo na hora, sem esperar o proximo tick

        break

    if target_name is None:
        # sequencia de chefes acabou (nenhum pronto restante) - se o amuleto
        # foi trocado pro Stone Skin em algum ponto dela, volta pro que
        # estava antes AGORA, uma unica vez pra sequencia inteira.
        if BOSS_AMULET_MEMORY["changed"]:
            revert_boss_amulet(page, log, BOSS_AMULET_MEMORY["changed"])
            BOSS_AMULET_MEMORY["changed"] = {}

        # nenhum chefe selecionado esta pronto agora. Antes de fechar, desliga o
        # filtro 'Prontos' pra ver o cooldown real de cada um marcado (o jogo
        # mostra tipo '15h 58m' em '.boss-cell-status.cd') e usa o MENOR deles
        # como tempo de espera exato - em vez de chutar 13h fixo.
        status_selector = step.get("status_selector", ".boss-cell-status.cd")
        wait_seconds = None
        try:
            ready_class_now = page.eval_on_selector(ready_selector, "el => el.className") or ""
            if "on" in ready_class_now.split():
                page.click(ready_selector, timeout=3000)
                time.sleep(0.3)
            # le nome + status de TODAS as linhas numa unica chamada (mesmo
            # motivo do resto dessa funcao - dezenas de chefes, cada leitura
            # separada e uma ida-e-volta pelo protocolo de depuracao).
            rows_data = page.evaluate(
                """([rowSel, nameSel, statusSel]) => Array.from(document.querySelectorAll(rowSel)).map(row => ({
                    name: row.querySelector(nameSel)?.textContent?.trim() || '',
                    status: row.querySelector(statusSel)?.textContent?.trim() || '',
                }))""",
                [row_selector, name_selector, status_selector],
            )
            remaining = []
            for row_data in rows_data:
                if row_data["name"] not in enabled_names or not row_data["status"]:
                    continue
                parsed = parse_cooldown_text(row_data["status"])
                if parsed is not None:
                    remaining.append(parsed)
            if remaining:
                wait_seconds = min(remaining)
        except Exception as error:
            log(f"  Nao consegui ler o cooldown exato, usando estimativa: {error}")

        # se leu pelo menos um cooldown real (formato 'Xh Ym'), a proxima consulta
        # e uma estimativa em que confiamos - so ENTAO faz sentido, se ela falhar,
        # cair no modo de retentativa curta abaixo. Chefes sem cooldown legivel
        # (ex: status 'Sem cargas' - sem tentativas hoje, sem contagem regressiva
        # nenhuma pra ler) nao geram estimativa nenhuma; tratar isso como 'estimativa
        # que falhou' fazia o bot entrar no modo de retentativa curta (5 em 5min,
        # pra sempre, ate o proximo dia) so por ter um chefe assim na lista -
        # CONFIRMADO ao vivo como causa real do bot ficando reabrindo a lista de
        # Chefes sem parar.
        had_real_estimate = wait_seconds is not None

        if BOSS_MEMORY["missed_estimate"]:
            # ja confiamos numa estimativa uma vez e o horario calculado chegou
            # sem ninguem pronto - em vez de arriscar outra estimativa longa
            # (que pode repetir o mesmo problema), passa a tentar num ritmo
            # curto e fixo ate realmente conseguir.
            wait_seconds = BOSS_RETRY_SECONDS
            log(f"  Ainda ninguem pronto no horario calculado - tentando de novo em {wait_seconds // 60}min.")
            # nao temos uma leitura real aqui - mostra o mesmo prazo de retentativa.
            BOSS_MEMORY["display_next_check"] = time.monotonic() + wait_seconds
        else:
            if wait_seconds is None:
                wait_seconds = BOSS_COOLDOWN_SECONDS  # nao deu pra ler (ex: 'Sem cargas') - cai no chute de seguranca
            log(f"  Nenhum chefe marcado esta pronto - proxima consulta em ~{wait_seconds / 3600:.1f}h.")
            # guarda o horario real (sem a margem) pra GUI exibir igual ao jogo -
            # a margem abaixo e so pra CONSULTAR um pouco antes, por seguranca,
            # nao deve aparecer pro usuario como se fosse o tempo real restante.
            BOSS_MEMORY["display_next_check"] = time.monotonic() + wait_seconds
            wait_seconds = max(wait_seconds - BOSS_BACKOFF_MARGIN_SECONDS, 0)
            BOSS_MEMORY["missed_estimate"] = had_real_estimate

        BOSS_MEMORY["next_check"] = time.monotonic() + wait_seconds
        recover(page, log)
        if fought_any:
            # atualiza o placar (Bosstiary) UMA VEZ so, depois de todos os
            # combates dessa chamada - nao a cada chefe (evita reabrir o
            # painel pesado do Bosstiary varias vezes seguidas). So agora,
            # com o painel de Chefes ja fechado (recover acima), porque
            # abrir o Bosstiary navega pra outra tela e fecharia ele mesmo.
            read_bosstiary_kills(page, log)
            if not GUILD_TASK_MEMORY.get("grinding"):
                # chefes sao a interrupcao de maior prioridade - ao
                # terminar TODOS os prontos, volta pra hunt padrao (a
                # 'base') na hora, sem esperar o proximo tick de
                # 'ensure_active_hunt'. force=True: chefes tem prioridade
                # sobre o avanco automatico tambem.
                return_to_default_hunt(page, log, force=True)
        return True

    if fought_any:
        read_bosstiary_kills(page, log)
        if not GUILD_TASK_MEMORY.get("grinding"):
            return_to_default_hunt(page, log, force=True)
    return True


def fight_one_boss(page, stop_event, log, target_name, row_selector, name_selector, go_selector):
    """Enfrenta UM chefe especifico (ja confirmado pronto pelo chamador) e
    espera o combate de verdade acontecer (barra de vida aparecer e sumir).
    Retorna True se o combate rolou (vitoria ou derrota, tanto faz - o
    placar real vem do Bosstiary), False se algo deu errado ao clicar ou o
    combate nem comecou. NAO atualiza o Bosstiary aqui - quem chama faz isso
    UMA VEZ so, depois de enfrentar todos os chefes prontos, pra nao abrir o
    painel do Bosstiary (pesado) de novo a cada combate."""
    # garante que a proxima rodada nao fique presa num recuo antigo (pode ter
    # mais da lista prontos ainda), e destrava o modo de retentativa curta
    # (a estimativa funcionou desta vez).
    BOSS_MEMORY["next_check"] = 0.0
    BOSS_MEMORY["display_next_check"] = 0.0
    BOSS_MEMORY["missed_estimate"] = False

    # clica por seletor (nao por uma referencia de elemento ja capturada): a
    # grade de chefes se recria a cada atualizacao, entao uma referencia
    # guardada de antes fica "presa" a um no que some ("Element is not
    # attached to the DOM"). Escapa aspas no nome por seguranca no seletor.
    safe_name = target_name.replace('"', '\\"')
    row_scope = f'{row_selector}:has({name_selector}:text-is("{safe_name}"))'
    go_scoped = f"{row_scope} {go_selector}"

    try:
        go_button = page.query_selector(go_scoped)
        if go_button is None or not go_button.is_visible():
            # o card do chefe pode precisar ser expandido (clicado) antes do
            # botao 'Enfrentar' ficar visivel - mesmo padrao da Loot Pouch/Hunts.
            page.click(row_scope, timeout=3000)
            time.sleep(0.3)
        page.click(go_scoped, timeout=5000)
        log(f"  Enfrentando '{target_name}'...")
    except Exception as error:
        log(f"  Erro ao clicar 'Enfrentar' em '{target_name}': {error}")
        return False

    # espera o combate comecar (a barra de vida do chefe aparecer)
    started = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not stop_event.is_set():
        if page.query_selector(".bossbar") is not None:
            started = True
            break
        time.sleep(0.5)

    if not started:
        log(f"  Combate contra '{target_name}' nao parece ter comecado.")
        return False

    # espera o combate terminar (a barra de vida sumir) - pode demorar de verdade.
    deadline = time.monotonic() + BOSS_FIGHT_MAX_SECONDS
    while time.monotonic() < deadline and not stop_event.is_set():
        if page.query_selector(".bossbar") is None:
            log(f"  Combate contra '{target_name}' terminou.")
            break
        time.sleep(1)
    else:
        log(f"  Combate contra '{target_name}' ainda em andamento apos {BOSS_FIGHT_MAX_SECONDS}s - seguindo em frente.")

    # ao vencer/perder um chefe o jogo pode abrir um popup por cima de tudo
    # (ex: oferta de 'Boss Pass'), que bloqueia os cliques de qualquer outra
    # rotina ate ser fechado. Fecha qualquer coisa assim antes de continuar.
    recover(page, log)
    return True


# personagem e preset usados pela troca de amuleto pra chefes dificeis (ver
# 'stone_skin' no BossPicker/equip_boss_amulet) - fixo por enquanto, so' o EK
# (Elite Knight - personagem que tanka) precisa disso.
BOSS_AMULET_CHAR = "EK"
BOSS_AMULET_ITEM = "Stone Skin Amulet"
# % dos 2 selects do card de Amuleto no Helper ('Equipar com vida abaixo de'
# / 'Restaurar com vida acima de') enquanto o Stone Skin estiver ativo -
# valores altos (mais agressivos) fazem sentido pra chefe: troca pro
# emergencial mais cedo (vida ainda alta) e ja volta pro padrao assim que
# recuperar um pouco, dado que o proprio Stone Skin ja e' o "plano B".
BOSS_AMULET_EQUIP_PCT = "85"
BOSS_AMULET_RESTORE_PCT = "90"


def open_helper_equip_amulet(page, char_label, preset_label, log):
    """Abre o Helper (icone 'Helper' no topo, ao lado de Progressao) na aba
    Equipamento > Amuleto do personagem e preset pedidos ('Hunt'/'Boss'/'PVP')
    - deixa pronto pra ler/trocar os campos Emergencial/Padrao. CONFIRMADO ao
    vivo: '#tab-helper' abre o painel, '.bar-char' sao as abas de personagem
    (uma por vocacao, span com a sigla tipo 'EK'), '.helper-profilebtn' e o
    Hunt/Boss/PVP, '.helper-menubtn' e o menu esquerdo (inclui 'Equipamento').
    Cada clique so' acontece se o estado ainda nao for o esperado (idempotente -
    nao reabre/retroca a toa se ja estiver na tela certa).

    BUG CONFIRMADO ao vivo (e corrigido): '.bar-char' aparece 2x no DOM com o
    Helper aberto - a barra de personagem inferior do jogo (title termina em
    '... clique p/ configurar no Helper', SEMPRE presente, mas fica atras do
    fundo do proprio modal e bloqueia clique) e as abas de verdade DENTRO do
    card do Helper ('#helper-modal .im-card'). Sem escopar pro '.im-card', o
    clique podia mirar a barra de baixo (bloqueada) e travar - ou, pior,
    'active_char' podia ler o personagem que esta JOGANDO no momento em vez
    do que o Helper esta de fato mostrando, fazendo achar que ja estava no
    personagem certo e pular a troca (o Helper abria mas nao alterava nada)."""
    try:
        if not page.eval_on_selector("#helper-modal", "el => !el.className.includes('hidden')"):
            page.click("#tab-helper", timeout=3000)
            time.sleep(0.5)
        active_char = page.eval_on_selector("#helper-modal .im-card .bar-char.active span", "el => el.textContent")
        if active_char != char_label:
            page.click(f'#helper-modal .im-card .bar-char:has(span:text-is("{char_label}"))', timeout=3000)
            time.sleep(0.3)
        active_tab = page.eval_on_selector(".helper-profilebtn.on", "el => el.textContent")
        if active_tab != preset_label:
            page.click(f'.helper-profilebtn:has-text("{preset_label}")', timeout=3000)
            time.sleep(0.3)
        active_menu = page.eval_on_selector(".helper-menubtn.on", "el => el.textContent")
        if not active_menu or "Equipamento" not in active_menu:
            page.click('.helper-menubtn:has-text("Equipamento")', timeout=3000)
            time.sleep(0.3)
        return True
    except Exception as error:
        log(f"  Erro ao abrir Helper ({char_label}/{preset_label}): {error}")
        return False


def read_helper_amulet(page, field_cls):
    """Le o nome do item no slot de amuleto 'emer' (Emergencial) ou 'padr'
    (Padrao) - Helper ja precisa estar aberto na tela certa."""
    try:
        return page.eval_on_selector(f'.helper-equipfield:has(.fl.{field_cls}) .helper-equipitem-name', "el => el.textContent")
    except Exception:
        return None


def set_helper_amulet(page, field_cls, item_name, log):
    """Troca o amuleto do slot 'emer'/'padr' pro item procurado por nome,
    usando a busca do picker (mesmo padrao de '.pick-search' ja usado nas
    Hunts) - CONFIRMADO ao vivo que a lista ('.sp-list.sp-book-list') so'
    mostra o que ha na pouch/mochila. Retorna True se achou e trocou; False
    se o item nao esta disponivel agora - nesse caso fecha o picker (Escape)
    sem mudar nada, do jeito que o usuario pediu."""
    try:
        page.click(f'.helper-equipfield:has(.fl.{field_cls}) .helper-equipitem', timeout=3000)
        time.sleep(0.4)
        search = page.query_selector('.pick-search')
        if search is None:
            return False
        search.fill(item_name, timeout=3000)
        time.sleep(0.4)
        count = page.evaluate("() => document.querySelectorAll('.sp-list.sp-book-list .sp-book-row').length")
        if not count:
            page.keyboard.press("Escape")
            return False
        page.click('.sp-list.sp-book-list .sp-book-row button', timeout=3000)
        time.sleep(0.3)
        return True
    except Exception as error:
        log(f"  Erro ao trocar amuleto '{field_cls}' pra '{item_name}': {error}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def read_helper_amulet_thresholds(page):
    """Le os 2 selects do card de Amuleto no Helper (nessa ordem no HTML):
    'Equipar com vida abaixo de' (emergencial) e 'Restaurar com vida acima
    de' (padrao). Retorna (equip_pct, restore_pct) como string (o 'value' de
    cada <option>), ou (None, None) se nao achar os 2."""
    try:
        values = page.eval_on_selector_all(
            ".helper-equipcard .helper-sel", "els => els.map(el => el.value)"
        )
        if len(values) >= 2:
            return values[0], values[1]
    except Exception:
        pass
    return None, None


def set_helper_amulet_thresholds(page, equip_pct, restore_pct, log):
    """Ajusta os 2 selects de % do card de Amuleto (ver 'read_helper_amulet_thresholds').
    Se algum valor pedido nao existir como opcao (o jogo pode limitar o range),
    so' loga e deixa aquele select como estava - nao quebra o resto. Retorna
    True so' se os 2 de fato foram ajustados (pra quem chama nao logar
    'ajustada'/'revertida' quando na verdade falhou)."""
    try:
        selects = page.query_selector_all(".helper-equipcard .helper-sel")
    except Exception as error:
        log(f"  Erro ao achar os campos de % de ativacao do amuleto: {error}")
        return False
    if len(selects) < 2:
        log("  Nao achei os campos de % de ativacao do amuleto.")
        return False
    ok = True
    try:
        selects[0].select_option(str(equip_pct), timeout=3000)
    except Exception as error:
        log(f"  Erro ao ajustar 'Equipar com vida abaixo de' pra {equip_pct}%: {error}")
        ok = False
    try:
        selects[1].select_option(str(restore_pct), timeout=3000)
    except Exception as error:
        log(f"  Erro ao ajustar 'Restaurar com vida acima de' pra {restore_pct}%: {error}")
        ok = False
    return ok


def equip_boss_amulet(page, log):
    """Antes de enfrentar um chefe marcado 'stone_skin' no BossPicker, troca
    o amuleto Emergencial e Padrao do EK (preset Boss, no Helper) pro Stone
    Skin Amulet, pra aguentar mais dano, e deixa os 2 gatilhos de % de vida
    (ver BOSS_AMULET_EQUIP_PCT/RESTORE_PCT) mais agressivos. So' troca o item
    que de fato achar na pouch/mochila - se nao tiver, mantem o que ja
    estava (sem reclamar, conforme pedido). Retorna um dict
    {'items': {'emer'/'padr': nome_original, ...}, 'thresholds': (equip_pct,
    restore_pct) originais ou None} - vazio ({}) so' se nem conseguiu abrir o
    Helper. 'revert_boss_amulet' usa esse dict pra desfazer tudo depois."""
    if not open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
        # 'open_helper_equip_amulet' pode ter aberto o painel Helper e falhado
        # so' num clique seguinte (ex: bloqueado por outro modal ainda aberto
        # por cima) - fecha de qualquer jeito antes de desistir, senao o
        # Helper fica aberto por cima de tudo travando o resto do bot ate a
        # proxima recuperacao.
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        return {}

    items = {}
    for field_cls in ("emer", "padr"):
        original = read_helper_amulet(page, field_cls)
        if original == BOSS_AMULET_ITEM:
            continue  # ja esta com o item certo - nada a trocar/lembrar
        if set_helper_amulet(page, field_cls, BOSS_AMULET_ITEM, log):
            items[field_cls] = original
            log(f"  Amuleto {field_cls} do {BOSS_AMULET_CHAR} trocado pra '{BOSS_AMULET_ITEM}' (era '{original}').")

    orig_equip_pct, orig_restore_pct = read_helper_amulet_thresholds(page)
    thresholds = None
    if orig_equip_pct is not None:
        thresholds = (orig_equip_pct, orig_restore_pct)
        if set_helper_amulet_thresholds(page, BOSS_AMULET_EQUIP_PCT, BOSS_AMULET_RESTORE_PCT, log):
            log(f"  % do amuleto do {BOSS_AMULET_CHAR} ajustada pra {BOSS_AMULET_EQUIP_PCT}%/{BOSS_AMULET_RESTORE_PCT}% (era {orig_equip_pct}%/{orig_restore_pct}%).")

    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    return {"items": items, "thresholds": thresholds}


def revert_boss_amulet(page, log, changed):
    """Desfaz a troca feita por 'equip_boss_amulet' - volta cada slot de item
    que foi de fato alterado pro que estava antes, e as 2 % de ativacao pro
    que estavam antes tambem."""
    if not changed:
        return
    if not open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
        return
    for field_cls, name in (changed.get("items") or {}).items():
        if not name:
            continue
        if set_helper_amulet(page, field_cls, name, log):
            log(f"  Amuleto {field_cls} do {BOSS_AMULET_CHAR} revertido pra '{name}'.")
    thresholds = changed.get("thresholds")
    if thresholds is not None:
        if set_helper_amulet_thresholds(page, thresholds[0], thresholds[1], log):
            log(f"  % do amuleto do {BOSS_AMULET_CHAR} revertida pra {thresholds[0]}%/{thresholds[1]}%.")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")


def click_guild_task_button(page, sec_selector, card_selector, name_sel, foot_sel, matched_class, task_name, keyword, log, retries=3):
    """Acha de novo o botao certo (pelo nome+dificuldade da task e pelo TEXTO
    do botao), em TODAS as secoes (Diárias/Semanais), a cada tentativa - em
    vez de clicar numa referencia achada uma unica vez antes. O painel de
    tasks se atualiza sozinho com frequencia (o contador de kills muda ao
    vivo), e uma referencia antiga pode ficar 'presa' a um no que o React ja
    substituiu no meio do caminho - mesmo problema ja visto na grade de
    Chefes ('Element is not attached to the DOM'). Retorna True se conseguiu
    clicar."""
    for attempt in range(retries):
        for sec in page.query_selector_all(sec_selector):
            grid_handle = sec.evaluate_handle("el => el.nextElementSibling")
            grid = grid_handle.as_element()
            if grid is None:
                continue
            for card in grid.query_selector_all(card_selector):
                classes = (card.get_attribute("class") or "").split()
                if matched_class not in classes:
                    continue
                name_el = card.query_selector(name_sel)
                name = (name_el.text_content() or "").strip() if name_el else ""
                if name != task_name:
                    continue
                foot = card.query_selector(foot_sel)
                if foot is None:
                    continue
                for btn in foot.query_selector_all("button"):
                    text = (btn.text_content() or "").strip().lower()
                    if keyword in text:
                        try:
                            btn.click(timeout=2000)
                            return True
                        except Exception:
                            break  # no proximo attempt, refaz a busca do zero
        time.sleep(0.3)
    log(f"  '{task_name}': botao '{keyword}' sumiu antes do clique {retries}x seguidas - tenta de novo no proximo ciclo.")
    return False


def execute_dom_guild_tasks_step(page, step, log):
    """Passo tipo 'dom_guild_tasks': em Social > Guild > Tasks, aceita as
    tasks (Diárias E Semanais - o limite diario costuma deixar a secao
    Diárias sem nada por boa parte do dia) cuja dificuldade o usuario marcou,
    garante o rastreamento ligado, entrega as que ja completaram (tocando o
    som de conquista) e troca a hunt ativa pra fase certa da task pendente de
    MAIOR prioridade (a mais facil dentre as dificuldades habilitadas, nao
    necessariamente a primeira encontrada na tela - acha a fase comparando os
    monstros da task com os monstros de cada fase na lista de Hunts - ex:
    task pede 'Troll' -> fase com 'Troll' nos mobs). Quando nao sobra nenhuma
    task pendente, volta pra hunt de antes (se o bot tiver trocado alguma)."""
    enabled_classes = {d["card_class"] for d in step["difficulties"] if d.get("enabled")}
    if not enabled_classes:
        return True

    diff_by_class = {d["card_class"]: d for d in step["difficulties"]}

    open_selector = step["open_selector"]
    hunts_selector = step["hunts_selector"]
    row_selector = step.get("row_selector", ".stage-row")
    hunt_name_selector = step.get("name_selector", ".stage-name-line b")
    mobs_selector = step.get("mobs_selector", ".stage-mobs")
    go_selector = step.get("go_selector", ".stage-go")

    try:
        current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
    except Exception:
        current_hunt = ""

    social_selector = step.get("social_selector", "#tab-social")
    guild_selector = step.get("guild_selector", "#tab-guild")
    tasks_tab_selector = step.get("tasks_tab_selector", '.gw-tab[data-lb="Tasks"]')
    sec_selector = step.get("sec_selector", ".gwt-sec")
    card_selector = step.get("card_selector", ".gwt-card")

    try:
        page.click(social_selector, timeout=3000)
        page.click(guild_selector, timeout=3000)
    except Exception as error:
        log(f"  Erro ao abrir Social/Guild: {error}")
        page.keyboard.press("Escape")
        return True  # sem guild, ou painel diferente do esperado - nao trata como falha de rotina

    # o painel de Guild pode abrir em QUALQUER sub-aba (lembra a ultima
    # visitada, ex: Membros) - precisa garantir a aba Tasks ativa ANTES de
    # esperar a secao 'Diárias' aparecer, senao ela nunca aparece (o
    # wait_for_selector abaixo vivia estourando o prazo por causa disso).
    # espera a aba aparecer em vez de checar na hora - o painel ainda esta
    # renderizando logo apos abrir, e um 'nao achou' instantaneo era tratado
    # como "guild sem tasks" (log real: 5 checagens seguidas de 30min cada,
    # ~3h sem mexer nas tasks, com tasks ja aceitas pendentes).
    try:
        tasks_tab_el = page.wait_for_selector(tasks_tab_selector, timeout=3000, state="attached")
    except Exception:
        tasks_tab_el = None
    if tasks_tab_el is None:
        # a guild pode nao ter tasks liberadas ainda (nivel insuficiente etc)
        # - nao e um erro de verdade, so nao ha nada pra fazer agora.
        log("  Tasks da guild nao disponiveis agora - tenta de novo mais tarde.")
        page.keyboard.press("Escape")
        return True
    try:
        tasks_class = tasks_tab_el.get_attribute("class") or ""
        if "active" not in tasks_class.split():
            # clica pelo seletor (re-resolve o elemento na hora do clique) em
            # vez do handle ja consultado acima - o painel re-renderiza a
            # cada segundo (cronometro regressivo), entao o handle antigo as
            # vezes ja estava desanexado do DOM quando o clique chegava.
            page.click(tasks_tab_selector, timeout=3000)
    except Exception as error:
        log(f"  Erro ao abrir a aba Tasks da guild: {error}")
        page.keyboard.press("Escape")
        return True

    try:
        # o painel busca os dados da guild ao abrir - um sleep fixo curto as vezes
        # nao e suficiente (demora varia com a resposta do servidor), o que fazia
        # a secao 'Diárias' parecer "nao encontrada" de vez em quando. Espera de
        # verdade a secao aparecer, com um prazo maior.
        page.wait_for_selector(sec_selector, timeout=5000)
    except Exception as error:
        log(f"  Erro ao carregar as tasks da guild: {error}")
        page.keyboard.press("Escape")
        return True

    # processa TODAS as secoes (Diárias e Semanais) com a mesma logica - nao so
    # 'section_label' - o limite diario ("Máximo de tasks diárias atingido")
    # costuma deixar a secao Diárias sem nada pra fazer por boa parte do dia,
    # enquanto a Semanais ainda tem tasks pra aceitar/entregar normalmente.
    # Antes isso era feito indo de cada 'sec' pro seu nextElementSibling (grid)
    # com uma consulta separada por secao - varios idas-e-voltas seguidos, e
    # o cabecalho de cada secao tem um cronometro regressivo que re-renderiza
    # a cada segundo, entao um desses idas-e-voltas podia cair no meio de um
    # re-render e achar 'sec' desanexado (nextElementSibling vira None),
    # perdendo os cards inteiros daquele ciclo - por isso o bot ficava horas
    # sem aceitar/entregar nada mesmo com tasks disponiveis. Os cards tem
    # classe propria e unica na tela (.gwt-card), entao da pra pegar todos de
    # uma vez, direto, sem depender dessa relacao fragil entre elementos.
    all_cards = page.query_selector_all(card_selector)

    if not all_cards:
        log("  Nenhum card de tasks da guild encontrado (Diárias/Semanais).")
        page.keyboard.press("Escape")
        return True

    name_sel = step.get("card_name_selector", ".gwt-name")
    where_sel = step.get("card_where_selector", ".gwt-where")
    foot_sel = step.get("card_foot_selector", ".gwt-foot")
    track_sel = step.get("card_track_selector", ".gwt-track")
    done_class = step.get("card_done_class", "done")

    # prioridade pra escolher qual task pendente caçar quando mais de uma
    # estiver aceita ao mesmo tempo (ex: Facil e Media aceitas juntas) -
    # segue a ORDEM configurada em 'difficulties' (Facil, Media, Dificil),
    # nao a ordem em que os cards aparecem na tela.
    difficulty_priority = {d["card_class"]: i for i, d in enumerate(step["difficulties"])}

    pending_candidates = []  # [(prioridade, nome, texto dos monstros)] de todas as tasks aceitas ainda incompletas
    for card in all_cards:
        classes = (card.get_attribute("class") or "").split()
        matched_class = next((c for c in enabled_classes if c in classes), None)
        if matched_class is None:
            continue

        name_el = card.query_selector(name_sel)
        task_name = (name_el.text_content() or "").strip() if name_el else "?"
        diff_label = diff_by_class[matched_class].get("label", matched_class)
        foot = card.query_selector(foot_sel)

        # o jogo reaproveita a MESMA classe CSS ('gw-btn') tanto pro botao
        # 'Aceitar' quanto pro 'Entregar' (que aparece quando o card ganha a
        # classe extra 'done') - por isso o texto do botao e o que decide, nao
        # a classe. Sem isso, um card ja pronto pra entregar seria confundido
        # com um card ainda nao aceito (ou vice-versa).
        foot_buttons = foot.query_selector_all("button") if foot is not None else []
        deliver_btn = next((b for b in foot_buttons if "entregar" in (b.text_content() or "").strip().lower()), None)
        accept_btn = next((b for b in foot_buttons if "aceitar" in (b.text_content() or "").strip().lower()), None)

        if done_class in classes:
            if deliver_btn is None:
                log(f"  Task '{task_name}' esta marcada como concluida, mas nao achei o botao 'Entregar'.")
                continue
            if click_guild_task_button(page, sec_selector, card_selector, name_sel, foot_sel, matched_class, task_name, "entregar", log):
                log(f"  Task da guild '{task_name}' ({diff_label}) concluida e entregue!")
                play_achievement_sound()
                record_activity(f"Task da guild completa: '{task_name}' - entregue com sucesso.")
            continue

        if accept_btn is not None:
            if accept_btn.get_attribute("disabled") is not None:
                # ex: "Maximo de tasks diarias atingido" - nada a fazer, so segue.
                continue
            if click_guild_task_button(page, sec_selector, card_selector, name_sel, foot_sel, matched_class, task_name, "aceitar", log):
                log(f"  Task da guild '{task_name}' ({diff_label}) aceita.")
            continue  # progresso/hunt dessa task so e avaliado no proximo ciclo

        track_btn = foot.query_selector(track_sel) if foot else None
        if track_btn is not None and "on" not in (track_btn.get_attribute("class") or "").split():
            click_guild_task_button(page, sec_selector, card_selector, name_sel, foot_sel, matched_class, task_name, "rastrear", log)

        where_el = card.query_selector(where_sel)
        where_text = (where_el.text_content() or "").strip() if where_el else ""
        pending_candidates.append((difficulty_priority.get(matched_class, 99), task_name, where_text))

    pending_task = None
    if pending_candidates:
        pending_candidates.sort(key=lambda item: item[0])
        _, best_name, best_where = pending_candidates[0]
        pending_task = (best_name, best_where)

    # fecha o painel Social/Guild ANTES de tentar abrir os Teleportes - o
    # overlay dele (#guild-overlay) fica por cima da tela inteira e bloqueia
    # o clique em '#wave-title' se ainda estiver aberto.
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    time.sleep(0.3)

    if pending_task is not None:
        task_name, where_text = pending_task
        try:
            click_open_wave(page, open_selector)
            page.click(hunts_selector, timeout=3000)
            # um sleep fixo curto as vezes nao era suficiente pra lista de
            # Hunts terminar de renderizar, fazendo a busca por nome/monstros
            # da task nao achar nada (mesmo problema ja visto em outros
            # lugares) - espera de verdade a lista aparecer.
            page.wait_for_selector(row_selector, timeout=4000)
            clear_hunt_search(page)
        except Exception as error:
            log(f"  Erro ao abrir a lista de Hunts para a task '{task_name}': {error}")
        else:
            # a maioria das tasks tem o mesmo nome da propria fase de hunt (ex:
            # task 'Asuras' -> fase de Hunts 'Asuras', confirmado tambem pelo
            # rastreador "Guild Task" no canto da tela) - tenta por nome exato
            # primeiro, e so cai pra comparacao por monstros pedidos (mais lenta,
            # mas cobre o caso raro de o nome da task nao bater com a fase).
            hunt_name, hunt_row = task_name, None
            for row in page.query_selector_all(row_selector):
                name_el = row.query_selector(hunt_name_selector)
                if name_el and (name_el.text_content() or "").strip() == task_name:
                    hunt_row = row
                    break
            if hunt_row is None:
                hunt_name, hunt_row = match_hunt_by_monsters(
                    page, where_text.split(","), row_selector, hunt_name_selector, mobs_selector
                )
            if hunt_row is None:
                log(f"  Nao encontrei uma hunt pra task '{task_name}' ({where_text}).")
            else:
                # tasks de guild tem prioridade sobre o Codex/Bestiary da hunt
                # atual (ordem: chefes > tasks de guild > bestiary > codex) -
                # troca na hora, mesmo que a hunt atual ainda esteja em
                # progresso. O progresso dela nao se perde: GUILD_TASK_MEMORY
                # guarda a hunt pra voltar quando as tasks selecionadas
                # acabarem.
                #
                # Marca 'grinding' e guarda 'previous_hunt' SEMPRE que ha uma
                # task pendente com hunt conhecida - mesmo se o personagem ja
                # estiver nela (hunt_name == current_hunt) e nenhum clique de
                # troca for necessario. Sem isso (bug real ja visto: bastava
                # o personagem ja estar na hunt certa no PRIMEIRO ciclo em
                # que o bot via a task - por troca manual, ou ate por outra
                # rotina - pra 'grinding' nunca ser ligado), quando a task
                # terminava o bot nunca sabia que precisava voltar pra hunt
                # padrao/anterior, e ficava preso na hunt da task pra sempre.
                if GUILD_TASK_MEMORY["previous_hunt"] is None:
                    GUILD_TASK_MEMORY["previous_hunt"] = current_hunt
                # 'grinding_since' e' um HEARTBEAT: renovado a CADA vez que a
                # rotina confirma que ainda ha task pendente (roda de 60 em
                # 60s enquanto 'grinding') - a trava de seguranca de
                # ensure_active_hunt so' dispara se a rotina PARAR de
                # confirmar isso por GUILD_TASK_GRINDING_MAX_SECONDS. Antes
                # contava desde o INICIO do grind, e uma task longa (ex:
                # 2000+ kills) era arrancada no meio aos 20min - bug real
                # visto no log (tasks aceitas e nunca concluidas).
                GUILD_TASK_MEMORY["grinding_since"] = time.monotonic()
                GUILD_TASK_MEMORY["grinding"] = True
                if hunt_name != current_hunt:
                    try:
                        go_button = hunt_row.query_selector(go_selector)
                        if go_button is None or not go_button.is_visible():
                            hunt_row.click(timeout=3000)
                            time.sleep(0.3)
                            go_button = hunt_row.query_selector(go_selector)
                        if go_button is not None and go_button.is_enabled():
                            go_button.click(timeout=5000)
                            log(f"  Trocando para a hunt '{hunt_name}' (task '{task_name}')...")
                    except Exception as error:
                        log(f"  Erro ao trocar para a hunt '{hunt_name}': {error}")
    elif GUILD_TASK_MEMORY.get("grinding") and GUILD_TASK_MEMORY.get("previous_hunt"):
        # a hunt padrao escolhida pelo usuario (se configurada) tem
        # prioridade sobre 'a hunt de antes' - o objetivo e sempre acabar de
        # volta nela, nao remontar o historico de onde estava antes da task.
        # Le do settings.json na hora (nao so a copia em memoria) - garante
        # que uma hunt padrao recem-configurada seja respeitada mesmo que
        # 'ensure_active_hunt' ainda nao tenha rodado de novo pra atualizar
        # DEFAULT_HUNT_MEMORY (bug real ja visto: caia no fallback errado).
        DEFAULT_HUNT_MEMORY["name"] = load_settings().get("default_hunt", "")
        target_hunt = DEFAULT_HUNT_MEMORY.get("name") or GUILD_TASK_MEMORY["previous_hunt"]
        log(f"  Tasks da guild selecionadas concluidas - voltando para '{target_hunt}'...")
        ok = find_and_go_to_hunt(
            page,
            target_hunt,
            open_selector=open_selector,
            hunts_selector=hunts_selector,
            row_selector=row_selector,
            name_selector=hunt_name_selector,
            go_selector=go_selector,
            log=log,
        )
        if ok:
            GUILD_TASK_MEMORY["previous_hunt"] = None
            GUILD_TASK_MEMORY["grinding"] = False
            GUILD_TASK_MEMORY["grinding_since"] = None

    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    return True


def untrack_unrelated_bestiary(page, monster_names, step, log):
    """Usa o proprio quadro do HUD (#bestiarytrack-overlay) pra parar de
    rastrear, com um clique direto (sem abrir o Cyclopedia), qualquer criatura
    que NAO seja da hunt atual - libera vaga no limite de 5 rastreadas ao
    trocar de hunt."""
    wanted = {name.strip().lower() for name in monster_names}
    row_selector = step.get("bestiary_overlay_row_selector", "#bestiarytrack-overlay .bsk-row")
    name_selector = step.get("bestiary_overlay_name_selector", ".gtk-hunt-name")

    removed = 0
    for row in page.query_selector_all(row_selector):
        name_el = row.query_selector(name_selector)
        name = (name_el.text_content() or "").strip().lower() if name_el else ""
        if name and name not in wanted:
            try:
                row.click(timeout=2000)
                removed += 1
            except Exception:
                pass
    if removed:
        log(f"  {removed} criatura(s) de hunts antigas paradas de rastrear no Bestiary (limite de 5).")


def read_and_track_hunt_details(page, step, target_hunt, log):
    """Abre 'Detalhes' da hunt (dentro da lista de Hunts, ja aberta pelo
    chamador - clica a linha pra expandir, depois o botao 'Detalhes') e liga
    'Rastrear na tela' de cada criatura direto ali. Atualizacao do jogo
    passou a mostrar TODAS as criaturas da hunt nessa tela, cada uma com o
    MESMO botao/classe ('.cyc-track'/'.on') que o Cyclopedia > Bestiary -
    nao precisa mais abrir o Cyclopedia e buscar uma por uma (era 1 busca +
    1 ida-e-volta por monstro; agora e' 1 tela so' pra hunt inteira).
    Retorna (monster_names, already_complete) - already_complete e' um set
    (nomes em minusculo) de quem ja bateu o total de kills."""
    row_selector = step.get("row_selector", ".stage-row")
    hunt_name_selector = step.get("name_selector", ".stage-name-line b")
    details_btn_selector = step.get("hunt_details_button_selector", ".stage-details")
    modal_selector = step.get("hunt_details_modal_selector", "#hunt-details-modal")
    close_selector = step.get("hunt_details_close_selector", "#hunt-details-modal-close")
    card_selector = step.get("hunt_details_card_selector", ".hd-card")
    card_name_selector = step.get("hunt_details_card_name_selector", ".hd-card-name")
    card_kills_selector = step.get("hunt_details_card_kills_selector", ".hd-card-kills")
    track_selector = step.get("hunt_details_track_selector", ".cyc-track")

    safe_hunt = target_hunt.strip().replace('"', '\\"')
    row_scope = f'{row_selector}:has({hunt_name_selector}:text-is("{safe_hunt}"))'

    try:
        details_btn = page.query_selector(f"{row_scope} {details_btn_selector}")
        if details_btn is None or not details_btn.is_visible():
            # a linha precisa estar expandida (clicada) antes do botao
            # 'Detalhes' ficar visivel - mesmo padrao ja visto no botao
            # 'Enfrentar' dos chefes e 'Caçar' das hunts.
            page.click(row_scope, timeout=3000)
            time.sleep(0.3)
        page.click(f"{row_scope} {details_btn_selector}", timeout=3000)
        page.wait_for_selector(f"{modal_selector} {card_selector}", timeout=4000)
    except Exception as error:
        log(f"  Erro ao abrir Detalhes da hunt '{target_hunt}': {error}")
        return [], set()

    # le nome + progresso + se ja esta rastreando de TODAS as criaturas numa
    # unica chamada (mesmo motivo de sempre - varias criaturas, cada leitura
    # separada e uma ida-e-volta pelo protocolo de depuracao).
    cards = page.evaluate(
        """([cardSel, nameSel, killsSel, trackSel]) => Array.from(document.querySelectorAll(cardSel)).map(card => ({
            name: card.querySelector(nameSel)?.textContent?.trim() || '',
            kills: card.querySelector(killsSel)?.textContent || '',
            tracking: card.querySelector(trackSel)?.classList.contains('on') || false,
        }))""",
        [card_selector, card_name_selector, card_kills_selector, track_selector],
    )

    monsters = []
    already_complete = set()
    tracked = 0
    for card in cards:
        name = card["name"]
        if not name:
            continue
        monsters.append(name)

        kill_match = re.search(r"([\d.]+)\s*/\s*([\d.]+)", card["kills"] or "")
        if kill_match and kill_match.group(1).replace(".", "") == kill_match.group(2).replace(".", ""):
            already_complete.add(name.lower())

        if card["tracking"]:
            continue
        try:
            safe_name = name.replace('"', '\\"')
            btn_selector = f'{card_selector}:has({card_name_selector}:text-is("{safe_name}")) {track_selector}'
            page.click(btn_selector, timeout=3000)
            tracked += 1
        except Exception as error:
            log(f"  Erro ao ligar rastreio de '{name}': {error}")

    if tracked:
        log(f"  {tracked} criatura(s) marcada(s) pra rastrear (direto pelos Detalhes da hunt).")

    try:
        page.click(close_selector, timeout=2000)
    except Exception:
        page.keyboard.press("Escape")

    return monsters, already_complete


def bestiary_all_complete(page, monster_names, already_complete, step, log):
    """Confere, so lendo o quadro do HUD (sem abrir nenhum painel), se todos os
    monstros da hunt atual ja bateram o total de kills do Bestiary."""
    remaining = [name for name in monster_names if name.strip().lower() not in already_complete]
    if not remaining:
        return True  # todos ja estavam 'Completo' quando foram checados

    wanted = {name.strip().lower() for name in remaining}
    row_selector = step.get("bestiary_overlay_row_selector", "#bestiarytrack-overlay .bsk-row")
    name_selector = step.get("bestiary_overlay_name_selector", ".gtk-hunt-name")
    count_selector = step.get("bestiary_overlay_count_selector", ".gtk-count")

    found = {}
    for row in page.query_selector_all(row_selector):
        name_el = row.query_selector(name_selector)
        name = (name_el.text_content() or "").strip().lower() if name_el else ""
        if name not in wanted:
            continue
        count_el = row.query_selector(count_selector)
        count_text = (count_el.text_content() or "") if count_el else ""
        match = re.search(r"([\d.]+)\s*/\s*([\d.]+)", count_text)
        if match:
            done = int(match.group(1).replace(".", ""))
            target = int(match.group(2).replace(".", ""))
            found[name] = done >= target

    return len(found) == len(wanted) and all(found.values())


def peek_next_hunt(page, current_hunt, open_selector, hunts_selector, row_selector, name_selector, log, done_selector=".stage-done"):
    """Acha o NOME da proxima fase pra sugerir no avanco, sem clicar em
    'Cacar' - so olha e fecha a lista de novo. Prioriza a primeira fase
    depois da atual que ainda esta 'vazia' (sem contador 'Nx' de vezes
    concluida, ex: '✓ 37x' em '.stage-done' - ou seja, o chefe dela nunca foi
    derrotado) - a atual acumula contador enquanto joga nela, a ideia e
    espalhar progresso pras fases que ainda nao tem nenhum. NAO pula fases
    com classe 'locked' - confirmado que da pra cacar nelas mesmo assim (a
    hunt ATUAL do personagem costuma estar marcada 'locked' tambem); por
    isso o avanco so acontece com confirmacao do usuario no popup, que pode
    escolher ir mesmo assim - se o jogo realmente bloquear, o clique em
    'Cacar' em 'find_and_go_to_hunt' so vai falhar/ficar desabilitado, sem
    forcar nada. Se nenhuma fase 'vazia' existir depois da atual (todas ja
    tem pelo menos 1x), cai pra primeira da lista mesmo assim. Usado pra
    saber o que perguntar no popup de confirmacao ANTES de avancar de
    verdade (ver HUNT_ADVANCE_CONFIRM); quem navega de fato, apos a
    confirmacao, e 'find_and_go_to_hunt'."""
    try:
        click_open_wave(page, open_selector)
        page.click(hunts_selector, timeout=3000)
        page.wait_for_selector(row_selector, timeout=4000)
        clear_hunt_search(page)
    except Exception as error:
        log(f"  Erro ao abrir a lista de Hunts pra ver a proxima: {error}")
        return None

    # acha o NOME da proxima fase sugerida numa unica chamada (mesmo motivo
    # de 'find_and_go_to_hunt' - dezenas de fases, cada leitura separada e
    # uma ida-e-volta pelo protocolo de depuracao).
    next_name = page.evaluate(
        """([rowSel, nameSel, doneSel, current]) => {
            const rows = Array.from(document.querySelectorAll(rowSel));
            let foundCurrent = false;
            let fallback = null;
            for (let i = 0; i < rows.length; i++) {
                const name = (rows[i].querySelector(nameSel)?.textContent || '').trim();
                if (foundCurrent) {
                    if (fallback === null) fallback = name;
                    if (!rows[i].querySelector(doneSel)) return name;
                    continue;
                }
                if (name === current) foundCurrent = true;
            }
            return fallback;
        }""",
        [row_selector, name_selector, done_selector, current_hunt],
    )
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    return next_name


def start_first_available_hunt(page, open_selector, hunts_selector, row_selector, name_selector, go_selector, log):
    """Acha a primeira fase DISPONIVEL (pula 'locked') na lista de Hunts e
    clica em 'Cacar' - usado quando o bot inicia (ou o personagem fica) sem
    nenhuma hunt ativa, ex: parado na cidade. Retorna True se conseguiu
    comecar a cacar em alguma fase."""
    try:
        click_open_wave(page, open_selector)
        page.click(hunts_selector, timeout=3000)
        page.wait_for_selector(row_selector, timeout=4000)
        clear_hunt_search(page)
    except Exception as error:
        log(f"  Erro ao abrir a lista de Hunts: {error}")
        return False

    # acha o INDICE da primeira fase disponivel numa unica chamada (mesmo
    # motivo de 'find_and_go_to_hunt').
    result = page.evaluate(
        """([rowSel, nameSel]) => {
            const rows = Array.from(document.querySelectorAll(rowSel));
            const index = rows.findIndex(row => !row.classList.contains('locked'));
            if (index === -1) return null;
            return [index, (rows[index].querySelector(nameSel)?.textContent || '').trim()];
        }""",
        [row_selector, name_selector],
    )
    target_row, target_name = None, None
    if result is not None:
        target_index, target_name = result
        rows = page.query_selector_all(row_selector)
        if target_index < len(rows):
            target_row = rows[target_index]

    if target_row is None:
        log("  Nao encontrei nenhuma hunt disponivel pra comecar.")
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        return False

    try:
        go_button = target_row.query_selector(go_selector)
        if go_button is None or not go_button.is_visible():
            target_row.click(timeout=3000)
            time.sleep(0.3)
            go_button = target_row.query_selector(go_selector)
        if go_button is not None and go_button.is_enabled():
            go_button.click(timeout=5000)
            log(f"  Sem hunt ativa - comecando a cacar em '{target_name}'.")
            return True
        log(f"  Botao 'Cacar' indisponivel pra '{target_name}'.")
    except Exception as error:
        log(f"  Erro ao comecar a cacar em '{target_name}': {error}")
    return False


def read_hunt_list(page, log, open_selector="#wave-title", hunts_selector='.tp-opt[data-tp="hunts"]',
                    row_selector=".stage-row", name_selector=".stage-name-line b"):
    """Abre o menu de teleportes > Hunts e le, de cada fase, na mesma ordem
    que aparece na tela: nome, se esta 'locked', nivel recomendado, se rende
    mais EXP ou LOOT ('.stage-lean') e quantas vezes ja foi concluida
    ('.stage-done', vazio se nunca) - usado pelo HuntPicker na GUI, que
    reproduz esses mesmos filtros (Todas/EXP/LOOT/Concluidas/Pendentes) que o
    proprio jogo mostra, a partir da lista REAL da conta, nao uma lista fixa
    no codigo."""
    try:
        click_open_wave(page, open_selector)
        page.click(hunts_selector, timeout=3000)
        page.wait_for_selector(row_selector, timeout=4000)
        clear_hunt_search(page)
    except Exception as error:
        log(f"  Erro ao abrir a lista de Hunts: {error}")
        return []

    raw = page.evaluate(
        """(sel) => Array.from(document.querySelectorAll(sel)).map(row => {
            const name = row.querySelector('.stage-name-line b')?.textContent?.trim() || '';
            const lvlText = row.querySelector('.stage-lvl')?.textContent || '';
            const lvlMatch = lvlText.match(/(\\d+)/);
            const lean = row.querySelector('.stage-lean');
            const doneEl = row.querySelector('.stage-done');
            const doneMatch = (doneEl?.textContent || '').match(/(\\d+)/);
            return {
                name,
                locked: row.classList.contains('locked'),
                level: lvlMatch ? parseInt(lvlMatch[1], 10) : null,
                lean: lean ? (lean.className.replace('stage-lean', '').trim()) : '',
                done: !!doneEl,
                done_count: doneMatch ? parseInt(doneMatch[1], 10) : 0,
            };
        })""",
        row_selector,
    )

    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    return [h for h in raw if h["name"]]


def fetch_hunt_names(log=print):
    """Conecta no Chrome (abrindo se precisar) so pra ler a lista de Hunts e
    devolver - usado pelo botao 'Atualizar lista' do HuntPicker, que pode ser
    chamado com o bot rodando ou parado."""
    if not launch_browser(log):
        return []
    with sync_playwright() as playwright:
        page = connect_game_page(playwright)
        return read_hunt_list(page, log)


def read_boss_list(page, log, open_selector="#wave-title", boss_menu_selector='.tp-opt[data-tp="boss"]',
                   ready_selector=".pick-leanbtn.ready", row_selector=".boss-cell",
                   name_selector=".boss-cell-name", meta_selector=".boss-cell-meta"):
    """Abre a lista de Chefes do jogo e le TODOS (nome + level), na ordem da
    tela - usado pelo botao 'Atualizar lista' do BossPicker pra descobrir
    chefes novos apos uma atualizacao do jogo. Desliga o filtro 'Prontos' se
    estiver ligado (senao so listaria os prontos agora)."""
    opened = False
    last_error = None
    for _attempt in range(2):  # o menu de Teleportes as vezes ainda nao abriu no 1o clique
        try:
            click_open_wave(page, open_selector)
            page.click(boss_menu_selector, timeout=3000)
            page.wait_for_selector(row_selector, timeout=4000)
            ready_class = page.eval_on_selector(ready_selector, "el => el.className") or ""
            if "on" in ready_class.split():
                page.click(ready_selector, timeout=3000)
                page.wait_for_timeout(300)
            opened = True
            break
        except Exception as error:
            last_error = error
            for _ in range(3):
                page.keyboard.press("Escape")
            page.wait_for_timeout(400)
    if not opened:
        log(f"  Erro ao abrir a lista de Chefes: {last_error}")
        return []

    raw = page.evaluate(
        """([rowSel, nameSel, metaSel]) => Array.from(document.querySelectorAll(rowSel)).map(row => {
            const name = row.querySelector(nameSel)?.textContent?.trim() || '';
            const m = (row.querySelector(metaSel)?.textContent || '').match(/lvl\\s*(\\d+)/i);
            return {name, level: m ? parseInt(m[1], 10) : null};
        })""",
        [row_selector, name_selector, meta_selector],
    )
    for _ in range(3):
        page.keyboard.press("Escape")
    return [b for b in raw if b["name"]]


def fetch_boss_list(log=print):
    """Conecta no navegador (abrindo se precisar) so pra ler a lista de
    Chefes e devolver - usado pelo botao 'Atualizar lista' do BossPicker."""
    if not launch_browser(log):
        return []
    with sync_playwright() as playwright:
        page = connect_game_page(playwright)
        return read_boss_list(page, log)


def finish_completed_bestiary_tracks(page, step, log):
    """Desliga o rastreio de qualquer criatura no quadro do HUD do Bestiary
    que ja tenha batido o total de kills (contador com a classe extra 'ok').
    Roda todo ciclo, logo ao iniciar o bot inclusive - nao precisa esperar a
    hunt mudar pra liberar a vaga (limite de 5 rastreadas ao mesmo tempo)."""
    row_selector = step.get("bestiary_overlay_row_selector", "#bestiarytrack-overlay .bsk-row")
    name_selector = step.get("bestiary_overlay_name_selector", ".gtk-hunt-name")
    count_selector = step.get("bestiary_overlay_count_selector", ".gtk-count")

    for row in page.query_selector_all(row_selector):
        count_el = row.query_selector(count_selector)
        if count_el is None or "ok" not in (count_el.get_attribute("class") or "").split():
            continue
        name_el = row.query_selector(name_selector)
        name = (name_el.text_content() or "").strip() if name_el else "?"
        try:
            row.click(timeout=2000)
            log(f"  Bestiary de '{name}' completo - rastreio finalizado (vaga liberada).")
        except Exception as error:
            log(f"  Erro ao finalizar rastreio de '{name}' no Bestiary: {error}")


def execute_dom_hunt_bestiary_step(page, step, log):
    """Passo tipo 'dom_hunt_bestiary': quando a hunt ativa muda, descobre os
    monstros dela e liga 'Rastrear na tela' de cada um direto pela tela de
    'Detalhes' da hunt (ve read_and_track_hunt_details), parando de rastrear
    o que sobrou de hunts antigas (limite do jogo e 5 rastreadas ao mesmo
    tempo). Todo ciclo tambem finaliza (desliga) qualquer rastreio ja 100%
    completo, mesmo sem trocar de hunt. Depois, confere - so lendo o quadro do
    HUD, sem abrir painel nenhum - se o Codex E o Bestiary da hunt atual ja
    estao 100%. Se estiverem e a flag ADVANCE_MEMORY estiver ligada, pergunta
    (popup na GUI, via HUNT_ADVANCE_CONFIRM) se o usuario quer ir pra proxima
    hunt disponivel da lista - so avanca (e so ENTAO essa vira a nova hunt
    padrao) se ele confirmar."""
    try:
        current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
    except Exception:
        return True

    if current_hunt:
        LAST_KNOWN_HUNT_MEMORY["name"] = current_hunt

    open_selector = step["open_selector"]
    hunts_selector = step["hunts_selector"]
    row_selector = step.get("row_selector", ".stage-row")
    hunt_name_selector = step.get("name_selector", ".stage-name-line b")
    go_selector = step.get("go_selector", ".stage-go")

    if not current_hunt:
        if GUILD_TASK_MEMORY.get("grinding"):
            # ficou sem hunt ativa NO MEIO de uma task de guild (ex: falha
            # transitoria ao trocar) - tasks de guild tem prioridade maior que
            # o bestiary, entao nao escolhe uma hunt qualquer aqui; deixa a
            # propria rotina de Tarefas da Guild resolver no proximo ciclo
            # dela (ela sabe pra qual hunt ir por causa da task pendente).
            return True

        default_hunt = DEFAULT_HUNT_MEMORY.get("name")
        last_known = LAST_KNOWN_HUNT_MEMORY.get("name")
        started = False
        if default_hunt:
            # hunt padrao escolhida pelo usuario tem prioridade sobre 'a
            # ultima que estava ativa' - e a preferencia explicita dele pra
            # onde voltar sempre que sobrar sem nada mais prioritario (ex:
            # logo apos um chefe).
            log(f"  Sem hunt ativa agora - indo para a hunt padrao ('{default_hunt}')...")
            started = find_and_go_to_hunt(
                page, default_hunt,
                open_selector=open_selector, hunts_selector=hunts_selector,
                row_selector=row_selector, name_selector=hunt_name_selector,
                go_selector=go_selector, log=log,
            )
        if not started and last_known:
            # sem hunt ativa AGORA mas ja estava caçando antes (chefe, troca
            # de painel, erro pontual de leitura etc.) - volta pra ELA, nao
            # pra 'qualquer uma' da lista. So cai pra 'primeira disponivel'
            # se nunca esteve em hunt nenhuma.
            log(f"  Sem hunt ativa agora - voltando para a ultima hunt conhecida ('{last_known}')...")
            started = find_and_go_to_hunt(
                page, last_known,
                open_selector=open_selector, hunts_selector=hunts_selector,
                row_selector=row_selector, name_selector=hunt_name_selector,
                go_selector=go_selector, log=log,
            )
        if not started:
            started = start_first_available_hunt(page, open_selector, hunts_selector, row_selector, hunt_name_selector, go_selector, log)
        if not started:
            return True
        try:
            current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
        except Exception:
            return True
        if not current_hunt:
            return True
        LAST_KNOWN_HUNT_MEMORY["name"] = current_hunt

    finish_completed_bestiary_tracks(page, step, log)

    if BESTIARY_MEMORY.get("last_hunt") != current_hunt:
        monsters = []
        already_complete = set()
        try:
            click_open_wave(page, open_selector)
            page.click(hunts_selector, timeout=3000)
            page.wait_for_selector(row_selector, timeout=4000)
            clear_hunt_search(page)
            # le os monstros da hunt E liga 'Rastrear na tela' de cada um,
            # tudo na mesma tela de 'Detalhes' - ver read_and_track_hunt_details.
            monsters, already_complete = read_and_track_hunt_details(page, step, current_hunt, log)
        except Exception as error:
            log(f"  Erro ao ler os monstros da hunt '{current_hunt}': {error}")

        # fecha a lista de Hunts ANTES de mexer no quadro do HUD - o overlay
        # dela cobre a tela e bloqueia clique em qualquer coisa por baixo
        # enquanto estiver aberta (mesmo problema ja visto com o painel
        # Social/Guild).
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        time.sleep(0.3)

        untrack_unrelated_bestiary(page, monsters, step, log)

        BESTIARY_MEMORY["last_hunt"] = current_hunt
        BESTIARY_MEMORY["monsters"] = monsters
        BESTIARY_MEMORY["already_complete"] = already_complete
        HUNT_PROGRESS_MEMORY["advanced_from"] = None

    if not ADVANCE_MEMORY.get("enabled"):
        return True

    if GUILD_TASK_MEMORY.get("grinding"):
        # tasks de guild tem prioridade maior que o avanco automatico de hunt
        # (ordem: chefes > tasks de guild > bestiary > codex) - nao avanca
        # enquanto uma task ainda estiver sendo realizada, senao interrompe o
        # progresso dela no meio.
        return True

    monsters = BESTIARY_MEMORY.get("monsters") or []
    if not monsters:
        return True

    if HUNT_PROGRESS_MEMORY.get("advanced_from") == current_hunt:
        return True  # ja tentamos avancar dessa hunt - espera ela mudar antes de tentar de novo

    if COMPLETION_MEMORY.get("notified_hunt") != current_hunt:
        return True  # Codex dessa hunt ainda nao bateu 100%

    if not bestiary_all_complete(page, monsters, BESTIARY_MEMORY.get("already_complete", set()), step, log):
        return True  # Bestiary ainda nao bateu 100% em todos os monstros da hunt

    # Codex e Bestiary da hunt atual estao 100% - mas o avanco so acontece com
    # confirmacao do usuario (popup na GUI, ver HUNT_ADVANCE_CONFIRM). Chefes,
    # tasks de guild e venda continuam normais enquanto espera - so o avanco
    # de hunt fica parado.
    if HUNT_ADVANCE_CONFIRM.get("current_hunt") != current_hunt:
        next_name = peek_next_hunt(page, current_hunt, open_selector, hunts_selector, row_selector, hunt_name_selector, log)
        if next_name is None:
            log(f"  Codex e Bestiary de '{current_hunt}' completos, mas nao ha proxima hunt liberada.")
            HUNT_PROGRESS_MEMORY["advanced_from"] = current_hunt
            return True
        log(f"  Codex e Bestiary de '{current_hunt}' completos - aguardando confirmacao pra ir pra '{next_name}'...")
        HUNT_ADVANCE_CONFIRM["current_hunt"] = current_hunt
        HUNT_ADVANCE_CONFIRM["next_hunt"] = next_name
        HUNT_ADVANCE_CONFIRM["answer"] = None
        return True

    answer = HUNT_ADVANCE_CONFIRM.get("answer")
    if answer is None:
        return True  # ainda esperando a resposta do popup

    next_name = HUNT_ADVANCE_CONFIRM.get("next_hunt")
    HUNT_ADVANCE_CONFIRM["current_hunt"] = None
    HUNT_ADVANCE_CONFIRM["next_hunt"] = None
    HUNT_ADVANCE_CONFIRM["answer"] = None
    HUNT_PROGRESS_MEMORY["advanced_from"] = current_hunt

    if not answer:
        log(f"  Usuario optou por nao avancar agora - continua em '{current_hunt}'.")
        return True

    log(f"  Confirmado - avancando para '{next_name}'.")
    ok = find_and_go_to_hunt(
        page, next_name,
        open_selector=open_selector, hunts_selector=hunts_selector,
        row_selector=row_selector, name_selector=hunt_name_selector,
        go_selector=go_selector, log=log,
    )
    if ok:
        # a hunt confirmada vira a nova hunt padrao - e o que fecha o loop
        # pedido: da proxima vez que ESSA hunt bater 100%, pergunta de novo.
        DEFAULT_HUNT_MEMORY["name"] = next_name
        settings = load_settings()
        settings["default_hunt"] = next_name
        save_settings(settings)
    return True


VOCATION_KEYWORDS = ["Knight", "Paladin", "Sorcerer", "Druid", "Monk"]

# Nivel de cada vocacao na ULTIMA VEZ que a arvore de talentos foi realmente
# aberta e conferida - zera a cada reinicio do bot (nao e persistido). Usado
# pra decidir se vale a pena abrir a arvore de novo: se o nivel na party nao
# mudou desde essa checagem, os pontos disponiveis tambem nao podem ter
# aumentado sozinhos, entao nao ha motivo pra abrir o painel so pra ver a
# mesma coisa de novo.
BUILD_LEVEL_MEMORY = {}


def read_party_levels(page, log, party_selector="#party-list", member_selector=".member", meta_selector=".m-meta"):
    """Le o nivel atual de cada personagem na PARTY (rotulo tipo 'Knight ·
    lvl 317' em '.m-meta') sem abrir nenhum painel - a party ja fica visivel
    na tela principal o tempo todo. Retorna {vocacao: nivel}. So personagens
    que estao na party AGORA aparecem aqui (a conta pode ter ate 3, nem todos
    precisam estar partidos no momento) - quem nao aparece simplesmente nao
    entra no dict, e quem chama isso trata como 'precisa checar a arvore pra
    saber' nesse caso."""
    levels = {}
    try:
        for member in page.query_selector_all(f"{party_selector} {member_selector}"):
            meta_el = member.query_selector(meta_selector)
            text = (meta_el.text_content() or "") if meta_el else ""
            match = re.search(r"lvl\s*(\d+)", text, re.IGNORECASE)
            if not match:
                continue
            vocation = detect_vocation(text[: match.start()])
            if vocation:
                levels[vocation] = int(match.group(1))
    except Exception as error:
        log(f"  Erro ao ler nivel da party: {error}")
    return levels


def detect_vocation(full_name):
    """A tela de Build mostra o nome completo da vocacao evoluida (ex: 'Elite
    Knight') mas o site otimizador usa o nome curto (ex: 'Knight') - acha qual
    das 5 vocacoes esta contida no nome completo. Retorna None se nao achar
    nenhuma (ex: personagem ainda sem vocacao escolhida)."""
    lower = (full_name or "").lower()
    for voc in VOCATION_KEYWORDS:
        if voc.lower() in lower:
            return voc
    return None


def fetch_build_code(context, vocation, level, config, log):
    """Abre uma aba SEPARADA (mesmo navegador, nao mexe na aba do jogo) pro
    site otimizador (baiakidle-build-optimizer.pages.dev), preenche os
    parametros da vocacao e le o codigo de build gerado (ex:
    'BT1-K500-1000...'). As opcoes de Foco (e se 'pegar XP'/'pegar Loot' sao
    escolhas de verdade) mudam de acordo com a Vocacao+Modo - ex: Off-tank e
    PvP travam o foco numa unica opcao (campo fica desabilitado), e Tank usa
    um conjunto de opcoes bem diferente do DPS (ex: 'tank_fire' em vez de
    'elemental_weapon'). Por isso: so mexe em Foco/XP/Loot se o campo estiver
    HABILITADO, e so seleciona o foco configurado se ele realmente estiver
    entre as opcoes disponiveis pra essa combinacao - senao deixa o valor que
    o proprio site ja escolheu sozinho. Fecha a aba antes de retornar. Retorna
    o codigo (string) ou None se der erro."""
    site_page = context.new_page()
    try:
        site_page.goto("https://baiakidle-build-optimizer.pages.dev/", timeout=15000)
        site_page.click(f'.voc-choice:has-text("{vocation}")', timeout=5000)
        site_page.fill("#level", str(level), timeout=5000)
        site_page.select_option("#buildMode", config.get("mode", "dps"), timeout=5000)
        site_page.wait_for_timeout(300)  # o site pode trocar as opcoes de foco ao mudar o modo

        focus_value = config.get("focus") or ""
        focus_el = site_page.query_selector("#elementFocus")
        if focus_value and focus_el is not None and not focus_el.is_disabled():
            available = [opt.get_attribute("value") for opt in focus_el.query_selector_all("option")]
            if focus_value in available:
                site_page.select_option("#elementFocus", focus_value, timeout=5000)
                site_page.wait_for_timeout(200)  # pode liberar foco secundario logo em seguida
            else:
                log(f"  Foco '{focus_value}' nao disponivel pra {vocation}/{config.get('mode', 'dps')} - usando o padrao do site.")

        # so algumas vocacoes (ex: Sorcerer com foco elemental "puro") liberam
        # combinar mais 1-2 elementos (foco secundario, depois terciario) -
        # mesma logica defensiva: so mexe se o campo estiver habilitado e o
        # valor configurado realmente estiver entre as opcoes.
        secondary_value = config.get("secondary_focus") or ""
        secondary_el = site_page.query_selector("#secondaryFocus")
        if secondary_value and secondary_el is not None and not secondary_el.is_disabled():
            available_sec = [opt.get_attribute("value") for opt in secondary_el.query_selector_all("option")]
            if secondary_value in available_sec:
                site_page.select_option("#secondaryFocus", secondary_value, timeout=5000)
                site_page.wait_for_timeout(200)  # pode liberar foco terciario logo em seguida

                tertiary_value = config.get("tertiary_focus") or ""
                tertiary_el = site_page.query_selector("#tertiaryFocus")
                if tertiary_value and tertiary_el is not None and not tertiary_el.is_disabled():
                    available_ter = [opt.get_attribute("value") for opt in tertiary_el.query_selector_all("option")]
                    if tertiary_value in available_ter:
                        site_page.select_option("#tertiaryFocus", tertiary_value, timeout=5000)
                    else:
                        log(f"  Foco terciario '{tertiary_value}' nao disponivel - ignorando.")
            else:
                log(f"  Foco secundario '{secondary_value}' nao disponivel pra {vocation}/{config.get('mode', 'dps')} - ignorando.")

        force_xp = site_page.query_selector("#forceXp")
        if force_xp is not None and not force_xp.is_disabled() and force_xp.is_checked() != bool(config.get("force_xp")):
            force_xp.click(timeout=5000)
        force_loot = site_page.query_selector("#forceLoot")
        if force_loot is not None and not force_loot.is_disabled() and force_loot.is_checked() != bool(config.get("force_loot")):
            force_loot.click(timeout=5000)

        site_page.wait_for_timeout(800)  # recalculo do codigo e client-side, rapido, mas assincrono
        code_el = site_page.query_selector("#buildCode")
        code = (code_el.text_content() or "").strip() if code_el else ""
        return code or None
    except Exception as error:
        log(f"  Erro ao consultar o otimizador de build: {error}")
        return None
    finally:
        site_page.close()


def open_tree_and_select_char(page, open_selector, tree_tab_selector, char_selector, vocation, log):
    """Abre Progressao > Build e seleciona, dentro dela, o personagem cujo
    'data-tip' contem 'vocation' (ex: 'Cibele Druid (Druid)' pra vocation
    'Druid') - a conta pode ter ate 3 personagens, cada um com sua propria
    arvore, e clicar no icone de um deles troca toda a tela (pontos, botao
    Importar) pra refletir aquele personagem especifico, mesmo que nao seja o
    que esta na party agora. Retorna o elemento do personagem selecionado, ou
    None se nao achou/deu erro."""
    try:
        # depois de consultar o site otimizador numa aba separada, a aba do
        # jogo fica em segundo plano - o Chrome throttla renderizacao de abas
        # em background, entao o React demora (ou nunca) atualiza a lista de
        # personagens. Traz a aba do jogo de volta pro primeiro plano antes de
        # mexer nela.
        page.bring_to_front()
        click_open_wave(page, open_selector)
        page.click(tree_tab_selector, timeout=3000)
        page.wait_for_selector(char_selector, timeout=4000)
    except Exception as error:
        log(f"  Erro ao abrir Progressao/Build: {error}")
        return None

    # 'data-tip' e uma tooltip que so e preenchida depois que o mouse passa
    # em cima do botao (nao vem pronta no HTML) - por isso precisa dar hover
    # em cada personagem antes de ler o atributo, senao fica vazio/None pra
    # quem nunca foi "tocado".
    target = None
    for char_el in page.query_selector_all(char_selector):
        try:
            char_el.hover(timeout=2000)
        except Exception:
            pass
        if detect_vocation(char_el.get_attribute("data-tip")) == vocation:
            target = char_el
            break
    if target is None:
        return None

    if "active" not in (target.get_attribute("class") or "").split():
        try:
            target.click(timeout=2000)
            page.wait_for_timeout(400)
        except Exception as error:
            log(f"  Erro ao selecionar o personagem '{vocation}' na arvore: {error}")
            return None

    return target


def execute_dom_auto_build_step(page, step, log):
    """Passo tipo 'dom_auto_build': em Progressao > Build, passa por TODOS os
    personagens da conta (ate 3, cada um pode estar ou nao na party agora -
    ve 'open_tree_and_select_char'). Pra cada um cuja vocacao tenha uma config
    ligada e pontos disponiveis suficientes ('min_points'), consulta o site
    otimizador (numa aba separada, ve 'fetch_build_code') pra pegar o codigo
    de build ideal pro level atual (pontos gastos + disponiveis) e importa
    esse codigo de volta no jogo (botao 'Importar' > cola o codigo >
    'Carregar'). O jogo pede confirmacao antes de aplicar (mesmo popup
    generico #confirm-yes/#confirm-no usado em outras acoes do jogo) - a
    mensagem avisa que a build atual sera SUBSTITUIDA e informa o custo em
    gold da troca; o bot le essa mensagem e confirma (a menos que contenha
    alguma das DANGEROUS_CONFIRM_KEYWORDS, mesma protecao usada em qualquer
    outro clique nesse botao compartilhado).

    ANTES de abrir a arvore (que e um painel pesado - varios cliques, troca
    de personagem, as vezes uma aba nova pro site otimizador), confere o
    nivel de cada vocacao ligada pelo painel de Party (BUILD_LEVEL_MEMORY vs
    'read_party_levels', ambos leves - a party ja fica visivel na tela sem
    precisar abrir nada). So abre a arvore de novo quando o personagem tiver
    subido PELO MENOS 'min_points' niveis desde a ultima checagem real (nao
    a cada level up - min_points ja e o numero de pontos que o usuario
    configurou como necessario pra valer a pena mexer, entao e tambem o
    intervalo certo de espera, ja que 1 level = 1 ponto). BUILD_LEVEL_MEMORY
    comeca vazia a cada reinicio do bot, entao a primeira rodada apos iniciar
    sempre confere todo mundo de novo (guarda o nivel inicial ali)."""
    open_selector = step.get("open_selector", "#tab-progressao")
    tree_tab_selector = step.get("tree_tab_selector", "#tab-tree")
    char_selector = step.get("char_active_selector", ".tree-char")
    pts_selector = step.get("pts_selector", ".tree-chip.pts")
    spent_selector = step.get("spent_selector", ".tree-chip:not(.pts)")
    import_btn_selector = step.get("import_btn_selector", ".tree-code-btn.import")
    import_input_selector = step.get("import_input_selector", ".tree-code-in")
    load_btn_selector = step.get("load_btn_selector", ".tree-code-row .tree-code-btn")
    msg_selector = step.get("msg_selector", ".tree-code-msg")
    confirm_body_selector = step.get("confirm_body_selector", "#confirm-modal-body")
    confirm_yes_selector = step.get("confirm_yes_selector", "#confirm-yes")

    enabled_configs = {c["vocation"]: c for c in step["configs"] if c.get("enabled")}
    if not enabled_configs:
        return True

    party_levels = read_party_levels(page, log)
    pending_configs = {}
    for vocation, config in enabled_configs.items():
        current_level = party_levels.get(vocation)
        baseline_level = BUILD_LEVEL_MEMORY.get(vocation)
        if current_level is not None and baseline_level is not None:
            min_points = config.get("min_points", 1)
            if current_level - baseline_level < min_points:
                continue  # ainda nao subiu 'min_points' niveis desde a ultima checagem - nada novo pra ver
        pending_configs[vocation] = config

    if not pending_configs:
        return True

    # antes abria a arvore 1a vez SO' pra descobrir quais vocacoes existem na
    # conta (hover em cada personagem, fechar) e depois abria de novo pra
    # cada vocacao pendente - 'open_tree_and_select_char' ja faz essa mesma
    # descoberta (e retorna None se a vocacao nao existir), entao essa 1a
    # rodada so' duplicava trabalho. Itera direto 'pending_configs'.
    for vocation, config in pending_configs.items():
        char_el = open_tree_and_select_char(page, open_selector, tree_tab_selector, char_selector, vocation, log)
        if char_el is None:
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            continue

        pts_el = page.query_selector(pts_selector)
        pts_match = re.search(r"(\d+)", (pts_el.text_content() or "") if pts_el else "")
        available = int(pts_match.group(1)) if pts_match else 0

        spent_el = page.query_selector(spent_selector)
        spent_match = re.search(r"([\d.]+)", (spent_el.text_content() or "") if spent_el else "")
        spent = int(spent_match.group(1).replace(".", "")) if spent_match else 0
        # marca que essa vocacao foi checada NESSE nivel - so abre a arvore
        # de novo quando ele subir mais, tenha batido 'min_points' agora ou nao.
        BUILD_LEVEL_MEMORY[vocation] = spent + available

        min_points = config.get("min_points", 1)
        if available < min_points:
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            continue

        level = spent + available

        log(f"  {vocation}: {available} ponto(s) disponivel(is) (level ~{level}) - consultando build ideal...")
        code = fetch_build_code(page.context, vocation, level, config, log)
        if not code:
            log(f"  Nao consegui obter o codigo de build pra '{vocation}'.")
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            continue

        # a arvore continua aberta no personagem certo (nao precisa fechar e
        # reabrir so' porque 'fetch_build_code' mexeu numa aba SEPARADA, sem
        # relacao nenhuma com essa) - so traz a aba do jogo de volta pro
        # primeiro plano (o Chrome throttla renderizacao de abas em 2o plano
        # enquanto a aba do site otimizador ficou em foco).
        page.bring_to_front()

        try:
            # o botao 'Importar' e um toggle (abre/fecha o campo de colar) - se
            # ja estiver aberto (residuo de um ciclo anterior que nao fechou
            # direito), clicar de novo FECHA em vez de abrir. So clica se ainda
            # nao estiver ativo (mesmo padrao de 'dom_ensure_active').
            import_btn = page.query_selector(import_btn_selector)
            if import_btn is not None and "active" not in (import_btn.get_attribute("class") or "").split():
                page.click(import_btn_selector, timeout=5000)
                page.wait_for_timeout(300)

            page.fill(import_input_selector, code, timeout=3000)
            page.click(load_btn_selector, timeout=3000)
            page.wait_for_timeout(500)

            # importar troca a build de verdade (custa gold) - o jogo pede
            # confirmacao pelo MESMO popup generico usado em outras acoes
            # (#confirm-yes/#confirm-no). So aparece quando a build realmente
            # muda - se for identica a atual, o site so avisa e nao pede nada.
            confirm_body = page.query_selector(confirm_body_selector)
            if confirm_body is not None and confirm_body.is_visible():
                confirm_text = (confirm_body.text_content() or "").strip()
                if any(word in confirm_text.lower() for word in DANGEROUS_CONFIRM_KEYWORDS):
                    log(f"  BLOQUEADO por seguranca: confirmacao da build diz '{confirm_text}' - nao clicado.")
                else:
                    log(f"  Confirmando: {confirm_text}")
                    page.click(confirm_yes_selector, timeout=3000)
                    page.wait_for_timeout(500)

            msg_el = page.query_selector(msg_selector)
            msg = (msg_el.text_content() or "").strip() if msg_el else ""
            log(f"  Build de '{vocation}' importada ({code}). {msg}".strip())
            play_achievement_sound()
            record_activity(f"Build de '{vocation}' atualizada! ({code})")
        except Exception as error:
            log(f"  Erro ao importar a build de '{vocation}': {error}")
        finally:
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")

    return True


_CONNECTION_DEAD_SIGNATURES = (
    "Connection closed",
    "Target page, context or browser has been closed",
    "Target closed",
    "Browser has been closed",
)


def is_connection_dead_error(error):
    """True se o erro indica que a conexao com o navegador (ou o processo
    driver do Playwright em si) morreu de vez - nao um problema pontual de
    UM elemento/tela isolado. CONFIRMADO ao vivo como causa real do bot
    ficando 'zumbi': praticamente toda checagem/rotina tem seu proprio
    try/except que so loga o erro e segue (de proposito - pra um problema
    pontual nao derrubar a sessao inteira), entao um erro desses nunca
    chegava ate o codigo de reconexao em run() - o bot ficava preso
    reportando o mesmo erro (ou simplesmente parava de logar, travado numa
    chamada que nunca retornava) ate alguem reiniciar na mao."""
    text = str(error)
    return any(sig in text for sig in _CONNECTION_DEAD_SIGNATURES)


def dismiss_blocking_overlays(page, log):
    """Fecha telas que travam o jogo INTEIRO se aparecerem - sem isso o bot
    para de vender/separar loot e seguir as rotinas ate alguem fechar na mao
    (foi exatamente o problema relatado: o jogo caiu, ficou preso numa
    dessas telas, e nada mais rodou ate reiniciar). Roda a CADA TICK do loop
    principal, ANTES de qualquer rotina.

    - Nota de atualizacao do jogo ('Novidades', '#changelog-modal'): aparece
      sozinha quando o jogo lanca uma atualizacao enquanto o bot ja esta
      rodando - CONFIRMADO ao vivo travando tudo (mesmo efeito do Mercado
      esquecido aberto: cobre a tela e todo clique por baixo falha) ate
      alguem fechar na mao. So clicar em '#changelog-modal-close' ('Fechar')
      - nao precisa de periodo de graca tipo o Mercado, ninguem "usa" essa
      tela de proposito por muito tempo.
    - Resumo de treino offline ('Bem-vindo de volta', '#offline-modal'): so
      clicar em '#offline-modal-close' (botao 'Coletar').
    - Conexao perdida ('#conn-overlay' - confirmado lendo o bundle JS do
      proprio jogo, funcao que mostra essa tela): o jogo recarrega a pagina
      SOZINHO depois de ~5s ('#conn-hint' mostra a contagem regressiva,
      'location.reload()' automatico). Clicar em '#conn-retry' ('Reconectar
      agora') faz exatamente a mesma coisa, so que na hora, sem esperar.
    - Mercado/leilao aberto ('#auction-modal', aba 'Market') deixado aberto
      fica bloqueando QUALQUER clique por baixo dele (Hunts, Guild,
      Codex...). CONFIRMADO ao vivo (log de 12h+ bloqueado) como causa real
      de chefes/tasks de guild parecendo 'travados' ou 'pulados' - a rotina
      abria a tela certa mas todo clique dentro falhava por causa desse
      modal por cima. Fecha em '#auction-modal-close' ('Fechar') - MAS so
      depois de ficar aberto continuamente por AUCTION_MODAL_GRACE_SECONDS:
      o usuario pode estar usando o mercado na hora (bug real ja visto: a
      primeira versao fechava na hora, expulsando o usuario do mercado
      enquanto ele ainda estava navegando nele).

    Retorna True se algum desses overlays estava visivel agora - quem chama
    deve pular o resto do tick nesse caso (a pagina pode estar recarregando
    ou o modal pode ter coberto os elementos que uma rotina tentaria usar)."""
    try:
        changelog_close = page.query_selector("#changelog-modal-close")
        if changelog_close is not None and changelog_close.is_visible():
            changelog_close.click(timeout=3000)
            log("  Fechei a tela de novidades/atualizacao do jogo.")
            return True
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        log(f"  Erro ao fechar a tela de novidades: {error}")

    try:
        offline_close = page.query_selector("#offline-modal-close")
        if offline_close is not None and offline_close.is_visible():
            offline_close.click(timeout=3000)
            log("  Fechei o resumo de treino offline ('Bem-vindo de volta').")
            return True
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        log(f"  Erro ao fechar o resumo de treino offline: {error}")

    try:
        overlay = page.query_selector("#conn-overlay")
        if overlay is not None and "hidden" not in (overlay.get_attribute("class") or "").split():
            log("  Conexao perdida - clicando em 'Reconectar agora'...")
            retry_btn = page.query_selector("#conn-retry")
            if retry_btn is not None:
                retry_btn.click(timeout=3000)
            return True
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        log(f"  Erro ao lidar com a tela de conexao perdida: {error}")

    try:
        auction_modal = page.query_selector("#auction-modal")
        is_open = auction_modal is not None and "hidden" not in (auction_modal.get_attribute("class") or "").split()
        if is_open:
            now = time.monotonic()
            first_seen = AUCTION_MODAL_MEMORY["first_seen_open"]
            if first_seen is None:
                AUCTION_MODAL_MEMORY["first_seen_open"] = now
            elif now - first_seen >= AUCTION_MODAL_GRACE_SECONDS:
                # ficou aberto tempo demais - trata como esquecido/travado
                # (nao alguem usando na hora) e fecha sozinho.
                log(f"  Mercado/leilao aberto ha mais de {AUCTION_MODAL_GRACE_SECONDS // 60}min - fechando...")
                close_btn = page.query_selector("#auction-modal-close")
                if close_btn is not None:
                    close_btn.click(timeout=3000)
                AUCTION_MODAL_MEMORY["first_seen_open"] = None
                return True
            # dentro do periodo de graca - pode ser o usuario usando o
            # mercado na hora, nao mexe em nada (rotinas que precisarem de
            # telas por baixo dele so falham essa rodada e tentam de novo
            # depois, igual sempre fizeram antes desse tratamento existir).
            return False
        else:
            AUCTION_MODAL_MEMORY["first_seen_open"] = None
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        log(f"  Erro ao lidar com o Mercado/leilao: {error}")

    return False


def return_to_default_hunt(page, log, force=False):
    """Se houver hunt padrao configurada (DEFAULT_HUNT_MEMORY) e o
    personagem estiver numa hunt ativa DIFERENTE dela (ou sem hunt nenhuma),
    troca pra ela. Retorna True se ha uma hunt padrao configurada (mesmo que
    nao precisasse trocar por ja estar nela), False se nao ha nenhuma - quem
    chama usa isso pra saber se deve cair num fallback (ex: ultima hunt
    conhecida).

    'force=True' ignora 'Avancar hunt automaticamente' e troca de qualquer
    jeito - usado logo apos uma interrupcao de prioridade maior terminar
    (chefes, tasks de guild): a hunt padrao e a 'base' pra onde SEMPRE volta
    depois de uma interrupcao, mesmo que o avanco automatico tivesse
    explorado outras hunts antes dela comecar. 'force=False' (usado no tick
    periodico de 'ensure_active_hunt') respeita o avanco automatico -
    enquanto nada interrompe, ele pode ir explorando livremente a partir da
    hunt padrao, sem essa checagem periodica brigando com ele a cada 30s.

    Recarrega DEFAULT_HUNT_MEMORY do settings.json a cada chamada (custa
    pouco - so um arquivo pequeno) em vez de confiar so na copia em memoria
    atualizada pela GUI - bug real ja visto: a hunt padrao ficava vazia na
    memoria do processo rodando, fazendo cair no fallback errado mesmo com a
    hunt padrao ja salva no arquivo."""
    DEFAULT_HUNT_MEMORY["name"] = load_settings().get("default_hunt", "")
    default_hunt = DEFAULT_HUNT_MEMORY.get("name")
    if not default_hunt:
        return False
    if not force and ADVANCE_MEMORY.get("enabled"):
        return True  # tem hunt padrao configurada, mas o avanco automatico tem prioridade agora
    if TRAINING_MEMORY.get("waiting"):
        # esperando a stamina recuperar (ate o limiar de 'Voltar a Cacar') pra
        # voltar pra hunt de antes do treino - sem isso, 'Treino online' era
        # visto como 'hunt errada' e o personagem era puxado de volta na hora,
        # antes da stamina realmente recuperar ate o limiar configurado.
        return True

    try:
        current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return True
    if current_hunt:
        LAST_KNOWN_HUNT_MEMORY["name"] = current_hunt
    if current_hunt == default_hunt:
        return True

    if current_hunt:
        log(f"  Na hunt '{current_hunt}', mas a hunt padrao e '{default_hunt}' - indo pra ela...")
    else:
        log(f"  Sem hunt ativa - indo para a hunt padrao '{default_hunt}'...")
    find_and_go_to_hunt(
        page,
        default_hunt,
        open_selector="#wave-title",
        hunts_selector='.tp-opt[data-tp="hunts"]',
        row_selector=".stage-row",
        name_selector=".stage-name-line b",
        go_selector=".stage-go",
        log=log,
    )
    return True


def ensure_active_hunt(page, log):
    """Garante que o personagem esteja numa hunt sempre que nao ha nada de
    prioridade maior acontecendo (sem task de guild em andamento). Roda a
    CADA TICK do loop principal, independente de quais rotinas estao
    ligadas - antes, esse comportamento so existia dentro da rotina do
    Bestiary (so cobria 'sem hunt nenhuma'), entao quem nao tinha ela ligada
    (ou o bot ainda nao tinha chegado nela nesse ciclo) via o personagem
    ficar parado numa hunt errada mesmo com uma hunt padrao configurada.

    Delega pra 'return_to_default_hunt' (force=False - respeita o avanco
    automatico, ve o docstring dela) tanto pro caso 'sem hunt ativa' quanto
    'em hunt ativa mas errada'. So cai pra ultima hunt conhecida
    (LAST_KNOWN_HUNT_MEMORY) se nao houver hunt padrao configurada."""
    if GUILD_TASK_MEMORY.get("grinding"):
        grinding_since = GUILD_TASK_MEMORY.get("grinding_since")
        stuck = grinding_since is not None and time.monotonic() - grinding_since >= GUILD_TASK_GRINDING_MAX_SECONDS
        if not stuck:
            return  # task de guild em andamento tem prioridade - ela mesma resolve a hunt
        # preso ha tempo demais (ex: a volta pra hunt padrao apos a task
        # falhou e nunca conseguiu desligar 'grinding' sozinha) - libera aqui
        # como rede de seguranca, em vez de ficar bloqueado pra sempre.
        log(f"  'grinding' de task da guild preso ha mais de {GUILD_TASK_GRINDING_MAX_SECONDS // 60}min - liberando.")
        GUILD_TASK_MEMORY["grinding"] = False
        GUILD_TASK_MEMORY["previous_hunt"] = None
        GUILD_TASK_MEMORY["grinding_since"] = None

    try:
        current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return

    if current_hunt:
        LAST_KNOWN_HUNT_MEMORY["name"] = current_hunt
        return_to_default_hunt(page, log, force=False)
        return

    if return_to_default_hunt(page, log, force=True):
        return  # tinha hunt padrao configurada (foi usada, ou ja tentou)

    target = LAST_KNOWN_HUNT_MEMORY.get("name")
    if not target:
        return  # nunca esteve em hunt nenhuma e nao tem padrao - deixa o Bestiary (se ligado) escolher a primeira disponivel
    log(f"  Sem hunt ativa - voltando para a ultima hunt conhecida ('{target}')...")
    find_and_go_to_hunt(
        page,
        target,
        open_selector="#wave-title",
        hunts_selector='.tp-opt[data-tp="hunts"]',
        row_selector=".stage-row",
        name_selector=".stage-name-line b",
        go_selector=".stage-go",
        log=log,
    )


def execute_step(page, step, stop_event, log, all_routines=None):
    """Executa um passo. Retorna True se o passo foi considerado bem-sucedido.

    'all_routines' (opcional): lista COMPLETA de rotinas carregadas - so
    repassado pra frente pra quem precisar disparar outra rotina inteira no
    meio da propria execucao (ve 'execute_dom_boss_fight_step')."""
    step_type = step.get("type")
    if step_type == "dom_click":
        return execute_dom_click_step(page, step, stop_event, log)
    if step_type == "dom_clear_search":
        return execute_dom_clear_search_step(page, step, log)
    if step_type == "dom_ensure_checked":
        return execute_dom_ensure_checked_step(page, step, log)
    if step_type == "dom_ensure_active":
        return execute_dom_ensure_active_step(page, step, log)
    if step_type == "dom_ensure_select":
        return execute_dom_ensure_select_step(page, step, log)
    if step_type == "dom_favorite_hunt":
        return execute_dom_favorite_hunt_step(page, step, log)
    if step_type == "dom_watch_favorite":
        return execute_dom_watch_favorite_step(page, step, log)
    if step_type == "dom_tier_sort":
        return execute_dom_tier_sort_step(page, step, stop_event, log)
    if step_type == "dom_threshold_click":
        return execute_dom_threshold_click_step(page, step, log)
    if step_type == "dom_resume_hunt":
        return execute_dom_resume_hunt_step(page, step, log)
    if step_type == "dom_boss_fight":
        return execute_dom_boss_fight_step(page, step, stop_event, log, all_routines=all_routines)
    if step_type == "dom_guild_tasks":
        return execute_dom_guild_tasks_step(page, step, log)
    if step_type == "dom_hunt_bestiary":
        return execute_dom_hunt_bestiary_step(page, step, log)
    if step_type == "dom_auto_build":
        return execute_dom_auto_build_step(page, step, log)

    log(f"Tipo de passo desconhecido: '{step_type}'.")
    return False


def run_routine(page, routine, stop_event, log, all_routines=None):
    # 'gate_selector': checagem barata (sem abrir nada, sem logar nada) pra so
    # rodar a rotina de verdade quando o elemento estiver habilitado. Permite
    # um intervalo curto (fica de olho de perto) sem ficar abrindo paineis
    # (ex: Codex) e poluindo o log a cada tick enquanto ainda esta em recarga.
    gate_selector = routine.get("gate_selector")
    if gate_selector:
        try:
            is_disabled = page.eval_on_selector(gate_selector, "el => !!el.disabled")
        except Exception:
            is_disabled = True  # nao achou o elemento - nao arrisca, trata como 'ainda nao pronto'
        if is_disabled:
            return

    if routine["id"] == "vender_loot" and all_routines:
        # GARANTE 'Separar Loot' ANTES de entregar/vender - senao um item que
        # devia ser separado (guardado no backpack) podia ser vendido antes
        # de 'Separar Loot' ter a chance de tirar ele da Loot Pouch, ja que
        # as duas rotinas rodavam so pelo proprio intervalo (sem ordem
        # garantida entre si). Roda na hora, ignorando o intervalo dela -
        # 'Separar Loot' e rapida (ve execute_dom_tier_sort_step), custa
        # pouco rodar um pouco mais que o normal.
        separar = next((r for r in all_routines if r.get("id") == "separar_loot" and r.get("enabled")), None)
        if separar is not None:
            run_routine(page, separar, stop_event, log, all_routines=all_routines)

    log(f"Rotina '{routine['name']}' iniciando...")
    for step in routine["steps"]:
        if stop_event.is_set():
            return
        ok = execute_step(page, step, stop_event, log, all_routines=all_routines)
        if not ok and not step.get("repeat"):
            log(f"Rotina '{routine['name']}' interrompida (passo nao encontrado).")
            # fecha qualquer menu/painel que a rotina tenha deixado aberto no
            # meio do caminho - senao fica preso ali ate a proxima falha em
            # outra rotina disparar uma recuperacao (ou nunca).
            recover(page, log)
            return


def ensure_game_loaded(page, log):
    """Confere se a pagina do jogo carregou de verdade (nao ficou em branco).
    Ao abrir o Chrome pela primeira vez (--remote-debugging-port + URL do
    jogo direto na linha de comando), a aba inicial as vezes renderiza em
    branco - provavelmente uma corrida entre a navegacao e o protocolo de
    depuracao anexando bem no comeco. Uma aba aberta manualmente DEPOIS (ex:
    Ctrl+T) nao tem esse problema; um reload da aba em branco resolve igual.
    So mexe se realmente detectar a tela em branco, pra nao atrasar o caso
    normal (que e a maioria)."""
    try:
        if page.query_selector("#app") is not None:
            return
        log("  Pagina do jogo parece em branco - recarregando...")
        page.reload(wait_until="load", timeout=15000)
        page.wait_for_selector("#app", timeout=15000)
    except Exception as error:
        log(f"  Erro ao recarregar a pagina do jogo: {error}")


def reload_game_page(page, log):
    """Recarrega a aba do jogo do zero - mitigacao pro vazamento de memoria do
    jogo/anuncios em sessoes longas (ver PAGE_RELOAD_INTERVAL_SECONDS). So
    chamado quando nao ha nada de prioridade maior em andamento (ver o
    gatilho em run()); memorias como TRAINING_MEMORY/GUILD_TASK_MEMORY nao
    dependem de nenhuma referencia de pagina, entao sobrevivem ao reload sem
    problema - o proximo tick de cada rotina so vai reconsultar a tela do
    zero, igual faria apos qualquer reconexao.

    O reload sozinho NAO basta - confirmado ao vivo que o V8 mantem os
    listeners/documentos "fantasma" (de iframes de anuncio ja removidos)
    presos ate uma coleta de lixo de verdade rodar, e isso nao acontece so
    por navegar pra uma pagina nova. Sem forcar via 'HeapProfiler.collectGarbage',
    o processo do Chrome continuava do mesmo tamanho de antes do reload (~2.5GB);
    forcando a coleta (duas vezes, com uma pausa no meio - a primeira ainda
    deixava bastante lixo preso) o processo caiu pra ~650MB, do jeito que fica
    ao abrir o jogo do zero."""
    log("Recarregando a pagina do jogo (rotina de liberar memoria)...")
    try:
        page.reload(wait_until="load", timeout=30000)
        page.wait_for_selector("#app", timeout=15000)
    except Exception as error:
        log(f"  Erro ao recarregar a pagina do jogo: {error}")
        return
    recover(page, log)

    cdp = None
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("HeapProfiler.enable")
        cdp.send("HeapProfiler.collectGarbage")
        time.sleep(1)
        cdp.send("HeapProfiler.collectGarbage")
    except Exception as error:
        log(f"  Erro ao forcar liberacao de memoria: {error}")
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass


def recover(page, log):
    """Fecha qualquer menu/painel que possa ter ficado aberto (ex: uma rotina anterior
    parou no meio, com um menu de contexto ou popup ainda na tela). Roda ao conectar
    e depois de qualquer erro, pra garantir que o proximo ciclo comece limpo."""
    log("Fechando janelas/menus abertos, se houver...")
    for _ in range(3):
        page.keyboard.press("Escape")
        time.sleep(0.3)


def test_routine_once(routine, log=print):
    """Roda uma rotina uma unica vez, com sua propria conexao. Usado pelo botao 'Testar rotina'."""
    stop_event = threading.Event()
    try:
        if not launch_browser(log):
            return
        with sync_playwright() as playwright:
            page = connect_game_page(playwright)
            log(f"Conectado na aba: {page.url}")
            ensure_game_loaded(page, log)
            run_routine(page, routine, stop_event, log)
    except Exception as error:
        log(f"Erro: {error}")
    finally:
        log("Teste concluido.")


def run(stop_event, flags, routines, log=print, pause_event=None):
    """flags: dict {routine_id: threading.Event}. routines: lista carregada de routines.json.

    Fica rodando ate stop_event ser setado. Um erro numa rotina especifica (ex: um
    problema pontual de memoria, uma tela inesperada) nao derruba a sessao inteira
    - so pula pro proximo ciclo. Se a conexao com o navegador cair de vez, tenta
    reconectar sozinho em vez de simplesmente parar (evita ficar a noite toda sem
    vender/entregar por causa de um erro isolado).

    'pause_event' (opcional): enquanto estiver setado, o laco continua vivo (conexao
    com o navegador, memorias tipo TRAINING_MEMORY, ultimo horario de cada rotina)
    mas nao processa nenhuma rotina - um "pausar" de verdade, diferente de parar e
    iniciar de novo (que reconecta e reseta tudo do zero)."""
    if pause_event is None:
        pause_event = threading.Event()

    while not stop_event.is_set():
        try:
            if not launch_browser(log):
                stop_event.wait(RESTART_DELAY_SECONDS)
                continue

            with sync_playwright() as playwright:
                page = connect_game_page(playwright)
                log(f"Conectado na aba: {page.url}")
                ensure_game_loaded(page, log)
                recover(page, log)
                log(f"Bot iniciado (v{VERSION}).")

                last_run = {routine["id"]: 0.0 for routine in routines}
                next_hunt_check = 0.0
                next_page_reload = time.monotonic() + PAGE_RELOAD_INTERVAL_SECONDS
                while not stop_event.is_set():
                    if pause_event.is_set():
                        stop_event.wait(TICK_SECONDS)
                        continue

                    now = time.monotonic()
                    # confere ANTES de qualquer rotina - se o jogo caiu (tela
                    # de conexao perdida) ou voltou de treino offline (tela
                    # 'Bem-vindo de volta'), essas telas cobrem tudo e travam
                    # o bot inteiro ate alguem fechar na mao. Pula o resto do
                    # tick nesse caso (a pagina pode estar recarregando).
                    try:
                        if dismiss_blocking_overlays(page, log):
                            stop_event.wait(TICK_SECONDS)
                            continue
                    except Exception as error:
                        if is_connection_dead_error(error):
                            raise
                        log(f"Erro ao checar telas bloqueantes: {error}")

                    # roda sempre, nao so quando uma rotina especifica estiver
                    # ligada - "ir pra hunt padrao quando sem hunt ativa" e um
                    # comportamento de base do bot, nao algo opcional preso a
                    # uma unica rotina.
                    if now >= next_hunt_check:
                        try:
                            ensure_active_hunt(page, log)
                        except Exception as error:
                            if is_connection_dead_error(error):
                                raise
                            log(f"Erro ao garantir hunt ativa: {error}")
                        next_hunt_check = time.monotonic() + HUNT_CHECK_INTERVAL_SECONDS

                    # reload periodico pra conter o vazamento de memoria do jogo
                    # (ver PAGE_RELOAD_INTERVAL_SECONDS) - so quando nao ha nada
                    # de prioridade maior em andamento, pro reload nao cortar um
                    # chefe ou uma task de guild pela metade.
                    if now >= next_page_reload:
                        if GUILD_TASK_MEMORY.get("grinding") or TRAINING_MEMORY.get("waiting"):
                            next_page_reload = now + TICK_SECONDS  # tenta de novo no proximo tick
                        else:
                            try:
                                reload_game_page(page, log)
                            except Exception as error:
                                if is_connection_dead_error(error):
                                    raise
                                log(f"Erro ao recarregar a pagina do jogo: {error}")
                            next_page_reload = time.monotonic() + PAGE_RELOAD_INTERVAL_SECONDS

                    for routine in routines:
                        flag = flags.get(routine["id"])
                        if not flag or not flag.is_set():
                            continue
                        if routine.get("skip_when_training") and TRAINING_MEMORY.get("waiting"):
                            continue

                        trigger = routine.get("trigger", {"mode": "automatico"})
                        interval = (
                            trigger.get("seconds", TICK_SECONDS) if trigger.get("mode") == "interval" else TICK_SECONDS
                        )
                        # 'active_seconds': intervalo mais curto pra usar enquanto
                        # uma task de guild esta sendo realizada (mesmo padrao ja
                        # usado por 'skip_when_training' - checa uma memoria
                        # global direto aqui). Sem isso, uma falha transitoria na
                        # hora de trocar de hunt so seria percebida no proximo
                        # intervalo normal (ex: 30min), deixando o personagem
                        # parado a toa por muito tempo.
                        active_seconds = trigger.get("active_seconds")
                        if active_seconds is not None and routine["id"] == "tarefas_guild" and GUILD_TASK_MEMORY.get("grinding"):
                            interval = min(interval, active_seconds)
                        # FORCE_RUN_NOW: a GUI usa isso pra pedir "roda essa rotina
                        # JA', sem esperar o intervalo normal" (ex: configuracao da
                        # Build Automatica mudou enquanto o bot ja estava rodando -
                        # sem isso, a mudanca so valeria no jogo depois de ate 5min,
                        # o intervalo padrao dessa rotina).
                        forced = routine["id"] in FORCE_RUN_NOW
                        if forced or now - last_run[routine["id"]] >= interval:
                            FORCE_RUN_NOW.discard(routine["id"])
                            try:
                                run_routine(page, routine, stop_event, log, all_routines=routines)
                            except Exception as error:
                                if is_connection_dead_error(error):
                                    raise
                                log(f"Erro na rotina '{routine['name']}': {error}")
                                recover(page, log)
                            last_run[routine["id"]] = time.monotonic()
                    stop_event.wait(TICK_SECONDS)
        except Exception as error:
            log(f"Erro de conexao: {error}")
            if not stop_event.is_set():
                log(f"Tentando reconectar em {RESTART_DELAY_SECONDS}s...")
                stop_event.wait(RESTART_DELAY_SECONDS)

    log("Bot parado.")


if __name__ == "__main__":
    stop_event = threading.Event()
    routines = load_routines()
    flags = {routine["id"]: threading.Event() for routine in routines}
    for routine in routines:
        if routine.get("enabled"):
            flags[routine["id"]].set()
    try:
        run(stop_event, flags, routines)
    except KeyboardInterrupt:
        stop_event.set()
