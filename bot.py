import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

from playwright.sync_api import sync_playwright

VERSION = "4.21.2"

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
    # IdleDeck: o jogo roda num "slot" do app (Electron), nao num navegador
    # nosso - 'launcher' faz o launch_browser abrir o app com a porta de
    # depuracao (ver launch_idledeck). 9224 pra nao colidir com Chrome (9222) e
    # Opera (9223). 'profile_dir_name' nao e usado (o app guarda as proprias sessoes).
    "idledeck": {
        "label": "IdleDeck",
        "cdp_port": 9224,
        "profile_dir_name": "idledeck_profile",
        "routines_filename": "routines_idledeck.json",
        "settings_filename": "settings_idledeck.json",
        "launcher": "idledeck",
    },
    # IdleDeck (VPN): uma COPIA do IdleDeck (.exe solto) que o split tunneling
    # da VPN manda por outro IP - 2a conta/IP ao lado do IdleDeck normal. 9225.
    "idledeck_copy": {
        "label": "IdleDeck (VPN)",
        "cdp_port": 9225,
        "profile_dir_name": "idledeck_copy_profile",
        "routines_filename": "routines_idledeck_vpn.json",
        "settings_filename": "settings_idledeck_vpn.json",
        "launcher": "idledeck_copy",
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
#
# 'verified' = a ultima leitura do Helper CONFIRMOU o Stone Skin equipado (so'
# entao um chefe 'stone_skin' pode ser enfrentado). 'revert_fails' conta
# tentativas seguidas de reverter sem sucesso (desiste depois de algumas).
BOSS_AMULET_MEMORY = {"changed": {}, "verified": False, "revert_fails": 0}

# Chefe 'stone_skin' sem o amuleto confirmado NUNCA e' enfrentado (pode custar
# a morte dos chares). Pula ele e tenta de novo em poucos minutos, em vez de
# cair na espera longa de cooldown.
BOSS_AMULET_RETRY_SECONDS = 180

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

# Memoria da rotina das Missoes do Passe (mesmo papel da GUILD_TASK_MEMORY):
# 'grinding' = o bot esta caçando a hunt de uma missao do passe; 'previous_hunt'
# = de onde saiu (pra voltar quando acabarem as missoes elegiveis);
# 'go_fails' = vezes seguidas que 'Ir pra caçada' nao levou pra hunt da missao;
# 'deliver_retry_at' = nao tenta entregar de novo antes disso (apos uma falha);
# 'logged' = avisos de 'missao pulada' ja dados (nao repete a cada rodada).
PASSE_MEMORY = {"previous_hunt": None, "grinding": False, "grinding_since": None, "go_fails": 0,
                "deliver_retry_at": 0.0, "logged": set()}
PASSE_DELIVER_RETRY_SECONDS = 30
PASSE_MAX_GO_FAILS = 3

# Nivel recomendado de cada hunt ('.stage-lvl' da lista de Hunts), lido uma vez
# e reaproveitado - serve pra regra "so faz a task/missao se o nosso nivel for
# pelo menos o da hunt" (Tasks da Guild e Passe).
HUNT_LEVELS_MEMORY = {"loaded_at": 0.0, "levels": {}}
HUNT_LEVELS_MAX_AGE_SECONDS = 60 * 60

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

# Campanha de Codex (ver execute_dom_codex_campaign_step): 'target_hunt' e a
# hunt do primeiro item da fila que ainda nao terminou - quando preenchida, ela
# vira a hunt "base" no lugar da hunt padrao (ver return_to_default_hunt).
# 'done' = entradas ja vistas como concluidas (nunca voltam atras, entao nao
# precisam ser relidas); 'skip_until' (monotonic) = entradas puladas por um
# tempo (hunt inalcancavel, desbloqueio que falhou); 'travel_fails' = ciclos
# seguidos sem conseguir chegar na hunt alvo; 'training_sent' evita mandar pro
# treino online de novo a cada ciclo depois que a fila acabou.
CAMPAIGN_MEMORY = {
    "target_hunt": None, "target_entry": None, "done": set(),
    "skip_until": {}, "travel_fails": 0, "training_sent": False, "missing_logged": set(),
    # leitura completa so' quando algo muda: 'baseline_done' = contador de
    # concluidos do Codex na ultima leitura completa; 'last_check'/'last_full'
    # (monotonic) = ultima olhada no contador / ultima leitura completa.
    "baseline_done": None, "last_check": 0.0, "last_full": 0.0,
}


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


# ---------------------------------------------------------------------------
# Auto-update: consulta o GitHub Releases do proprio repo (API publica, sem
# precisar de token) pela versao mais recente publicada, baixa o build certo
# pro sistema operacional de quem esta rodando e troca o proprio executavel/
# app sozinho. So' faz sentido rodando congelado (.exe/.app) - em modo dev
# ('python gui.py') so' teria o codigo-fonte pra atualizar, nao um binario.
UPDATE_REPO = "matheusromano6/baiakidle"
UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
# nomes FIXOS dos assets em cada release (nao mudam de versao pra versao) -
# e' o que permite achar o build certo sem precisar saber o numero da
# versao de antemao.
UPDATE_ASSET_NAMES = {
    "win32": "BaiakIdleBot-windows.zip",
    "darwin": "BaiakIdleBot-macos-arm64.zip",
}


def _parse_version(text):
    """'4.12.7' ou 'v4.12.7' -> (4, 12, 7), pra comparar numericamente (nao
    como texto - senao '4.9.10' > '4.10.0' incorretamente, string vem antes
    por causa do '9' > '1' no 2o digito)."""
    text = (text or "").strip().lstrip("vV")
    parts = []
    for piece in text.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


def check_for_update(log=print):
    """Consulta a release mais recente no GitHub. Retorna
    {'version', 'asset_url', 'asset_name'} se houver uma versao MAIOR que a
    atual disponivel pro sistema operacional de quem esta chamando, ou None
    (ja esta na ultima, ainda nao tem release nenhuma, SO nao tem build pra
    esse SO, ou erro de rede - nunca trava o bot por causa disso, so' avisa
    no log e segue sem atualizar)."""
    try:
        request = urllib.request.Request(
            UPDATE_API_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "BaiakIdleBot"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.load(response)
    except Exception as error:
        log(f"  Nao consegui checar atualizacoes: {error}")
        return None

    latest_tag = data.get("tag_name") or ""
    latest_version = _parse_version(latest_tag)
    if not latest_version or latest_version <= _parse_version(VERSION):
        return None

    asset_name = UPDATE_ASSET_NAMES.get(sys.platform)
    if asset_name is None:
        return None  # SO sem build (ex: Linux) - nada a fazer

    asset_url = None
    for asset in data.get("assets", []) or []:
        if asset.get("name") == asset_name:
            asset_url = asset.get("browser_download_url")
            break
    if not asset_url:
        return None  # release existe mas ainda nao subiu o build desse SO

    return {
        "version": latest_tag.lstrip("vV"),
        "asset_url": asset_url,
        "asset_name": asset_name,
    }


def _download_update_zip(asset_url, log, progress=None):
    """Baixa o zip da atualizacao pra uma pasta temporaria e confere que e'
    um zip valido antes de mexer em qualquer coisa. Retorna o caminho do
    zip baixado, ou None se falhar (nada foi trocado ainda nesse ponto).
    'progress' (opcional) recebe a fracao 0..1 baixada - usado pela tela de
    carregamento da GUI."""
    tmp_dir = tempfile.mkdtemp(prefix="baiakidle_update_")
    zip_path = os.path.join(tmp_dir, "update.zip")

    def report(blocks, block_size, total_size):
        if progress and total_size > 0:
            progress(min(1.0, blocks * block_size / total_size))

    try:
        urllib.request.urlretrieve(asset_url, zip_path, report)
    except Exception as error:
        log(f"  Erro ao baixar a atualizacao: {error}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    if not zipfile.is_zipfile(zip_path):
        log("  O arquivo baixado nao e' um zip valido - atualizacao cancelada.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    return zip_path


def apply_update_windows(asset_url, log, progress=None):
    """Baixa e aplica a atualizacao no Windows. Nao da pra sobrescrever o
    .exe rodando (fica travado pelo proprio processo) - gera um .bat que
    espera ESSE processo (pelo PID) terminar, so' ENTAO troca o arquivo
    (guardando o antigo como '.bak', apagado so' depois da troca confirmar)
    e reabre o bot. Quem chama precisa fechar o bot logo em seguida pra
    liberar o .exe pro .bat trocar."""
    zip_path = _download_update_zip(asset_url, log, progress)
    if zip_path is None:
        return False
    tmp_dir = os.path.dirname(zip_path)

    extract_dir = os.path.join(tmp_dir, "extracted")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    new_exe = None
    for name in os.listdir(extract_dir):
        if name.lower().endswith(".exe"):
            new_exe = os.path.join(extract_dir, name)
            break
    if new_exe is None:
        log("  Nao achei o .exe dentro do zip baixado - atualizacao cancelada.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    # atualiza tambem a copia solta de 'market/' do lado do exe, se existir
    # (a embutida no exe novo ja cobre o essencial - isso e' so' consistencia,
    # nao trava a atualizacao se der errado).
    new_market = os.path.join(extract_dir, "market")
    current_market = os.path.join(resource_dir(), "market")

    current_exe = sys.executable
    pid = os.getpid()
    bat_path = os.path.join(tmp_dir, "apply_update.bat")
    # merge (nao apaga/substitui a pasta inteira): 'new_market' so' tem
    # codigo (sem banco/config/chrome_profile pessoal, ve MARKET_EXCLUDE_NAMES
    # em make_share_zip.py), entao um xcopy por cima preserva os dados reais
    # do usuario que ja estao em 'current_market'.
    update_log = os.path.join(tmp_dir, "update_log.txt")
    market_swap = ""
    if os.path.isdir(new_market):
        market_swap = (
            f'if not exist "{current_market}" mkdir "{current_market}"\n'
            f'xcopy /Y /E /I /Q "{new_market}\\*" "{current_market}\\" >> "{update_log}" 2>&1\n'
        )
    # log proprio (diagnostico): se a troca falhar no meio do caminho por
    # algum motivo, esse arquivo mostra ate' onde ela chegou - sem ele, uma
    # falha aqui e' completamente silenciosa (ve' historico: ja aconteceu
    # em teste isolado e numa instalacao real).
    #
    # o "tasklist | find" do loop de espera NAO e' confiavel quando o
    # processo que roda o .bat e' criado totalmente sem console
    # (subprocess.Popen com DETACHED_PROCESS) - confirmado em teste: o
    # check da's vezes acerta, as vezes retorna errado (falso "ja fechou"
    # ou trava pra sempre), aparentemente por causa de como tasklist/find
    # se comportam sem um console de verdade por tras. Rodando o mesmo
    # .bat via uma tarefa agendada (schtasks /create + /run, tarefa unica
    # que se autodeleta no final) da' pra ele um contexto de processo
    # normal - testado e confirmado funcionando de forma consistente.
    task_name = f"BaiakIdleBotUpdate_{pid}"
    bat_content = (
        "@echo off\n"
        f'echo %date% %time% iniciando, esperando PID {pid} fechar >> "{update_log}"\n'
        ":waitloop\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\n'
        "if not errorlevel 1 (\n"
        "    timeout /t 1 /nobreak >NUL\n"
        "    goto waitloop\n"
        ")\n"
        f'echo %date% %time% PID fechou, trocando exe >> "{update_log}"\n'
        f'move /Y "{current_exe}" "{current_exe}.bak" >> "{update_log}" 2>&1\n'
        f'move /Y "{new_exe}" "{current_exe}" >> "{update_log}" 2>&1\n'
        f'echo %date% %time% exe trocado, atualizando market >> "{update_log}"\n'
        f"{market_swap}"
        f'echo %date% %time% reabrindo o bot >> "{update_log}"\n'
        f'start "" "{current_exe}"\n'
        f'del "{current_exe}.bak"\n'
        f'schtasks /delete /tn "{task_name}" /f >NUL 2>&1\n'
        f'echo %date% %time% concluido >> "{update_log}"\n'
    )
    with open(bat_path, "w", encoding="utf-8") as file:
        file.write(bat_content)

    try:
        subprocess.run(
            ["schtasks", "/create", "/tn", task_name, "/tr", bat_path,
             "/sc", "once", "/st", "00:00", "/sd", "01/01/2050", "/f"],
            creationflags=subprocess.CREATE_NO_WINDOW, check=True, capture_output=True,
        )
        subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            creationflags=subprocess.CREATE_NO_WINDOW, check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError) as error:
        log(f"  Erro ao agendar a troca: {error}")
        return False
    log("  Atualizacao baixada - o bot vai fechar e reabrir sozinho na versao nova.")
    log(f"  (se nao reabrir sozinho, o log da troca fica em: {update_log})")
    return True


def apply_update_mac(asset_url, log, progress=None):
    """Baixa e aplica a atualizacao no Mac. Mesma ideia que a versao
    Windows, mas trocando um pacote '.app' inteiro (uma pasta) em vez de um
    unico arquivo - usa um script shell em vez de .bat.

    NAO TESTADO num Mac de verdade ainda - se falhar em algum passo, o
    '.app' antigo continua intacto (so' e' apagado depois da troca
    confirmar), e o botao de atualizar continua disponivel pra tentar nas
    proxima vez ou baixar manualmente pela pagina de releases."""
    zip_path = _download_update_zip(asset_url, log, progress)
    if zip_path is None:
        return False
    tmp_dir = os.path.dirname(zip_path)

    extract_dir = os.path.join(tmp_dir, "extracted")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    new_app = None
    for name in os.listdir(extract_dir):
        if name.endswith(".app"):
            new_app = os.path.join(extract_dir, name)
            break
    if new_app is None:
        log("  Nao achei o .app dentro do zip baixado - atualizacao cancelada.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    # sys.executable de um app empacotado fica em Algo.app/Contents/MacOS/Algo
    exe_dir = os.path.dirname(sys.executable)
    current_app = os.path.dirname(os.path.dirname(exe_dir))  # MacOS -> Contents -> Algo.app
    pid = os.getpid()

    sh_path = os.path.join(tmp_dir, "apply_update.sh")
    sh_content = (
        "#!/bin/sh\n"
        f"while kill -0 {pid} 2>/dev/null; do\n"
        "    sleep 1\n"
        "done\n"
        f'mv "{current_app}" "{current_app}.bak"\n'
        f'mv "{new_app}" "{current_app}"\n'
        f'xattr -cr "{current_app}" 2>/dev/null\n'
        f'rm -rf "{current_app}.bak"\n'
        f'open "{current_app}"\n'
    )
    with open(sh_path, "w", encoding="utf-8") as file:
        file.write(sh_content)
    os.chmod(sh_path, 0o755)

    subprocess.Popen(["/bin/sh", sh_path], start_new_session=True)
    log("  Atualizacao baixada - o bot vai fechar e reabrir sozinho na versao nova.")
    return True


def apply_update(asset_url, log, progress=None):
    """Escolhe a funcao certa pro sistema operacional atual. Quem chama deve
    fechar o bot logo depois de um retorno True (o script auxiliar so'
    continua a troca quando esse processo terminar de verdade)."""
    if sys.platform == "win32":
        return apply_update_windows(asset_url, log, progress)
    if sys.platform == "darwin":
        return apply_update_mac(asset_url, log, progress)
    return False


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


# --- IdleDeck (perfil "idledeck") -------------------------------------------
# O IdleDeck (app Electron da Microsoft Store que roda varias contas de jogos
# idle em "slots") nao expoe porta de depuracao sozinho. CONFIRMADO ao vivo: aberto
# pela ativacao do pacote com '--remote-debugging-port=N' ele aceita o
# argumento e o jogo de cada slot aparece como uma PAGINA normal (nao iframe)
# em 'baiakidle.com/jogar/' - o resto do bot funciona igual. Matar os
# processos a forca ja deixou um subprocesso 'fantasma' que impedia reabrir o
# app ("aplicativo sendo encerrado", 0x8000001A) - por isso o bot so pede pra
# fechar pela janela (CloseMainWindow) e espera; nunca usa kill.
IDLEDECK_CLOSE_WAIT_SECONDS = 120   # tempo pro usuario confirmar 'Sair' no dialogo do app
IDLEDECK_LAUNCH_TIMEOUT_SECONDS = 40

_IDLEDECK_PS_COMMON = """
$ErrorActionPreference = 'Stop'
function Get-IdleDeckProcs { param($like) @(Get-Process IdleDeck -ErrorAction SilentlyContinue | Where-Object { -not $_.HasExited -and ($like -eq $null -or $_.Path -like $like) }) }
"""

_IDLEDECK_PS_ACTIVATE = """
Add-Type @"
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("2e941141-7f97-4756-ba1d-9decde894a3d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IApplicationActivationManager {
    int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, [MarshalAs(UnmanagedType.LPWStr)] string arguments, int options, out uint processId);
    int ActivateForFile([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, IntPtr itemArray, [MarshalAs(UnmanagedType.LPWStr)] string verb, out uint processId);
    int ActivateForProtocol([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, IntPtr itemArray, out uint processId);
}
[ComImport, Guid("45BA127D-10A8-46EA-8AB7-56EA9078943C"), ClassInterface(ClassInterfaceType.None)]
public class ApplicationActivationManager : IApplicationActivationManager {
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.InternalCall, MethodCodeType = System.Runtime.CompilerServices.MethodCodeType.Runtime)]
    public extern int ActivateApplication([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, [MarshalAs(UnmanagedType.LPWStr)] string arguments, int options, out uint processId);
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.InternalCall, MethodCodeType = System.Runtime.CompilerServices.MethodCodeType.Runtime)]
    public extern int ActivateForFile([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, IntPtr itemArray, [MarshalAs(UnmanagedType.LPWStr)] string verb, out uint processId);
    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.InternalCall, MethodCodeType = System.Runtime.CompilerServices.MethodCodeType.Runtime)]
    public extern int ActivateForProtocol([MarshalAs(UnmanagedType.LPWStr)] string appUserModelId, IntPtr itemArray, out uint processId);
}
"@
$pkg = Get-AppxPackage | Where-Object { $_.Name -like '*IdleDeck*' } | Select-Object -First 1
if (-not $pkg) { Write-Output 'ERR IdleDeck nao esta instalado (pacote da Microsoft Store nao encontrado)'; exit 0 }
$appId = (Get-AppxPackageManifest $pkg).Package.Applications.Application.Id
$mgr = [IApplicationActivationManager](New-Object ApplicationActivationManager)
[uint32]$procId = 0
try {
    $null = $mgr.ActivateApplication("$($pkg.PackageFamilyName)!$appId", "--remote-debugging-port=__PORT__", 0, [ref]$procId)
    Write-Output "OK $procId"
} catch {
    Write-Output ("ERR " + $_.Exception.Message)
}
"""


def _run_powershell(script, timeout=60):
    """Roda um script PowerShell (codificado, sem problema de aspas) sem abrir
    janela de console. Retorna a saida padrao (texto)."""
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (result.stdout or "").strip()


def _idledeck_ps_filter(path_like):
    """Trecho PowerShell que lista os processos do IdleDeck, opcionalmente so'
    os de um caminho (a versao da Store roda dentro de WindowsApps, a copia do
    perfil VPN em outra pasta - sao instancias DIFERENTES e uma nunca deve
    fechar a outra)."""
    if not path_like:
        return "Get-IdleDeckProcs"
    return "Get-IdleDeckProcs -like '" + path_like.replace("'", "''") + "'"


# processos da versao instalada pela Microsoft Store
IDLEDECK_PACKAGE_LIKE = "*" + os.sep + "WindowsApps" + os.sep + "*"
# onde fica a COPIA do IdleDeck usada pelo perfil 'idledeck_copy' (settings:
# 'idledeck_exe' muda). Uma copia da pasta 'app' do IdleDeck, com o executavel
# adicionado ao split tunneling da VPN, da' a ela um IP diferente do original.
IDLEDECK_COPY_DEFAULT_EXE = "C:/IdleDeck-VPN/app/IdleDeck.exe"


def idledeck_running(path_like=None):
    """True se ha processo do IdleDeck vivo (com ou sem porta de depuracao)."""
    out = _run_powershell(_IDLEDECK_PS_COMMON + "(" + _idledeck_ps_filter(path_like) + ").Count", timeout=30)
    return out.strip().isdigit() and int(out.strip()) > 0


def idledeck_request_close(path_like=None):
    """Pede pra janela principal fechar (igual clicar no X). O app pode abrir
    um dialogo nativo perguntando se quer sair - quem confirma e' o usuario."""
    _run_powershell(
        _IDLEDECK_PS_COMMON + _idledeck_ps_filter(path_like)
        + " | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object { $null = $_.CloseMainWindow() }",
        timeout=30,
    )


def _idledeck_close_and_wait(path_like, log):
    """Se ja ha um IdleDeck (do caminho dado) aberto SEM a porta de depuracao,
    pede pra fechar pela janela e espera o usuario confirmar 'Sair'. Retorna
    True se nao ha mais nenhum aberto."""
    if not idledeck_running(path_like):
        return True
    log(
        "O IdleDeck esta aberto SEM a depuracao remota - pedindo pra fechar. "
        "Se aparecer uma janela perguntando, escolha SAIR (nao so minimizar); "
        "o bot reabre sozinho em seguida (suas contas/logins ficam salvos)."
    )
    idledeck_request_close(path_like)
    deadline = time.monotonic() + IDLEDECK_CLOSE_WAIT_SECONDS
    last_reminder = time.monotonic()
    while time.monotonic() < deadline and idledeck_running(path_like):
        time.sleep(2)
        if time.monotonic() - last_reminder >= 30:
            last_reminder = time.monotonic()
            log("  Ainda aguardando o IdleDeck fechar - feche pela janela dele (Sair).")
    if idledeck_running(path_like):
        log("O IdleDeck nao fechou a tempo. Feche pela janela (Sair) e clique em 'Abrir Jogo' de novo.")
        return False
    time.sleep(2)  # deixa o Windows liberar o pacote/arquivos antes de reabrir
    return True


def _wait_debug_port(log, name):
    deadline = time.monotonic() + IDLEDECK_LAUNCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_debug_port_open():
            log(f"{name} pronto.")
            return True
        time.sleep(0.5)
    log(f"O {name} abriu mas a porta de depuracao nao respondeu.")
    return False


def launch_idledeck(log=print):
    """Equivalente de 'launch_browser' pro perfil IdleDeck (versao da Store):
    deixa o app aberto com a porta de depuracao (cdp_port do perfil)
    respondendo. Retorna True se a porta responde no final."""
    if is_debug_port_open():
        log("IdleDeck ja esta com a depuracao remota ativa.")
        return True
    if platform.system() != "Windows":
        log("O perfil IdleDeck so funciona no Windows por enquanto (app da Microsoft Store).")
        return False

    try:
        if not _idledeck_close_and_wait(IDLEDECK_PACKAGE_LIKE, log):
            return False

        log("Abrindo o IdleDeck com a depuracao remota...")
        script = _IDLEDECK_PS_ACTIVATE.replace("__PORT__", str(cdp_port()))
        deadline = time.monotonic() + IDLEDECK_LAUNCH_TIMEOUT_SECONDS
        last_error = ""
        while time.monotonic() < deadline:
            out = _run_powershell(script, timeout=60)
            if out.startswith("OK"):
                break
            last_error = out
            if "nao esta instalado" in out:
                break
            time.sleep(3)  # logo apos fechar o Windows ainda pode recusar ("aplicativo sendo encerrado")
        else:
            out = "ERR " + last_error
        if not out.startswith("OK"):
            log(f"Nao consegui abrir o IdleDeck: {last_error or out}")
            return False
        return _wait_debug_port(log, "IdleDeck")
    except Exception as error:
        log(f"Erro ao abrir o IdleDeck: {error}")
        return False


def launch_idledeck_copy(log=print):
    """Perfil 'idledeck_copy' (IdleDeck VPN): abre a COPIA do IdleDeck (um
    .exe solto, sem o pacote da Store) com pasta de dados propria
    ('--idledeck-data', o bloqueio de instancia unica do app e' por pasta) e a
    porta de depuracao do perfil. CONFIRMADO ao vivo: com o .exe da copia no
    split tunneling por aplicativo do Kaspersky VPN, ele sai por outro IP que o
    IdleDeck original."""
    name = "IdleDeck (VPN)"
    if is_debug_port_open():
        log(f"{name} ja esta com a depuracao remota ativa.")
        return True
    if platform.system() != "Windows":
        log("O perfil IdleDeck (VPN) so funciona no Windows por enquanto.")
        return False

    exe = os.path.normpath(str(load_settings().get("idledeck_exe") or IDLEDECK_COPY_DEFAULT_EXE))
    if not os.path.exists(exe):
        log(
            f"Copia do IdleDeck nao encontrada em '{exe}'. Copie a pasta 'app' do IdleDeck pra la "
            "(e adicione o executavel no split tunneling da sua VPN) ou ajuste 'idledeck_exe' nas configuracoes."
        )
        return False
    exe_dir = os.path.dirname(exe)
    data_folder = os.path.join(os.path.dirname(exe_dir), "data")
    try:
        if not _idledeck_close_and_wait(exe_dir + os.sep + "*", log):
            return False
        log(f"Abrindo o {name} com a depuracao remota...")
        subprocess.Popen(
            [exe, f"--idledeck-data={data_folder}", f"--remote-debugging-port={cdp_port()}"],
            cwd=exe_dir,
        )
        return _wait_debug_port(log, name)
    except Exception as error:
        log(f"Erro ao abrir o {name}: {error}")
        return False


def launch_browser(log=print):
    """Abre o navegador do perfil ativo (CURRENT_PROFILE: Chrome ou Opera) ja
    apontado pro jogo, com depuracao remota ligada.

    Usa um perfil proprio (profile_dir()) porque o navegador recusa abrir a
    porta de depuracao no perfil padrao por seguranca. Retorna True se, ao
    final, a porta de depuracao esta respondendo (ja estivesse aberta ou nao).
    """
    label = BROWSER_PROFILES[CURRENT_PROFILE]["label"]
    launcher = BROWSER_PROFILES[CURRENT_PROFILE].get("launcher")
    if launcher == "idledeck":
        return launch_idledeck(log)
    if launcher == "idledeck_copy":
        return launch_idledeck_copy(log)
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

# Mesmas 3 dificuldades pras Missoes do Passe ('card_class' aqui e a classe da
# etiqueta de dificuldade da missao dentro do modal do Passe).
PASSE_DIFFICULTIES = [
    {"key": "facil", "label": "Fácil", "card_class": "bp-band-easy", "enabled": True},
    {"key": "media", "label": "Média", "card_class": "bp-band-medium", "enabled": True},
    {"key": "dificil", "label": "Difícil", "card_class": "bp-band-hard", "enabled": True},
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
        # compra diaria de pocoes de boost (estoque pras sequencias de chefes):
        # so' age nas pocoes que o usuario marcou 'comprar 1 por dia' na tela
        # de Pocoes - sem nada marcado nao toca no jogo.
        "id": "pocoes_estoque",
        "name": "Pocoes: compra diaria",
        "enabled": True,
        "skip_when_training": True,
        "trigger": {"mode": "interval", "seconds": 600},
        "steps": [{"type": "dom_potion_stock"}],
    },
    {
        # Missoes do Passe de Temporada: escolhe (so a versao Normal) a missao
        # elegivel pelo nivel + dificuldade marcada e vai pra hunt dela. A ENTREGA
        # nao depende desta rotina - e automatica sempre que o contador do Passe
        # na tela bate a meta (ve 'deliver_pass_if_ready'). Fica ANTES da guild
        # de proposito: a ordem e chefes > passe > tasks da guild.
        "id": "missoes_passe",
        "name": "Missões do Passe",
        "enabled": False,
        "skip_when_training": True,
        "trigger": {"mode": "interval", "seconds": 1800, "active_seconds": 60},
        "steps": [
            {
                "type": "dom_battlepass",
                "difficulties": [dict(d) for d in PASSE_DIFFICULTIES],
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
        "id": "campanha_codex",
        "name": "Campanha de Codex",
        "enabled": False,
        "skip_when_training": True,
        "trigger": {"mode": "interval", "seconds": 60},
        "steps": [{"type": "dom_codex_campaign", "queue": []}],
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


def _ensure_campaign_routine(routines):
    """Um 'routines.json' salvo antes da Campanha de Codex existir nao tem a
    rotina dela - adiciona (desligada, fila vazia) logo apos a do Bestiary
    pra todo mundo ganhar o botao 'Campanha' sem editar arquivo na mao.
    Retorna True se adicionou."""
    if any(r.get("id") == "campanha_codex" for r in routines):
        return False
    template = next(r for r in DEFAULT_ROUTINES if r["id"] == "campanha_codex")
    position = next((i + 1 for i, r in enumerate(routines) if r.get("id") == "bestiary_hunt"), len(routines))
    routines.insert(position, json.loads(json.dumps(template)))
    return True


def _ensure_potion_routine(routines):
    """Um 'routines.json' salvo antes das Pocoes existirem ganha a rotina de
    compra diaria (ligada, mas inerte ate' o usuario marcar algo) logo apos
    'Enfrentar Chefes'. Retorna True se adicionou."""
    if any(r.get("id") == "pocoes_estoque" for r in routines):
        return False
    template = next(r for r in DEFAULT_ROUTINES if r["id"] == "pocoes_estoque")
    position = next((i + 1 for i, r in enumerate(routines) if r.get("id") == "enfrentar_chefes"), len(routines))
    routines.insert(position, json.loads(json.dumps(template)))
    return True


def _ensure_passe_routine(routines):
    """Um 'routines.json' salvo antes do Passe existir ganha a rotina das
    Missoes do Passe (desligada) logo ANTES das Tarefas da Guild (ordem:
    chefes > passe > guild). Retorna True se adicionou."""
    if any(r.get("id") == "missoes_passe" for r in routines):
        return False
    template = next(r for r in DEFAULT_ROUTINES if r["id"] == "missoes_passe")
    position = next((i for i, r in enumerate(routines) if r.get("id") == "tarefas_guild"), len(routines))
    routines.insert(position, json.loads(json.dumps(template)))
    return True


def load_routines():
    path = routines_path()
    if not os.path.exists(path):
        save_routines(DEFAULT_ROUTINES)
    with open(path, "r", encoding="utf-8") as file:
        routines = json.load(file)
    changed = _ensure_mandatory_vender_loot_steps(routines)
    changed = _ensure_campaign_routine(routines) or changed
    changed = _ensure_potion_routine(routines) or changed
    changed = _ensure_passe_routine(routines) or changed
    if changed:
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


def state_path():
    return settings_path()[: -len(".json")] + ".state.json"


def load_state():
    """Dados que o PROPRIO BOT grava enquanto roda (ex: dia da ultima compra
    de cada pocao, tempos das sequencias de chefes) - arquivo separado do
    settings.json porque a GUI regrava o settings.json inteiro a partir da
    copia que ela tem em memoria, o que apagaria o que o bot gravou ali."""
    try:
        with open(state_path(), "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}


def save_state(state):
    with open(state_path(), "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def connect_game_page(playwright):
    """Conecta no Chrome (porta de depuracao) e acha a aba do jogo pela URL.

    'launch_browser' so espera a porta de depuracao RESPONDER, nao a
    navegacao pra URL do jogo terminar - num perfil novo/primeira abertura
    isso pode demorar alguns segundos a mais (o Chrome ainda esta de fato
    carregando a aba inicial). Sem retry aqui, essa corrida fazia a conexao
    falhar ('aba do jogo nao encontrada') mesmo com o Chrome funcionando
    perfeitamente - so precisava de mais um instante."""
    browser = playwright.chromium.connect_over_cdp(cdp_url())

    # IdleDeck: cada slot/conta e' uma pagina do jogo na mesma conexao. Com
    # varias contas abertas, 'idledeck_account' (settings do perfil) e' um
    # trecho do titulo da aba (o titulo comeca com o nome do personagem, ex:
    # "Cibele Druid") que escolhe QUAL conta este bot controla; vazio = a 1a.
    needle = ""
    if BROWSER_PROFILES[CURRENT_PROFILE].get("launcher") in ("idledeck", "idledeck_copy"):
        needle = str(load_settings().get("idledeck_account") or "").strip().lower()

    deadline = time.monotonic() + CONNECT_GAME_PAGE_TIMEOUT_SECONDS
    while True:
        for context in browser.contexts:
            for page in context.pages:
                if GAME_URL_PATTERN not in page.url:
                    continue
                if needle:
                    try:
                        if needle not in (page.title() or "").lower():
                            continue
                    except Exception:
                        continue
                return page
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    label = BROWSER_PROFILES[CURRENT_PROFILE]["label"]
    if needle:
        raise RuntimeError(
            f"Aba do jogo nao encontrada no {label} (URL com '{GAME_URL_PATTERN}' e titulo com '{needle}'). "
            "Confira o nome da conta em 'idledeck_account' nas configuracoes."
        )
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


# ---------- Campanha de Codex ----------
# Atributos do filtro nativo do Codex ('.cx-attr') - valor interno do jogo e
# o texto que aparece na recompensa de cada entrada ('.cx-entry-bonus').
CODEX_ATTRIBUTES = [
    ("atkPct", "Ataque"), ("armorFlat", "Armadura"), ("defFlat", "Defesa"), ("hpPct", "Vida"),
    ("manaPct", "Mana"), ("critChance", "Chance de crítico"), ("critDmg", "Dano crítico"),
    ("absorbPct", "Resistência elemental"), ("elementDmgPct", "Dano elemental"),
    ("lifeLeech", "Roubo de vida"), ("manaLeech", "Roubo de mana"), ("spellDmgPct", "Dano de magia"),
    ("spellHealPct", "Cura de magia"), ("moveSpeed", "Velocidade de movimento"),
    ("onslaught", "Onslaught (fatal)"),
]
CODEX_ATTR_BY_LABEL = {label.lower(): value for value, label in CODEX_ATTRIBUTES}
CODEX_FILTER_LABELS = ("Entregáveis", "Esconder concluídos", "Esconder bloqueadas")
ROMAN_LEVELS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
CAMPAIGN_SKIP_SECONDS = 15 * 60
# Com o alvo definido, a campanha NAO fica mexendo no Codex a cada ciclo: de
# CAMPAIGN_CHECK_SECONDS em CAMPAIGN_CHECK_SECONDS so' abre o Codex pra ler o
# contador de concluidos (sem tocar em filtro nem campo); a leitura completa
# (que desmarca filtros) so' roda se esse contador mudou, ou como rede de
# seguranca a cada CAMPAIGN_FULL_RECHECK_SECONDS.
CAMPAIGN_CHECK_SECONDS = 5 * 60
CAMPAIGN_FULL_RECHECK_SECONDS = 20 * 60
CAMPAIGN_MAX_TRAVEL_FAILS = 3

CODEX_ENTRIES_JS = """() => Array.from(document.querySelectorAll('.cx-list .cx-entry')).map(e => {
    const nameEl = e.querySelector('.cx-entry-name');
    const bar = e.querySelector('.cx-bar-fill');
    const btn = e.querySelector('.cx-entry-side button.cx-give');
    const m = ((bar && bar.getAttribute('style')) || '').match(/width:\\s*([\\d.]+)%/);
    const btnText = btn ? btn.textContent.trim() : '';
    const unlock = /^Desbloquear/i.test(btnText);
    return {
        name: ((nameEl && (nameEl.getAttribute('title') || nameEl.textContent)) || '').trim(),
        slug: (e.querySelector('.cx-entry-num') && e.querySelector('.cx-entry-num').getAttribute('title')) || '',
        bonus_text: ((e.querySelector('.cx-entry-bonus') || {}).textContent || '').trim(),
        progress: m ? parseFloat(m[1]) : 0,
        done: e.classList.contains('done'),
        locked: unlock || !!(nameEl && nameEl.querySelector('svg')),
        unlock_text: unlock ? btnText : '',
    };
})"""


def format_gold(value):
    return f"{int(value):,}".replace(",", ".")


def parse_codex_bonus(text):
    """'Dano crítico +0,370% · Chance de crítico +0,071%' ->
    {'critDmg': 0.37, 'critChance': 0.071}. Partes com atributo desconhecido
    sao ignoradas."""
    bonuses = {}
    for part in re.split(r"\s*[·•]\s*", text or ""):
        match = re.match(r"^(.*?)\s*\+\s*([\d.,]+)\s*%?\s*$", part.strip())
        if not match:
            continue
        attr = CODEX_ATTR_BY_LABEL.get(match.group(1).strip().lower())
        if not attr:
            continue
        try:
            bonuses[attr] = float(match.group(2).replace(".", "").replace(",", "."))
        except ValueError:
            continue
    return bonuses


def split_codex_entry_name(raw_name):
    """'Domínio: Wereliones II' -> ('Wereliones', 2); sem numeral = nivel 1."""
    name = (raw_name or "").strip()
    if ":" in name:
        name = name.split(":", 1)[1].strip()
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].lower() in ROMAN_LEVELS:
        return parts[0].strip(), ROMAN_LEVELS[parts[1].lower()]
    return name, 1


def match_hunt_for_codex_base(base, hunt_names):
    """Acha a hunt do jogo que corresponde a base de uma entrada do Codex
    ('Wereliones II' -> base 'Wereliones'). Nome igual (sem diferenciar
    maiusculas) ou, na falta, o unico nome que contem/esta contido na base.
    Retorna None se nao achar (ou se ficar ambiguo)."""
    wanted = base.strip().lower()
    names = [n for n in hunt_names if n]
    for name in names:
        if name.strip().lower() == wanted:
            return name
    candidates = [n for n in names if wanted in n.lower() or n.strip().lower() in wanted]
    return candidates[0] if len(candidates) == 1 else None


def build_codex_entry(raw):
    base, level = split_codex_entry_name(raw["name"])
    cost = 0
    if raw.get("unlock_text") and "·" in raw["unlock_text"]:
        digits = re.sub(r"\D", "", raw["unlock_text"].split("·", 1)[1])
        cost = int(digits) if digits else 0
    return {
        "name": raw["name"],
        "slug": raw.get("slug", ""),
        "base": base,
        "level": level,
        "bonus_text": raw.get("bonus_text", ""),
        "bonuses": parse_codex_bonus(raw.get("bonus_text", "")),
        "progress": raw.get("progress", 0),
        "status": "done" if raw.get("done") else ("locked" if raw.get("locked") else "open"),
        "unlock_cost": cost,
    }


def open_codex(page):
    """Abre o Codex (Progressao > Codex, igual a rotina de entrega). Retorna
    True se abriu agora, False se ja estava aberto."""
    if page.is_visible("#picker-modal"):
        # a janela de Hunts/Chefes aberta por cima intercepta os cliques - sem
        # fechar, nada do Codex abaixo responde (visto ao vivo).
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    if page.is_visible(".codex-side"):
        return False
    if page.is_visible("#auction-modal"):
        # CONFIRMADO nos logs: o Mercado/leilao do jogo aberto por cima
        # intercepta o clique em 'Progressao' (timeout de 3s e a leitura do
        # Codex pro market falhava, deixando o mapa velho por horas). Falha
        # na hora e com motivo claro; 'dismiss_blocking_overlays' fecha esse
        # modal depois de AUCTION_MODAL_GRACE_SECONDS e quem chamou tenta de novo.
        raise RuntimeError("o Mercado/leilao do jogo esta aberto")
    page.click("#tab-progressao", timeout=3000)
    page.click("#tab-codex", timeout=3000)
    page.wait_for_selector(".codex-side", timeout=4000)
    page.wait_for_timeout(500)
    return True


def read_codex_completed_count(page):
    """Le o contador 'N / 690 concluidos' do Codex (soma de todas as
    categorias) - abre e fecha o Codex como a rotina de entrega ja faz, SEM
    mexer em filtro nem em campo. Sobe quando qualquer entrada completa
    (inclusive por Auto Collect, que entrega sem o painel aberto). Retorna
    None se nao conseguir ler."""
    opened_here = open_codex(page)
    try:
        text = page.inner_text(".cx-title-score", timeout=2000) or ""
    except Exception:
        return None
    finally:
        if opened_here:
            try:
                page.click("#codex-modal-close", timeout=2000)
            except Exception:
                pass
    match = re.match(r"\s*(\d+)", text)
    return int(match.group(1)) if match else None


@contextlib.contextmanager
def codex_hunts_view(page, log):
    """Abre o Codex, vai pra aba 'Hunts' e desliga os 3 filtros que escondem
    entradas (Entregaveis / Esconder concluidos / Esconder bloqueadas) pra
    enxergar TUDO. Ao sair, devolve o que mexeu (filtros, atributo, busca) e
    fecha o Codex - a rotina de entrega depende desses filtros ligados e os
    reafirma a cada ciclo, mas devolver aqui evita brigar com ela no meio."""
    open_codex(page)
    unchecked = []
    previous_attr = ""
    try:
        page.click('.codex-side .codex-tab:has-text("Hunts")', timeout=3000)
        search = page.query_selector(".cx-search")
        if search is not None and (search.input_value() or ""):
            search.fill("", timeout=3000)
        for label in CODEX_FILTER_LABELS:
            checkbox = page.locator(f'label.cx-check:has-text("{label}") input[type="checkbox"]')
            if checkbox.count() and checkbox.first.is_checked():
                page.locator(f'label.cx-check:has-text("{label}")').first.click(timeout=3000)
                unchecked.append(label)
        attr_el = page.query_selector(".cx-attr")
        previous_attr = attr_el.input_value() if attr_el else ""
        page.wait_for_timeout(400)
        yield
    finally:
        try:
            attr_el = page.query_selector(".cx-attr")
            if attr_el is not None and attr_el.input_value() != previous_attr:
                attr_el.select_option(previous_attr, timeout=3000)
            search = page.query_selector(".cx-search")
            if search is not None and (search.input_value() or ""):
                search.fill("", timeout=3000)
            for label in unchecked:
                checkbox = page.locator(f'label.cx-check:has-text("{label}") input[type="checkbox"]')
                if checkbox.count() and not checkbox.first.is_checked():
                    page.locator(f'label.cx-check:has-text("{label}")').first.click(timeout=3000)
            page.click("#codex-modal-close", timeout=2000)
        except Exception as error:
            log(f"  Aviso: nao consegui devolver o Codex ao estado de antes: {error}")


def read_codex_hunt_entries(page, attr=""):
    """Dentro de 'codex_hunts_view': filtra pelo atributo ('' = todos) e le as
    entradas de TODAS as paginas (o jogo mostra 30 por pagina; com atributo
    escolhido geralmente cabe em 1). Volta pra 1a pagina no fim."""
    page.select_option(".cx-attr", attr, timeout=3000)
    page.wait_for_timeout(700)
    entries, seen = [], set()
    for _ in range(40):
        for raw in page.evaluate(CODEX_ENTRIES_JS):
            if raw["name"] and raw["name"] not in seen:
                seen.add(raw["name"])
                entries.append(build_codex_entry(raw))
        if not click_codex_pager_button(page, 2):  # 2 = 'proxima'
            break
        page.wait_for_timeout(450)
    try:  # voltar pra 1a pagina e' so' arrumacao - nao pode derrubar a leitura
        click_codex_pager_button(page, 0)  # 0 = 'primeira'
    except Exception:
        pass
    return entries


def click_codex_pager_button(page, index):
    """Clica num botao do paginador do Codex por POSICAO (0 primeira, 1
    anterior, 2 proxima, 3 ultima) - o 'title' da 'proxima' some a partir da
    2a pagina (confirmado ao vivo) e o paginador e' recriado a cada troca, o
    que invalida referencias guardadas; por isso o clique e' feito dentro da
    propria pagina, no momento. Retorna False se nao ha como avancar."""
    return bool(page.evaluate(
        """(index) => {
            const buttons = document.querySelectorAll('#codex-pager:not(.hidden) .cx-pgbtn');
            if (buttons.length < 4 || buttons[index].disabled) return false;
            buttons[index].click();
            return true;
        }""",
        index,
    ))


def read_codex_campaign_data(page, log):
    """Le TODAS as entradas de Hunts do Codex (recompensa, progresso,
    bloqueio) + a lista de Hunts do jogo, numa pagina ja conectada."""
    with codex_hunts_view(page, log):
        entries = read_codex_hunt_entries(page, "")
    hunts = read_hunt_list(page, log)
    return {"entries": entries, "hunts": hunts}


def fetch_codex_campaign_data(log=print):
    """Com o bot PARADO: conecta no Chrome (abrindo se precisar) e le os
    dados da Campanha de Codex. Com o bot rodando, use
    'request_codex_refresh_from_bot' - mexer no Codex numa segunda conexao
    briga com a rotina de entrega, que abre/fecha o mesmo painel."""
    if not launch_browser(log):
        return None
    with sync_playwright() as playwright:
        page = connect_game_page(playwright)
        return read_codex_campaign_data(page, log)


# ---------- Mapa do Codex pro market ----------
# O market (market/codex.py) cruza o que FALTA entregar no Codex da conta com
# os leiloes de empilhaveis. So' o bot enxerga o Codex (precisa do jogo
# logado), entao ele le as entradas - recompensa, status e requisitos
# 'tem/precisa' de cada item - e grava um JSON ao lado do banco do market.
# So' le (nunca entrega nem desbloqueia nada). Equipamento fica de fora: os
# requisitos dele sao pecas (nao empilhaveis).
CODEX_PROGRESS_TABS = (("Hunts", "hunt"), ("Bosses", "boss"))

CODEX_PROGRESS_JS = """() => Array.from(document.querySelectorAll('.cx-list .cx-entry')).map(e => {
    const nameEl = e.querySelector('.cx-entry-name');
    const bar = e.querySelector('.cx-bar-fill');
    const btn = e.querySelector('.cx-entry-side button.cx-give');
    const m = ((bar && bar.getAttribute('style')) || '').match(/width:\\s*([\\d.]+)%/);
    const btnText = btn ? btn.textContent.trim() : '';
    const unlock = /^Desbloquear/i.test(btnText);
    return {
        name: ((nameEl && (nameEl.getAttribute('title') || nameEl.textContent)) || '').trim(),
        slug: (e.querySelector('.cx-entry-num') && e.querySelector('.cx-entry-num').getAttribute('title')) || '',
        bonus_text: ((e.querySelector('.cx-entry-bonus') || {}).textContent || '').trim(),
        progress: m ? parseFloat(m[1]) : 0,
        done: e.classList.contains('done'),
        locked: unlock || !!(nameEl && nameEl.querySelector('svg')),
        unlock_text: unlock ? btnText : '',
        tiles: Array.from(e.querySelectorAll('.cx-tiles .cx-tile')).map(t => ({
            label: t.getAttribute('aria-label') || '',
            ready: ((t.querySelector('.cx-tile-rdy') || {}).textContent || '').trim(),
        })),
    };
})"""

# aria-label de cada requisito: '<item>[\n...] <tem>/<precisa>' (numeros no
# formato pt-BR, ex. '1.900/1.900'); o tem ja vem limitado ao precisa.
CODEX_TILE_RE = re.compile(r"^(?P<text>.*?)\s+(?P<have>[\d.]+)/(?P<need>[\d.]+)\s*$", re.S)


def digits_to_int(text):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else 0


def parse_codex_tile(tile):
    """{'label': 'diabolic skull 120/275', 'ready': '+40'} ->
    {'item': 'diabolic skull', 'need': 275, 'have': 120, 'ready': 40} ('ready'
    = o que ja esta nas suas bags e a entrega consome). None se nao der."""
    match = CODEX_TILE_RE.match((tile.get("label") or "").strip())
    if not match:
        return None
    first_line = match.group("text").strip().split("\n")[0].strip()
    # tira o que o jogo acrescenta ao nome: ' (Epico)', ' +3'
    item = re.sub(r"(\s+\([^)]*\)|\s+\+\d+)+$", "", first_line).strip().lower()
    need = digits_to_int(match.group("need"))
    if not item or not need:
        return None
    return {"item": item, "need": need, "have": min(digits_to_int(match.group("have")), need),
            "ready": digits_to_int(tile.get("ready"))}


def build_progress_entry(raw, cat):
    entry = build_codex_entry(raw)
    entry["cat"] = cat
    entry["reqs"] = [r for r in (parse_codex_tile(t) for t in raw.get("tiles") or []) if r]
    return entry


def read_codex_tab_progress(page, cat):
    """Aba ja aberta e com os filtros desligados: le todas as paginas."""
    page.select_option(".cx-attr", "", timeout=3000)
    page.wait_for_timeout(700)
    entries, seen = [], set()
    for _ in range(40):
        for raw in page.evaluate(CODEX_PROGRESS_JS):
            if raw["name"] and raw["name"] not in seen:
                seen.add(raw["name"])
                entries.append(build_progress_entry(raw, cat))
        if not click_codex_pager_button(page, 2):  # 2 = 'proxima'
            break
        page.wait_for_timeout(450)
    try:  # voltar pra 1a pagina e' so' arrumacao
        click_codex_pager_button(page, 0)
    except Exception:
        pass
    return entries


def read_codex_progress(page, log):
    """Le o Codex da conta (Hunts + Bosses) numa pagina ja conectada. Mesmo
    cuidado da Campanha: 'codex_hunts_view' desliga os filtros que escondem
    entradas e devolve tudo como estava ao sair."""
    entries = []
    with codex_hunts_view(page, log):
        for label, cat in CODEX_PROGRESS_TABS:
            if label != "Hunts":
                page.click(f'.codex-side .codex-tab:has-text("{label}")', timeout=3000)
                search = page.query_selector(".cx-search")
                if search is not None and (search.input_value() or ""):
                    search.fill("", timeout=3000)
                page.wait_for_timeout(500)
            entries.extend(read_codex_tab_progress(page, cat))
    opened = sum(1 for e in entries if e["status"] == "open")
    log(f"  Codex mapeado pro market: {len(entries)} entradas ({opened} abertas).")
    return {"version": 1, "source": "bot", "read_at": int(time.time() * 1000), "entries": entries}


def fetch_codex_progress(log=print):
    """Com o bot PARADO: conecta no navegador (abrindo se precisar) e le o
    Codex. Com o bot rodando use 'request_from_bot(read_codex_progress)'."""
    if not launch_browser(log):
        return None
    with sync_playwright() as playwright:
        page = connect_game_page(playwright)
        return read_codex_progress(page, log)


def save_codex_progress(payload, path):
    """Grava o JSON de forma atomica (o market pode estar lendo)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
    os.replace(tmp, path)


# Pedido de atualizacao da lista da Campanha feito pela GUI com o bot rodando:
# a propria thread do bot atende (no proximo tick, ve 'run'), ja que ela e'
# quem tem a conexao com o jogo e nao concorre com as outras rotinas.
CODEX_REFRESH_REQUEST = {"pending": False, "result": None, "done": threading.Event()}


def request_codex_refresh_from_bot(timeout=120):
    """Pede pra thread do bot ler os dados da Campanha de Codex e espera a
    resposta (None se demorar demais ou falhar)."""
    CODEX_REFRESH_REQUEST["result"] = None
    CODEX_REFRESH_REQUEST["done"].clear()
    CODEX_REFRESH_REQUEST["pending"] = True
    if not CODEX_REFRESH_REQUEST["done"].wait(timeout):
        CODEX_REFRESH_REQUEST["pending"] = False
        return None
    return CODEX_REFRESH_REQUEST["result"]


def reset_campaign_memory():
    """Chamado pela GUI quando a fila muda - esquece o que ja decidiu."""
    CAMPAIGN_MEMORY.update({
        "target_hunt": None, "target_entry": None, "done": set(), "skip_until": {},
        "travel_fails": 0, "training_sent": False, "missing_logged": set(),
        "baseline_done": None, "last_check": 0.0, "last_full": 0.0,
    })


def codex_prerequisite_done(page, entry):
    """O nivel anterior da mesma hunt (ex: 'Wereliones I' pra 'Wereliones II')
    ja esta concluido? Busca pelo nome da hunt, sem filtro de atributo.
    Dentro de 'codex_hunts_view'."""
    if entry["level"] <= 1:
        return True
    page.select_option(".cx-attr", "", timeout=3000)
    search = page.query_selector(".cx-search")
    if search is None:
        return False
    try:
        search.fill(entry["base"], timeout=3000)
        page.wait_for_timeout(800)
        previous = [
            e for e in (build_codex_entry(raw) for raw in page.evaluate(CODEX_ENTRIES_JS))
            if e["base"].lower() == entry["base"].lower() and e["level"] == entry["level"] - 1
        ]
    finally:
        search.fill("", timeout=3000)
        page.wait_for_timeout(300)
    return bool(previous) and previous[0]["status"] == "done"


def try_unlock_codex_entry(page, queue_item, entry, log):
    """Paga o desbloqueio de uma entrada do Codex (so' chamada se o usuario
    marcou 'pagar desbloqueio' pra essa entrada na Campanha). Trava de
    seguranca em camadas: o custo atual nao pode passar do valor autorizado
    quando ele marcou; o nivel anterior precisa estar concluido; e o botao de
    confirmacao do jogo precisa mesmo dizer 'Desbloquear' antes do clique.
    Dentro de 'codex_hunts_view' (com o atributo da entrada selecionado)."""
    name = entry["name"]
    cost = entry.get("unlock_cost") or 0
    authorized = queue_item.get("unlock_cost") or 0
    if not cost or cost > authorized:
        log(f"  Desbloqueio de '{name}' custa {format_gold(cost)} (autorizado ate {format_gold(authorized)}) - NAO desbloqueado.")
        return False
    if not codex_prerequisite_done(page, entry):
        log(f"  '{name}': o nivel anterior ainda nao foi concluido - nao desbloqueio ainda.")
        return False

    page.select_option(".cx-attr", queue_item.get("attr", ""), timeout=3000)
    page.wait_for_timeout(700)
    index = page.evaluate(
        """(wanted) => Array.from(document.querySelectorAll('.cx-list .cx-entry')).findIndex(
            e => ((e.querySelector('.cx-entry-name')?.getAttribute('title')) || '').trim() === wanted
        )""",
        name,
    )
    rows = page.query_selector_all(".cx-list .cx-entry")
    if index is None or index < 0 or index >= len(rows):
        log(f"  Nao achei '{name}' na tela pra desbloquear.")
        return False
    button = rows[index].query_selector("button.cx-give")
    if button is None or not (button.text_content() or "").strip().lower().startswith("desbloquear"):
        log(f"  '{name}': botao de desbloqueio nao encontrado.")
        return False

    log(f"  Desbloqueando '{name}' ({format_gold(cost)} de gold)...")
    button.click(timeout=3000)
    try:
        confirm = page.wait_for_selector("#confirm-yes", timeout=3000, state="visible")
    except Exception:
        log("  Confirmacao do desbloqueio nao apareceu.")
        return False
    # a janela de confirmacao traz o nome da entrada, o custo e o saldo
    # (confirmado ao vivo: 'Desbloquear "Dominio: X"? / Custo do desbloqueio
    # 50.000.000 / Seu saldo ...') - so confirma se bater com o nome E o
    # custo esperados, nao so' pelo texto do botao.
    confirm_text = (confirm.text_content() or "").strip()
    try:
        body_text = (page.inner_text("#confirm-modal-body", timeout=2000) or "").strip()
    except Exception:
        body_text = ""
    if (
        "desbloquear" not in confirm_text.lower()
        or name not in body_text
        or format_gold(cost) not in body_text
    ):
        log(f"  BLOQUEADO por seguranca: a confirmacao nao e' a esperada ('{confirm_text}' / '{body_text[:120]}') - cancelando.")
        try:
            page.click("#confirm-no", timeout=2000)
        except Exception:
            page.keyboard.press("Escape")
        return False
    confirm.click(timeout=3000)
    page.wait_for_timeout(1200)

    refreshed = next(
        (e for e in (build_codex_entry(raw) for raw in page.evaluate(CODEX_ENTRIES_JS)) if e["name"] == name), None
    )
    if refreshed is not None and refreshed["status"] == "locked":
        log(f"  '{name}' continua bloqueada apos confirmar (gold insuficiente?).")
        return False
    log(f"  '{name}' desbloqueada.")
    record_activity(f"Codex desbloqueado: '{name}' ({format_gold(cost)} de gold).")
    return True


def pick_campaign_target(page, pending, log):
    """Abre o Codex uma vez e devolve o primeiro item da fila ('pending', ja
    na ordem) que da' pra trabalhar agora: aberto, ou bloqueado MAS com
    desbloqueio autorizado e conseguido. Marca como feitas as ja 100%."""
    by_attr = {}
    now = time.monotonic()
    with codex_hunts_view(page, log):
        for item in pending:
            name = item["name"]
            attr = item.get("attr", "")
            if attr not in by_attr:
                by_attr[attr] = {e["name"]: e for e in read_codex_hunt_entries(page, attr)}
            entry = by_attr[attr].get(name)
            if entry is None:
                if name not in CAMPAIGN_MEMORY["missing_logged"]:
                    CAMPAIGN_MEMORY["missing_logged"].add(name)
                    log(f"  Campanha de Codex: '{name}' nao apareceu no Codex (renomeada?) - pulando.")
                continue
            if entry["status"] == "done":
                CAMPAIGN_MEMORY["done"].add(name)
                log(f"  '{name}' completou 100% no Codex!")
                play_achievement_sound()
                record_activity(f"Codex completo: '{name}'.")
                continue
            if entry["status"] == "locked":
                if not item.get("unlock_ok"):
                    if name not in CAMPAIGN_MEMORY["missing_logged"]:
                        CAMPAIGN_MEMORY["missing_logged"].add(name)
                        log(f"  '{name}' esta bloqueada e o desbloqueio nao foi autorizado - pulando.")
                    continue
                if try_unlock_codex_entry(page, item, entry, log):
                    return item
                CAMPAIGN_MEMORY["skip_until"][name] = now + CAMPAIGN_SKIP_SECONDS
                continue
            return item
    return None


def finish_campaign_queue(page, queue, log):
    """Nada (mais) pra fazer na fila: volta pra hunt padrao; sem hunt padrao,
    vai pro treino online. Feito uma vez so' (CAMPAIGN_MEMORY['training_sent'])."""
    had_target = CAMPAIGN_MEMORY["target_entry"] is not None
    CAMPAIGN_MEMORY["target_hunt"] = None
    CAMPAIGN_MEMORY["target_entry"] = None
    if had_target and all(item["name"] in CAMPAIGN_MEMORY["done"] for item in queue):
        log("  Campanha de Codex concluida - todas as entradas da fila completas.")
        record_activity("Campanha de Codex concluida.")
    if CAMPAIGN_MEMORY["training_sent"]:
        return
    CAMPAIGN_MEMORY["training_sent"] = True
    if load_settings().get("default_hunt", ""):
        log("  Fila da Campanha sem nada pra fazer agora - voltando pra hunt padrao.")
        return_to_default_hunt(page, log, force=True)
        return
    log("  Fila da Campanha sem nada pra fazer e sem hunt padrao - indo pro treino online.")
    LAST_KNOWN_HUNT_MEMORY["name"] = None
    try:
        click_open_wave(page, "#wave-title")
        page.click('.tp-opt[data-tp="exercise"]', timeout=3000)
        log("  'Treino online' clicado.")
    except Exception as error:
        log(f"  Erro ao ir pro treino online: {error}")


def execute_dom_codex_campaign_step(page, step, log):
    """Passo tipo 'dom_codex_campaign': percorre a fila escolhida pelo usuario
    (step['queue'], ja ordenada: por atributo, % maior primeiro) e trabalha no
    primeiro item ainda nao concluido - a hunt dele vira a hunt 'base' (ve
    return_to_default_hunt), entao chefes/tasks de guild interrompem e o bot
    volta pra ela sozinho. Concluiu um item -> segue pro proximo da fila; fila
    esgotada -> hunt padrao, ou treino online se nao houver (ve
    finish_campaign_queue). Tasks de guild em andamento tem prioridade."""
    queue = step.get("queue") or []
    if not queue:
        CAMPAIGN_MEMORY["target_hunt"] = None
        CAMPAIGN_MEMORY["target_entry"] = None
        return True
    if task_grinding():
        return True

    now = time.monotonic()
    pending = [
        item for item in queue
        if item["name"] not in CAMPAIGN_MEMORY["done"] and CAMPAIGN_MEMORY["skip_until"].get(item["name"], 0) <= now
    ]

    # o alvo atual nao conseguiu chegar na hunt por varios ciclos seguidos
    # (hunt inexistente/bloqueada) - pula por um tempo em vez de ficar tentando
    gave_up = False
    target_hunt = CAMPAIGN_MEMORY["target_hunt"]
    if target_hunt:
        try:
            current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
        except Exception:
            current_hunt = target_hunt
        if current_hunt == target_hunt:
            CAMPAIGN_MEMORY["travel_fails"] = 0
        else:
            CAMPAIGN_MEMORY["travel_fails"] += 1
            if CAMPAIGN_MEMORY["travel_fails"] >= CAMPAIGN_MAX_TRAVEL_FAILS:
                entry_name = CAMPAIGN_MEMORY["target_entry"]
                log(f"  Nao consegui ir pra hunt '{target_hunt}' ('{entry_name}') - pulando por {CAMPAIGN_SKIP_SECONDS // 60}min.")
                CAMPAIGN_MEMORY["skip_until"][entry_name] = now + CAMPAIGN_SKIP_SECONDS
                CAMPAIGN_MEMORY["target_hunt"] = None
                CAMPAIGN_MEMORY["target_entry"] = None
                CAMPAIGN_MEMORY["travel_fails"] = 0
                pending = [i for i in pending if i["name"] != entry_name]
                gave_up = True

    count = None
    if pending and CAMPAIGN_MEMORY["last_full"] and not gave_up:
        # ja avaliou a fila antes: so' reavalia se algo mudou. Entre as
        # olhadas nao toca no Codex (so' a conferencia de hunt, la' em cima).
        since_full = now - CAMPAIGN_MEMORY["last_full"]
        if since_full < CAMPAIGN_FULL_RECHECK_SECONDS:
            if now - CAMPAIGN_MEMORY["last_check"] < CAMPAIGN_CHECK_SECONDS:
                return True
            count = read_codex_completed_count(page)
            CAMPAIGN_MEMORY["last_check"] = now
            if count is not None and count == CAMPAIGN_MEMORY["baseline_done"]:
                return True  # nada novo concluido desde a ultima leitura completa
            if count is not None:
                log(f"  Campanha de Codex: contador de concluidos mudou ({CAMPAIGN_MEMORY['baseline_done']} -> {count}) - reavaliando a fila.")

    if pending:
        if count is None:
            count = read_codex_completed_count(page)
        target = pick_campaign_target(page, pending, log)
        CAMPAIGN_MEMORY["baseline_done"] = count
        CAMPAIGN_MEMORY["last_full"] = CAMPAIGN_MEMORY["last_check"] = now
    else:
        target = None
        CAMPAIGN_MEMORY["last_full"] = 0.0
    if target is None:
        finish_campaign_queue(page, queue, log)
        return True

    hunt = target.get("hunt") or ""
    if CAMPAIGN_MEMORY["target_entry"] != target["name"]:
        log(f"  Campanha de Codex: agora '{target['name']}' (hunt '{hunt}').")
        CAMPAIGN_MEMORY["travel_fails"] = 0
    CAMPAIGN_MEMORY["target_entry"] = target["name"]
    CAMPAIGN_MEMORY["target_hunt"] = hunt or None
    CAMPAIGN_MEMORY["training_sent"] = False
    if hunt:
        return_to_default_hunt(page, log, force=True)
    return True


# ---------- Pocoes de boost (Mercador) ----------
# Fatos do jogo confirmados ao vivo: o Mercador (Comercio > Mercador > Pocoes)
# vende 6 pocoes a 10kk; o limite de compra e' 1 POR TIPO POR DIA (o '+' do
# carrinho trava na 1a unidade; reseta a meia-noite); a compra chega na
# Caixa de entrada do Armazem (clicar na pocao manda pra mochila); na mochila,
# clicar numa pocao USA 1 e cada uso soma +30 min de boost na conta (empilha).
POTION_BOOST_SECONDS = 30 * 60
POTION_CATALOG_DEFAULT = [
    {"name": "potion of critical", "id": 62165, "price": 10000000, "desc": "+10% crit por 30 min"},
    {"name": "potion of dodge", "id": 62166, "price": 10000000, "desc": "+8% esquiva por 30 min"},
    {"name": "potion of fatal", "id": 62167, "price": 10000000, "desc": "+8% fatal por 30 min"},
    {"name": "potion of momentum", "id": 62168, "price": 10000000, "desc": "+12% momentum por 30 min"},
    {"name": "potion of speed", "id": 62169, "price": 10000000, "desc": "+15% atk speed por 30 min"},
    {"name": "potion of transcendence", "id": 62170, "price": 10000000, "desc": "+8% avatar por 30 min"},
]
POTION_BUY_RETRY_SECONDS = 30 * 60
BOSS_RUN_HISTORY_KEEP = 10
BOSS_POTION_MARGIN = 1.15

# 'active_until' (monotonic): ate quando o boost de cada pocao usada vale;
# 'retry_after': nao tenta comprar de novo antes disso (falha/saldo).
BOSS_POTION_MEMORY = {"active_until": {}, "retry_after": {}}

# Pedido da GUI pra rodar algo NA thread do bot (que e' quem tem a conexao com
# o jogo - mexer no Mercador/Armazem numa segunda conexao briga com as rotinas).
BOT_CALL_REQUEST = {"fn": None, "result": None, "done": threading.Event()}


def request_from_bot(fn, timeout=180):
    """Pede pra thread do bot rodar 'fn(page, log)' no proximo tick e espera
    o resultado (None se demorar demais ou falhar)."""
    BOT_CALL_REQUEST["result"] = None
    BOT_CALL_REQUEST["done"].clear()
    BOT_CALL_REQUEST["fn"] = fn
    if not BOT_CALL_REQUEST["done"].wait(timeout):
        BOT_CALL_REQUEST["fn"] = None
        return None
    return BOT_CALL_REQUEST["result"]


def potion_config():
    """Escolhas do usuario (settings.json -> 'potions'): {'use_in_bosses':
    bool, 'items': {nome: {'use_qty': int, 'buy_daily': bool}}}. Sem nada
    salvo = tudo desligado (nada e' comprado nem usado ate' ele salvar)."""
    raw = load_settings().get("potions") or {}
    items = {}
    for name, value in (raw.get("items") or {}).items():
        value = value or {}
        try:
            use_qty = max(0, int(value.get("use_qty") or 0))
        except (TypeError, ValueError):
            use_qty = 0
        items[name] = {"use_qty": use_qty, "buy_daily": bool(value.get("buy_daily"))}
    return {"use_in_bosses": bool(raw.get("use_in_bosses")), "items": items}


def today_key():
    return time.strftime("%Y-%m-%d")


def boss_potion_suggestion():
    """Sugestao de quantas pocoes por tipo cobrem a lista de chefes: media
    dos tempos das ultimas sequencias COMPLETAS (BOSS_RUN_HISTORY) com
    BOSS_POTION_MARGIN de folga, dividido pela duracao de cada pocao
    (arredondado pra cima). None enquanto nao ha historico."""
    history = load_state().get("boss_run_history") or []
    seconds = [h["seconds"] for h in history if h.get("seconds")]
    if not seconds:
        return None
    average = sum(seconds) / len(seconds)
    needed = max(1, -(-int(average * BOSS_POTION_MARGIN) // POTION_BOOST_SECONDS))
    return {"runs": len(seconds), "avg_minutes": average / 60, "needed": needed}


def record_boss_run(seconds, bosses):
    state = load_state()
    history = state.get("boss_run_history") or []
    history.append({"date": today_key(), "seconds": int(seconds), "bosses": bosses})
    state["boss_run_history"] = history[-BOSS_RUN_HISTORY_KEEP:]
    save_state(state)


def open_merchant_potions(page):
    """Abre Comercio > Mercador na categoria 'Pocoes'."""
    if page.is_visible("#picker-modal"):
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    if not page.is_visible(".merchant-card"):
        # o menu 'Comercio' abre por hover (com atraso) E por clique (que
        # ALTERNA): clicar logo depois do hover fechava o que ele acabou de
        # abrir (corrida confirmada ao vivo). Tenta o hover primeiro, confere
        # se o item 'Mercador' apareceu e so' entao cai pro clique.
        for attempt in range(3):
            if page.is_visible("#tab-merchant"):
                break
            if attempt % 2 == 0:
                page.hover("#tab-comercio", timeout=3000)
            else:
                page.click("#tab-comercio", timeout=3000)
            page.wait_for_timeout(700)
        page.click("#tab-merchant", timeout=3000)
        page.wait_for_selector(".merchant-card .store-sidebtn", timeout=5000)
        page.wait_for_timeout(700)
    back = page.locator('.merchant-card button:has-text("Voltar")')
    if back.count() and back.first.is_visible():  # estava no Historico
        back.first.click(timeout=3000)
        page.wait_for_timeout(600)
    side = page.locator('.merchant-card .store-sidebtn:has-text("Poções")').first
    if "on" not in (side.get_attribute("class") or "").split():
        side.click(timeout=3000)
        page.wait_for_timeout(700)
    page.wait_for_selector(".merchant-card .gs-row", timeout=4000)


def close_merchant(page):
    """Fecha o carrinho (se aberto) e o Mercador - confere que fechou."""
    for _ in range(3):
        try:
            if page.is_visible(".gs-cartlayer"):
                page.locator(".gs-cartwin-close").first.click(timeout=2000)
                page.wait_for_timeout(300)
            if page.is_visible(".merchant-card"):
                page.click("#merchant-modal-close", timeout=3000)
                page.wait_for_timeout(400)
        except Exception:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        if not page.is_visible(".merchant-card"):
            return


def clear_merchant_cart(page, log):
    """O carrinho do Mercador persiste (sobra de uma tentativa interrompida ou
    de algo adicionado na mao) - o bot so' pode comprar EXATAMENTE o que
    pediu, entao comeca sempre de um carrinho vazio. Retorna quantos itens
    tinha."""
    fab = page.locator(".gs-cartfab")
    if not (fab.count() and fab.first.is_visible()):
        return 0
    badge = page.evaluate("() => ((document.querySelector('.gs-cartfab-badge') || {}).textContent || '')")
    count = int(re.sub(r"\D", "", badge) or 0)
    fab.first.click(timeout=3000)
    page.wait_for_timeout(600)
    page.locator(".gs-cart-clear").first.click(timeout=3000)
    page.wait_for_timeout(500)
    try:
        page.locator(".gs-cartwin-close").first.click(timeout=1000)
        page.wait_for_timeout(300)
    except Exception:
        pass
    if count:
        log(f"  O carrinho do Mercador tinha {count} item(ns) de antes - limpei pra comprar so' o pedido.")
    return count


def read_merchant_potions(page):
    """Dentro do Mercador/Pocoes: lista as pocoes (nome, id, descricao, preco,
    status: 'buyable' / 'limit' (limite diario) / 'in_cart') e o saldo."""
    raw = page.evaluate(
        r"""() => ({
            rows: Array.from(document.querySelectorAll('.merchant-card .gs-row')).map(r => {
                const b = r.querySelector('.gs-buy');
                return {
                    name: (r.querySelector('.gs-name') || {}).textContent || '',
                    desc: (r.querySelector('.gs-desc') || {}).textContent || '',
                    id_text: (r.querySelector('.gs-id') || {}).textContent || '',
                    price_text: b ? b.textContent : '',
                    title: b ? b.title : '',
                    disabled: b ? b.disabled : true,
                };
            }),
            balance_text: (document.querySelector('.merchant-card .merchant-balances') || {}).innerText || '',
        })"""
    )
    potions = []
    for row in raw["rows"]:
        title = (row["title"] or "").lower()
        if not row["disabled"]:
            status = "buyable"
        elif "limite" in title:
            status = "limit"
        elif "carrinho" in title:
            status = "in_cart"
        else:
            status = "unavailable"
        id_match = re.search(r"id\s+(\d+)", row["id_text"])
        # so' o PRIMEIRO numero: com a pocao ja no carrinho o botao mostra
        # '10.000.000 · 1x' e juntar todos os digitos lia o '1' do '1x' no preco
        # (100.000.001) - confirmado ao vivo.
        price_match = re.search(r"\d[\d.]*", row["price_text"])
        potions.append({
            "name": row["name"].strip(), "desc": row["desc"].strip(),
            "id": int(id_match.group(1)) if id_match else None,
            "price": int(price_match.group(0).replace(".", "")) if price_match else 0, "status": status,
        })
    balance_match = re.search(r"[\d.]+", raw["balance_text"])
    balance = int(balance_match.group(0).replace(".", "")) if balance_match else None
    return {"potions": potions, "balance": balance}


def buy_potions_now(page, names, log):
    """Compra 1 de cada pocao em 'names' (limite do jogo: 1 por tipo por dia)
    pelo caminho normal: preco -> carrinho -> confere o carrinho -> confirmar.
    So' confirma se o carrinho tiver EXATAMENTE as pocoes pedidas, 1 de cada,
    e o saldo cobrir. Confirma de verdade a compra olhando se cada pocao
    passou a mostrar 'limite diario' depois. Retorna {'bought': [...],
    'limit_reached': [...], 'failed': [...]}."""
    result = {"bought": [], "limit_reached": [], "failed": []}
    open_merchant_potions(page)
    try:
        if clear_merchant_cart(page, log):
            open_merchant_potions(page)
        data = read_merchant_potions(page)
        by_name = {p["name"]: p for p in data["potions"]}
        to_buy = []
        for name in names:
            potion = by_name.get(name)
            if potion is None:
                log(f"  '{name}' nao esta no Mercador.")
                result["failed"].append(name)
            elif potion["status"] == "limit":
                log(f"  '{name}': limite diario de compra ja atingido hoje.")
                result["limit_reached"].append(name)
            elif potion["status"] == "buyable":
                to_buy.append(potion)
            else:
                result["failed"].append(name)
        if not to_buy:
            return result
        total = sum(p["price"] for p in to_buy)
        if data["balance"] is not None and data["balance"] < total:
            log(f"  Saldo insuficiente pra comprar pocoes ({format_gold(data['balance'])} < {format_gold(total)}).")
            result["failed"].extend(p["name"] for p in to_buy)
            return result

        for potion in to_buy:
            row = page.locator(f'.merchant-card .gs-row:has(.gs-name:text-is("{potion["name"]}"))').first
            row.locator(".gs-buy").click(timeout=3000)
            page.wait_for_timeout(400)
        page.locator(".gs-cartfab").first.click(timeout=3000)
        page.wait_for_timeout(700)
        cart = page.evaluate(
            r"""() => ({
                rows: Array.from(document.querySelectorAll('.gs-cart-row')).map(r => ({
                    name: ((r.querySelector('.gs-cart-name') || {}).textContent || '').trim(),
                    qty: ((r.querySelector('.gs-cart-qtyval') || {}).value || '').trim(),
                })),
                total_text: ((document.querySelector('.gs-cart-total') || {}).innerText || ''),
            })"""
        )
        cart_total = int(re.sub(r"\D", "", cart["total_text"]) or 0)
        expected = {p["name"] for p in to_buy}
        if (
            {r["name"] for r in cart["rows"]} != expected
            or any(r["qty"] != "1" for r in cart["rows"])
            or cart_total != total
        ):
            log(f"  O carrinho nao confere com o pedido ({cart}) - limpando sem comprar.")
            page.locator(".gs-cart-clear").first.click(timeout=3000)
            result["failed"].extend(expected)
            return result

        log(f"  Comprando {len(to_buy)} pocao(oes) por {format_gold(total)}: {', '.join(sorted(expected))}...")
        page.locator(".gs-cart-confirm").first.click(timeout=3000)
        page.wait_for_timeout(1500)
        try:  # o carrinho costuma fechar sozinho depois de confirmar
            page.locator(".gs-cartwin-close").first.click(timeout=1000)
            page.wait_for_timeout(300)
        except Exception:
            pass
        after ={p["name"]: p for p in read_merchant_potions(page)["potions"]}
        for potion in to_buy:
            if after.get(potion["name"], {}).get("status") == "limit":
                result["bought"].append(potion["name"])
            else:
                result["failed"].append(potion["name"])
        if result["bought"]:
            log(f"  Compra concluida: {', '.join(result['bought'])}.")
            record_activity(f"Pocoes compradas: {', '.join(n.replace('potion of ', '') for n in result['bought'])}.")
        if result["failed"]:
            log(f"  Nao confirmei a compra de: {', '.join(result['failed'])}.")
        return result
    finally:
        close_merchant(page)


def collect_potions_from_inbox(page, log):
    """Armazem > Caixa de entrada: clica em cada pocao (manda pra mochila).
    NAO usa 'Coletar tudo' (a caixa tem milhares de itens misturados).
    Retorna quantas pilhas coletou."""
    if not page.is_visible(".chest-grid"):
        if page.is_visible("#picker-modal"):
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        page.click("#tab-chest", timeout=3000)
        page.wait_for_selector(".chest-grid", timeout=5000)
        page.wait_for_timeout(700)
    inbox = page.locator('.chest-side .store-sidebtn:has-text("Caixa de entrada")').first
    if inbox.count() and "on" not in (inbox.get_attribute("class") or "").split():
        inbox.click(timeout=3000)
        page.wait_for_timeout(700)
    collected = 0
    try:
        for _ in range(30):
            index = page.evaluate(
                r"""() => Array.from(document.querySelectorAll('.chest-grid:not(.chest-bag) .chest-cell.filled')).findIndex(
                    c => /^potion of /i.test(((c.querySelector('img') || {}).alt) || ''))"""
            )
            if index is None or index < 0:
                break
            cells = page.query_selector_all(".chest-grid:not(.chest-bag) .chest-cell.filled")
            if index >= len(cells):
                break
            cells[index].click(timeout=3000)
            page.wait_for_timeout(600)
            collected += 1
    finally:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    if collected:
        log(f"  {collected} pilha(s) de pocao coletada(s) da Caixa de entrada pra mochila.")
    return collected


def count_backpack_potions(page):
    """{nome: quantidade} das pocoes de boost na mochila (so' le o HUD)."""
    return page.evaluate(
        r"""() => {
            const out = {};
            for (const c of document.querySelectorAll('#backpack-grid .cell.buffpot')) {
                const name = ((c.querySelector('img') || {}).alt) || '';
                const qty = parseInt(((c.querySelector('.qty') || {}).textContent || '1').replace(/\D/g, ''), 10) || 1;
                if (name) out[name] = (out[name] || 0) + qty;
            }
            return out;
        }"""
    )


def use_potion(page, name, quantity, log):
    """Clica na pocao da mochila 'quantity' vezes (cada clique usa 1 e soma
    +30 min de boost). Retorna quantas usou de verdade."""
    used = 0
    for _ in range(quantity):
        index = page.evaluate(
            r"""(wanted) => Array.from(document.querySelectorAll('#backpack-grid .cell.buffpot')).findIndex(
                c => (((c.querySelector('img') || {}).alt) || '') === wanted)""",
            name,
        )
        if index is None or index < 0:
            break
        cells = page.query_selector_all("#backpack-grid .cell.buffpot")
        if index >= len(cells):
            break
        cells[index].click(timeout=3000)
        page.wait_for_timeout(500)
        used += 1
    if used:
        log(f"  Usei {used}x '{name}' (+{used * POTION_BOOST_SECONDS // 60} min de boost).")
    return used


def read_potion_info(page, log):
    """Pra tela de Pocoes da GUI: catalogo do Mercador (preco/status de hoje)
    + estoque na mochila."""
    open_merchant_potions(page)
    try:
        data = read_merchant_potions(page)
    finally:
        close_merchant(page)
    return {"potions": data["potions"], "balance": data["balance"], "stock": count_backpack_potions(page)}


def fetch_potion_info(log=print):
    """Com o bot PARADO: conecta no Chrome e le o Mercador. Com o bot rodando,
    use request_from_bot(read_potion_info)."""
    if not launch_browser(log):
        return None
    with sync_playwright() as playwright:
        return read_potion_info(connect_game_page(playwright), log)


def prepare_boss_potions(page, log):
    """Antes de uma sequencia de chefes: se o usuario ligou 'usar pocoes',
    completa o que faltar (compra ate' o limite diario) e USA a quantidade
    escolhida de cada uma - cada uso empilha +30 min, entao nao precisa
    reaplicar no meio. Nao reusa enquanto o boost anterior ainda vale."""
    config = potion_config()
    if not config["use_in_bosses"]:
        return
    now = time.monotonic()
    wanted = {
        name: item["use_qty"] for name, item in config["items"].items()
        if item["use_qty"] > 0 and BOSS_POTION_MEMORY["active_until"].get(name, 0) <= now
    }
    if not wanted:
        return
    stock = count_backpack_potions(page)
    short = [name for name, qty in wanted.items() if stock.get(name, 0) < qty]
    if short:
        state = load_state()
        last_buy = state.get("potion_last_buy") or {}
        to_buy = [n for n in short if last_buy.get(n) != today_key()]
        if to_buy:
            for attempt in range(2):
                result = buy_potions_now(page, to_buy, log)
                for name in result["bought"] + result["limit_reached"]:
                    last_buy[name] = today_key()
                state["potion_last_buy"] = last_buy
                save_state(state)
                to_buy = [n for n in to_buy if n in result["failed"]]
                if not to_buy:
                    break
                if attempt == 0:
                    log("  Compra de pocoes nao confirmou - tentando mais uma vez...")
                    page.wait_for_timeout(2000)
            collect_potions_from_inbox(page, log)
            stock = count_backpack_potions(page)
    for name, qty in wanted.items():
        have = stock.get(name, 0)
        if have < qty:
            log(f"  '{name}': so' tenho {have} (queria {qty}) - uso o que tem.")
        used = min(qty, have)
        if used > 0 and use_potion(page, name, used, log):
            BOSS_POTION_MEMORY["active_until"][name] = time.monotonic() + used * POTION_BOOST_SECONDS


def execute_dom_potion_stock_step(page, step, log):
    """Passo tipo 'dom_potion_stock': garante a compra diaria (1 por dia) de
    cada pocao que o usuario marcou 'comprar 1 por dia' - estoque pras
    sequencias longas de chefes. Sem nada marcado nao toca no jogo. O dia da
    ultima compra fica no arquivo de estado; se o jogo ja mostrar o limite
    atingido (comprou na mao hoje), conta como feito."""
    config = potion_config()
    wanted = [name for name, item in config["items"].items() if item["buy_daily"]]
    if not wanted:
        return True
    state = load_state()
    last_buy = state.get("potion_last_buy") or {}
    now = time.monotonic()
    pending = [
        name for name in wanted
        if last_buy.get(name) != today_key() and BOSS_POTION_MEMORY["retry_after"].get(name, 0) <= now
    ]
    if not pending:
        return True
    result = buy_potions_now(page, pending, log)
    for name in result["bought"] + result["limit_reached"]:
        last_buy[name] = today_key()
    for name in pending:
        if name not in last_buy or last_buy[name] != today_key():
            BOSS_POTION_MEMORY["retry_after"][name] = now + POTION_BUY_RETRY_SECONDS
    state["potion_last_buy"] = last_buy
    save_state(state)
    if result["bought"]:
        collect_potions_from_inbox(page, log)
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


# ---------- Boss Slots (Cyclopedia > Boss Slots) ----------

# 'slots' = nomes dos chefes nos 2 slots (None = slot vazio), lido nesta
# sequencia de chefes (None = ainda nao lido - relê a cada sequencia nova);
# 'blocked' = (dia, pagar_tudo, teto) em que faltou gold/teto pra remover - nao
# reabre o Cyclopedia a cada chefe pra descobrir de novo (o preco so sobe no dia);
# 'unpickable' = chefes que nao aparecem na lista do slot (nao tenta de novo).
BOSS_SLOTS_MEMORY = {"slots": None, "blocked": None, "unpickable": set()}

BOSS_SLOTS_READ_JS = """() => ({
    gold: ((document.querySelector('#cyc-gold') || {}).textContent || ''),
    slots: Array.from(document.querySelectorAll('.bs-panel .bs-slot')).map(s => {
        const btn = s.querySelector('button.bs-btn');
        return {
            title: ((s.querySelector('.bs-box-title') || {}).textContent || '').trim(),
            remove: btn ? (btn.textContent || '').trim() : null,
            picking: !!s.querySelector('.bs-picklist'),
        };
    }),
})"""


def boss_slots_config():
    """Escolhas do usuario (settings.json -> 'boss_slots'): {'enabled': bool,
    'pay_all': bool, 'daily_cap': gold por dia}. Sem nada salvo = desligado."""
    raw = load_settings().get("boss_slots") or {}
    try:
        cap = max(0, int(raw.get("daily_cap") or 0))
    except (TypeError, ValueError):
        cap = 0
    return {"enabled": bool(raw.get("enabled")), "pay_all": bool(raw.get("pay_all")), "daily_cap": cap}


def gold_text(value):
    return f"{value:,}".replace(",", ".")


def boss_slots_spent_today():
    data = load_state().get("boss_slots") or {}
    return int(data.get("spent") or 0) if data.get("day") == time.strftime("%Y-%m-%d") else 0


def add_boss_slots_spent(cost):
    state = load_state()
    state["boss_slots"] = {"day": time.strftime("%Y-%m-%d"), "spent": boss_slots_spent_today() + cost}
    save_state(state)


def read_boss_slots(page):
    """(gold do lider, [{'name': chefe ou None (vazio), 'cost': preco do
    'Remover' em gold (0 = gratis)}]) com a aba Boss Slots aberta."""
    data = page.evaluate(BOSS_SLOTS_READ_JS)
    gold_digits = re.sub(r"\D", "", data["gold"])
    slots = []
    for slot in data["slots"]:
        name = None if slot["picking"] else (slot["title"].split(":", 1)[1].strip() if ":" in slot["title"] else None)
        cost_match = re.search(r"\(([\d.]+)\s*gold", slot["remove"] or "")
        slots.append({"name": name or None, "cost": int(cost_match.group(1).replace(".", "")) if cost_match else 0})
    return (int(gold_digits) if gold_digits else None), slots


def open_boss_slots(page):
    """Abre Cyclopedia > Boss Slots. True se os slots apareceram."""
    try:
        try:
            page.click("#tab-cyclopedia", timeout=3000)
        except Exception:
            page.click("#tab-cyclopedia", timeout=3000, force=True)
        tab_class = page.eval_on_selector('.cyc-tabbtn[data-tab="bossslots"]', "el => el.className") or ""
        if "on" not in tab_class.split():
            page.click('.cyc-tabbtn[data-tab="bossslots"]', timeout=3000)
        page.wait_for_selector(".bs-panel .bs-slot", timeout=4000)
        return True
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return False


def close_cyclopedia(page):
    try:
        page.click("#cyclopedia-modal-close", timeout=3000)
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        page.keyboard.press("Escape")


def pick_boss_slot(page, index, name):
    """Slot 'index' vazio (lista 'Escolher boss'): escolhe 'name' (gratis, sem
    confirmacao). True se o slot passou a mostrar esse chefe."""
    safe_name = name.replace('"', '\\"')
    slot = page.locator(".bs-panel .bs-slot").nth(index)
    pick = slot.locator(f'.bs-pick:has(.cyc-cell-name:text-is("{safe_name}"))')
    if pick.count() == 0:
        return False
    pick.first.click(timeout=3000)
    for _ in range(10):
        time.sleep(0.2)
        _, slots = read_boss_slots(page)
        if index < len(slots) and slots[index]["name"] == name:
            return True
    return False


def ensure_boss_slots(page, wanted, log):
    """Garante os chefes de 'wanted' ([o que vai lutar agora, o proximo pronto])
    nos 2 Boss Slots - mais chance de loot no combate. So mexe no slot que
    NAO tem nenhum deles. Remover custa gold e o preco sobe a cada troca no dia
    (1a gratis, depois 100k, 400k...): so remove se tiver gold pro preco do
    botao e se couber no teto do dia (ou 'pagar tudo'); senao segue sem trocar.
    Escolher o chefe no slot vazio e gratis. Se o chefe nao aparecer na lista
    do slot depois de remover, devolve o que estava (gratis)."""
    cfg = boss_slots_config()
    today = time.strftime("%Y-%m-%d")
    if BOSS_SLOTS_MEMORY["blocked"] == (today, cfg["pay_all"], cfg["daily_cap"]):
        return
    if not open_boss_slots(page):
        log("  Nao consegui abrir os Boss Slots - segue sem trocar.")
        return
    try:
        for name in wanted:
            gold, slots = read_boss_slots(page)
            names = [s["name"] for s in slots]
            BOSS_SLOTS_MEMORY["slots"] = names
            if name in names:
                continue
            index = next((i for i, s in enumerate(slots) if s["name"] is None), None)
            removed = None
            if index is None:
                index = next((i for i, s in enumerate(slots) if s["name"] not in wanted), None)
                if index is None:
                    break  # os 2 slots ja tem chefes desta sequencia
                cost = slots[index]["cost"]
                spent = boss_slots_spent_today()
                if gold is None or gold < cost or (not cfg["pay_all"] and spent + cost > cfg["daily_cap"]):
                    BOSS_SLOTS_MEMORY["blocked"] = (today, cfg["pay_all"], cfg["daily_cap"])
                    log(f"  Boss Slots: trocar custa {gold_text(cost)} gold (gasto hoje {gold_text(spent)}) - sem gold ou acima do teto, segue sem trocar.")
                    break
                removed = slots[index]["name"]
                page.locator(".bs-panel .bs-slot").nth(index).locator("button.bs-btn").first.click(timeout=3000)
                page.locator(".bs-panel .bs-slot").nth(index).locator(".bs-picklist").wait_for(timeout=3000)
                add_boss_slots_spent(cost)
                log(f"  Boss Slot {index + 1}: '{removed}' removido ({gold_text(cost)} gold).")
            if pick_boss_slot(page, index, name):
                log(f"  Boss Slot {index + 1}: '{name}' colocado.")
            else:
                BOSS_SLOTS_MEMORY["unpickable"].add(name)
                log(f"  '{name}' nao aparece na lista do Boss Slot - nao tento mais com ele.")
                if removed and not pick_boss_slot(page, index, removed):
                    log(f"  ATENCAO: Boss Slot {index + 1} ficou vazio - nao consegui devolver '{removed}'.")
        _, slots = read_boss_slots(page)
        BOSS_SLOTS_MEMORY["slots"] = [s["name"] for s in slots]
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        log(f"  Erro ao ajustar os Boss Slots: {error}")
        BOSS_SLOTS_MEMORY["slots"] = None
    finally:
        close_cyclopedia(page)


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

    BOSS_SLOTS_MEMORY["slots"] = None  # relê os Boss Slots uma vez por sequencia (o usuario pode ter mexido)

    target_name = None  # garante que exista mesmo se stop_event ja estiver setado ao entrar no laco
    fought_any = False
    potions_prepared = False  # pocoes (compra/uso) so' uma vez por sequencia, antes do 1o chefe
    sequence_started = None  # inicio do 1o combate - pra gravar quanto tempo a sequencia leva
    fights = 0
    amulet_blocked = False  # Stone Skin nao confirmado nesta chamada: chefes 'stone_skin' ficam de fora
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
        target_in_slot = False  # o usuario marcou 'Slot' (BossPicker) pra esse chefe
        next_name = None  # o proximo chefe da fila marcado 'Slot' (adianta ele no outro slot)
        for boss in step["bosses"]:
            if boss.get("enabled") and boss["name"] in ready_names:
                if amulet_blocked and boss.get("stone_skin"):
                    continue  # sem Stone Skin confirmado NAO enfrenta (pode matar os chares)
                if target_name is None:
                    target_name = boss["name"]
                    target_needs_stone_skin = bool(boss.get("stone_skin"))
                    target_in_slot = bool(boss.get("boss_slot"))
                    continue
                if boss.get("boss_slot"):
                    next_name = boss["name"]
                    break

        slots_cfg = boss_slots_config()
        slots_blocked = BOSS_SLOTS_MEMORY["blocked"] == (time.strftime("%Y-%m-%d"), slots_cfg["pay_all"], slots_cfg["daily_cap"])
        if target_name is not None and slots_cfg["enabled"] and not slots_blocked:
            # Boss Slots: o chefe de agora (se marcado 'Slot') e o proximo marcado
            # 'Slot' da fila nos 2 slots (mais chance de loot) - so os que o
            # usuario escolheu, porque cada troca fica mais cara. Sem proximo
            # marcado, so garante o atual.
            # So abre o Cyclopedia quando falta algum deles nos slots. Mesma
            # limitacao do amuleto: a lista de chefes e o Cyclopedia sao modais.
            wanted = [
                n for n in ((target_name if target_in_slot else None), next_name)
                if n and n not in BOSS_SLOTS_MEMORY["unpickable"]
            ]
            known = BOSS_SLOTS_MEMORY["slots"]
            if wanted and (known is None or any(n not in known for n in wanted)):
                page.keyboard.press("Escape")
                time.sleep(0.3)
                ensure_boss_slots(page, wanted, log)
                try:
                    click_open_wave(page, open_selector)
                    page.click(boss_menu_selector, timeout=3000)
                    ready_class = page.eval_on_selector(ready_selector, "el => el.className") or ""
                    if "on" not in ready_class.split():
                        page.click(ready_selector, timeout=3000)
                    time.sleep(0.3)
                except Exception as error:
                    log(f"  Erro ao reabrir a lista de Chefes apos os Boss Slots: {error}")
                    return False

        if target_name is not None:
            # pra chefes marcados 'stone_skin' (BossPicker), troca o amuleto do
            # EK ANTES do combate - ve 'equip_boss_amulet'. FICA trocado ate o
            # FIM de toda a sequencia (nao reverte a cada chefe): um chefe que
            # nao precisa de 'stone_skin' no meio da sequencia simplesmente nao
            # mexe no amuleto, trocado ou nao. So' reverte quando a sequencia
            # acaba (mais abaixo) ou e interrompida (falha no combate, logo a
            # seguir) - conforme pedido, pra nao ficar abrindo/fechando o
            # Helper a cada chefe a toa.
            if target_needs_stone_skin and not BOSS_AMULET_MEMORY["verified"]:
                # a lista de chefes ('#boss-modal', aberta la em cima pra ler
                # quem esta pronto) e o Helper sao os dois modais - o jogo nao
                # deixa abrir o Helper com a lista ainda aberta por cima
                # (CONFIRMADO ao vivo: o clique na aba EK do Helper ficava
                # bloqueado pelo proprio '#boss-modal' - "Erro ao abrir
                # Helper"). Fecha a lista, troca o amuleto, reabre a lista
                # (com o filtro 'Prontos' de novo) antes de seguir pro combate.
                page.keyboard.press("Escape")
                time.sleep(0.3)
                changed, verified = equip_boss_amulet(page, log)
                BOSS_AMULET_MEMORY["changed"] = merge_amulet_changed(BOSS_AMULET_MEMORY["changed"], changed)
                BOSS_AMULET_MEMORY["verified"] = verified
                if not verified:
                    # GARANTIA: chefe 'stone_skin' so' e' enfrentado com o amuleto
                    # CONFIRMADO no Helper. Pula ele (e os demais 'stone_skin') nesta
                    # rodada e tenta de novo em BOSS_AMULET_RETRY_SECONDS.
                    amulet_blocked = True
                    log(f"  ATENCAO: nao consegui confirmar o '{BOSS_AMULET_ITEM}' equipado - NAO vou enfrentar '{target_name}' (nem outros chefes Stone Skin) sem ele; nova tentativa em {BOSS_AMULET_RETRY_SECONDS // 60}min.")
                    continue  # a lista ja foi fechada (Escape) - o topo do laco reabre e escolhe outro alvo
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
            if not fought_any and not potions_prepared:
                potions_prepared = True
                if potion_config()["use_in_bosses"]:
                    # mesma limitacao do amuleto: a lista de chefes e o Mercador/
                    # Armazem sao modais - fecha a lista, cuida das pocoes e
                    # reabre a lista (com 'Prontos') antes do combate.
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                    try:
                        prepare_boss_potions(page, log)
                    except Exception as error:
                        if is_connection_dead_error(error):
                            raise
                        log(f"  Erro ao preparar as pocoes dos chefes: {error}")
                    try:
                        click_open_wave(page, open_selector)
                        page.click(boss_menu_selector, timeout=3000)
                        ready_class = page.eval_on_selector(ready_selector, "el => el.className") or ""
                        if "on" not in ready_class.split():
                            page.click(ready_selector, timeout=3000)
                        time.sleep(0.3)
                    except Exception as error:
                        log(f"  Erro ao reabrir a lista de Chefes apos as pocoes: {error}")
                        return False
            if sequence_started is None:
                sequence_started = time.monotonic()
            fought = fight_one_boss(page, stop_event, log, target_name, row_selector, name_selector, go_selector)
            if not fought:
                restore_boss_amulet(page, log)
                if fought_any:
                    read_bosstiary_kills(page, log)
                return False
            fought_any = True
            fights += 1

            # entre um chefe e outro, da uma chance pras rotinas de proxima
            # prioridade (tasks de guild, depois vender/entregar) - sem isso,
            # uma sequencia longa de chefes prontos travava TUDO o resto ate
            # esgotar a lista inteira (task de guild ja aceita ficava esperando
            # sem ninguem ir ate ela, Loot Pouch enchendo etc).
            run_between_fights_routines(page, stop_event, log, all_routines)

            continue  # pode ter mais chefes prontos - checa de novo na hora, sem esperar o proximo tick

        break

    if target_name is None:
        if fought_any and sequence_started is not None:
            # sequencia COMPLETA (nao interrompida): grava quanto tempo levou -
            # base da sugestao de quantas pocoes cobrem a lista inteira.
            record_boss_run(time.monotonic() - sequence_started, fights)
            log(f"  Sequencia de chefes concluida em {(time.monotonic() - sequence_started) / 60:.1f}min ({fights} chefe(s)).")
        # (o amuleto Stone Skin volta ao original mais abaixo, DEPOIS de ler os
        # cooldowns e fechar a lista de chefes - aberta por cima, ela
        # interceptava o clique do Helper e o amuleto nunca voltava.)

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
            if not amulet_blocked:
                log(f"  Nenhum chefe marcado esta pronto - proxima consulta em ~{wait_seconds / 3600:.1f}h.")
            # guarda o horario real (sem a margem) pra GUI exibir igual ao jogo -
            # a margem abaixo e so pra CONSULTAR um pouco antes, por seguranca,
            # nao deve aparecer pro usuario como se fosse o tempo real restante.
            BOSS_MEMORY["display_next_check"] = time.monotonic() + wait_seconds
            wait_seconds = max(wait_seconds - BOSS_BACKOFF_MARGIN_SECONDS, 0)
            BOSS_MEMORY["missed_estimate"] = had_real_estimate

        BOSS_MEMORY["next_check"] = time.monotonic() + wait_seconds
        if amulet_blocked:
            # chefes Stone Skin ficaram de fora por falta do amuleto confirmado:
            # nao espera o cooldown longo - tenta de novo logo.
            BOSS_MEMORY["next_check"] = time.monotonic() + BOSS_AMULET_RETRY_SECONDS
            BOSS_MEMORY["display_next_check"] = BOSS_MEMORY["next_check"]
            BOSS_MEMORY["missed_estimate"] = False
            log(f"  Chefes Stone Skin pulados (amuleto nao confirmado) - nova consulta em {BOSS_AMULET_RETRY_SECONDS // 60}min.")
        recover(page, log)
        # sequencia de chefes acabou (nenhum pronto restante): volta o amuleto
        # que foi trocado pro Stone Skin, uma unica vez pra sequencia inteira.
        restore_boss_amulet(page, log)
        if fought_any:
            # atualiza o placar (Bosstiary) UMA VEZ so, depois de todos os
            # combates dessa chamada - nao a cada chefe (evita reabrir o
            # painel pesado do Bosstiary varias vezes seguidas). So agora,
            # com o painel de Chefes ja fechado (recover acima), porque
            # abrir o Bosstiary navega pra outra tela e fecharia ele mesmo.
            read_bosstiary_kills(page, log)
            if not task_grinding():
                # chefes sao a interrupcao de maior prioridade - ao
                # terminar TODOS os prontos, volta pra hunt padrao (a
                # 'base') na hora, sem esperar o proximo tick de
                # 'ensure_active_hunt'. force=True: chefes tem prioridade
                # sobre o avanco automatico tambem.
                return_to_default_hunt(page, log, force=True)
        return True

    restore_boss_amulet(page, log)
    if fought_any:
        read_bosstiary_kills(page, log)
        if not task_grinding():
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
    personagem certo e pular a troca (o Helper abria mas nao alterava nada).

    BUG CONFIRMADO no codigo do jogo (e corrigido): '#tab-helper' ALTERNA o
    estado 'helperOpen' do jogo (classe 'on' no proprio '#tab-helper'), e o
    modal fica escondido tambem quando o seletor de item esta aberto ou a
    lista de personagens ainda ressincroniza (apos um combate de chefe).
    Decidir pela classe 'hidden' do modal e clicar fazia FECHAR um Helper que
    o jogo achava aberto - o clique seguinte batia em elemento 'not visible'
    (erro dos logs, e o Stone Skin nunca era trocado). Ver 'ensure_helper_open'.
    Tenta ate 3 vezes (reabrindo o Helper) antes de desistir."""
    last_error = None
    for _ in range(3):
        try:
            if not ensure_helper_open(page):
                raise RuntimeError("o Helper nao ficou visivel")
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
            if is_connection_dead_error(error):
                raise
            last_error = error
            time.sleep(0.5)
    log(f"  Erro ao abrir Helper ({char_label}/{preset_label}): {last_error}")
    return False


def helper_state(page):
    """{'on': o jogo considera o Helper aberto (classe 'on' em '#tab-helper'),
    'visible': o modal esta de fato na tela}. Os dois podem divergir (seletor
    de item aberto por cima, ressincronizacao dos personagens)."""
    return page.evaluate(
        """() => {
            const modal = document.getElementById('helper-modal');
            const tab = document.getElementById('tab-helper');
            return {
                on: !!tab && tab.classList.contains('on'),
                visible: !!modal && !modal.classList.contains('hidden') && modal.getClientRects().length > 0,
            };
        }"""
    )


def wait_helper_visible(page, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if helper_state(page)["visible"]:
            return True
        time.sleep(0.25)
    return helper_state(page)["visible"]


def ensure_helper_open(page):
    """Deixa o modal do Helper VISIVEL e parado (True) ou desiste (False).
    '#tab-helper' alterna, entao so' clica quando o jogo considera o Helper
    FECHADO; se considera aberto mas o modal esta escondido, espera ele
    voltar (ressincronizacao) e, se nao voltar, Escape (fecha o seletor de
    item/o proprio Helper) e reavalia."""
    for _ in range(5):
        state = helper_state(page)
        if state["visible"]:
            time.sleep(0.4)  # animacao de abertura - o clique exige elemento parado
            if helper_state(page)["visible"]:
                return True
            continue
        if state["on"]:
            if wait_helper_visible(page, 2.5):
                continue
            page.keyboard.press("Escape")
            time.sleep(0.5)
            continue
        page.click("#tab-helper", timeout=3000)
        wait_helper_visible(page, 2.5)
    return False


def close_helper(page):
    """Fecha o Helper e confirma (o jogo guarda 'helperOpen' - um Helper
    esquecido aberto trava o clique de todo o resto)."""
    for _ in range(3):
        page.keyboard.press("Escape")
        time.sleep(0.4)
        try:
            state = helper_state(page)
        except Exception:
            return
        if not state["on"] and not state["visible"]:
            return


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
        # o Helper ja fechou sozinho depois do 1o item trocado (logs reais) -
        # reabre/reposiciona antes de clicar no slot, em vez de bater num
        # elemento 'not visible'.
        if not helper_state(page)["visible"] and not open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
            return False
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
        # CONFIRMADO ao vivo (v4.19.2): cada linha tem 2 botoes - 'eq-fav' ("Favoritar",
        # a estrela) PRIMEIRO e 'Usar' depois. O seletor antigo ('...row button') clicava
        # na estrela: so' favoritava e o amuleto nunca era trocado (e o log dizia "trocado").
        # Clica no 'Usar' da linha cujo NOME e' exatamente o item pedido.
        safe_name = item_name.replace('"', '\\"')
        use = page.locator(
            f'.sp-list.sp-book-list .sp-book-row:has(.sp-book-name:text-is("{safe_name}")) button:has-text("Usar")')
        if use.count() == 0:
            page.keyboard.press("Escape")
            return False
        use.first.click(timeout=3000)
        time.sleep(0.3)
        wait_helper_visible(page, 3)  # o seletor esconde o Helper; volta ao escolher
        # confere que o slot MUDOU de verdade (nao confia so' no clique) - o painel leva um
        # instante pra redesenhar (CONFIRMADO: lendo na hora dava o nome antigo mesmo com a
        # troca feita), entao espera ate' 3s o nome novo aparecer.
        wanted = item_name.strip().casefold()
        current = ""
        for _ in range(10):
            current = (read_helper_amulet(page, field_cls) or "").strip().casefold()
            if current == wanted:
                break
            time.sleep(0.3)
        if current != wanted:
            log(f"  O slot '{field_cls}' continua com '{current}' apos escolher '{item_name}'.")
            return False
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
    True so' se os 2 de fato ficaram no valor pedido (pra quem chama nao logar
    'ajustada'/'revertida' quando na verdade falhou).

    CONFIRMADO ao vivo (v4.19.2): mudar o 1o select faz o Helper REDESENHAR e a
    referencia guardada do 2o ficava velha ('Element is not attached to the DOM').
    Por isso localiza cada select de novo na hora (locator), pula o que ja esta no
    valor pedido e confere o resultado."""
    labels = ("Equipar com vida abaixo de", "Restaurar com vida acima de")
    targets = (str(equip_pct), str(restore_pct))
    ok = True
    for index, (label, target) in enumerate(zip(labels, targets)):
        applied = False
        for attempt in range(2):
            try:
                selects = page.locator(".helper-equipcard .helper-sel")
                if selects.count() < 2:
                    log("  Nao achei os campos de % de ativacao do amuleto.")
                    return False
                if selects.nth(index).input_value() == target:
                    applied = True
                    break
                selects.nth(index).select_option(target, timeout=3000)
                time.sleep(0.5)   # o Helper redesenha depois da mudanca
                if selects.nth(index).input_value() == target:
                    applied = True
                    break
            except Exception as error:
                if attempt == 1:
                    log(f"  Erro ao ajustar '{label}' pra {target}%: {error}")
                time.sleep(0.5)
        ok = ok and applied
    return ok


def equip_boss_amulet(page, log):
    """Antes de enfrentar um chefe marcado 'stone_skin' no BossPicker, troca
    o amuleto Emergencial e Padrao do EK (preset Boss, no Helper) pro Stone
    Skin Amulet, pra aguentar mais dano, e deixa os 2 gatilhos de % de vida
    (ver BOSS_AMULET_EQUIP_PCT/RESTORE_PCT) mais agressivos. So' troca o item
    que de fato achar na pouch/mochila.

    Retorna (changed, verified):
    - changed: {'items': {'emer'/'padr': nome_original}, 'thresholds':
      (equip_pct, restore_pct) originais ou None} - o que 'revert_boss_amulet'
      precisa pra desfazer; {} se nem abriu o Helper.
    - verified: True SO' se, relendo o Helper depois da troca, pelo menos um
      dos 2 slots mostra o Stone Skin (a pouch costuma ter 1 so' - o 2o slot
      pode legitimamente ficar de fora). E' o que libera o combate: sem isso
      o chefe 'stone_skin' nao e' enfrentado (ver o passo de chefes).
    Tenta ate 3 vezes (o Helper ja fechou sozinho no meio da troca em logs
    reais)."""
    items = {}
    thresholds = None
    verified = False
    for attempt in range(1, 4):
        try:
            if not open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
                continue
            for field_cls in ("emer", "padr"):
                original = read_helper_amulet(page, field_cls)
                if original == BOSS_AMULET_ITEM:
                    continue  # ja esta com o item certo - nada a trocar/lembrar
                if set_helper_amulet(page, field_cls, BOSS_AMULET_ITEM, log):
                    items.setdefault(field_cls, original)
                    log(f"  Amuleto {field_cls} do {BOSS_AMULET_CHAR} trocado pra '{BOSS_AMULET_ITEM}' (era '{original}').")

            if thresholds is None and open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
                orig_equip_pct, orig_restore_pct = read_helper_amulet_thresholds(page)
                if orig_equip_pct is not None:
                    thresholds = (orig_equip_pct, orig_restore_pct)
                    if set_helper_amulet_thresholds(page, BOSS_AMULET_EQUIP_PCT, BOSS_AMULET_RESTORE_PCT, log):
                        log(f"  % do amuleto do {BOSS_AMULET_CHAR} ajustada pra {BOSS_AMULET_EQUIP_PCT}%/{BOSS_AMULET_RESTORE_PCT}% (era {orig_equip_pct}%/{orig_restore_pct}%).")

            # confere de verdade: FECHA e reabre o Helper (o jogo so' redesenha o
            # painel ao abrir - o DOM antigo guarda o nome velho mesmo fechado)
            # e RELE os 2 slots.
            close_helper(page)
            if open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
                slots = {field_cls: read_helper_amulet(page, field_cls) for field_cls in ("emer", "padr")}
                if BOSS_AMULET_ITEM in slots.values():
                    verified = True
                    break
                log(f"  Stone Skin nao apareceu nos slots do amuleto (emer='{slots['emer']}', padr='{slots['padr']}') - tentativa {attempt}/3.")
        except Exception as error:
            if is_connection_dead_error(error):
                raise
            log(f"  Erro na troca do amuleto (tentativa {attempt}/3): {error}")
        # recomeca limpo: fecha o que estiver aberto (seletor de item, Helper)
        try:
            close_helper(page)
        except Exception as error:
            if is_connection_dead_error(error):
                raise

    try:
        close_helper(page)
    except Exception as error:
        if is_connection_dead_error(error):
            raise
    changed = {"items": items, "thresholds": thresholds} if (items or thresholds) else {}
    return changed, verified


def revert_boss_amulet(page, log, changed):
    """Desfaz a troca feita por 'equip_boss_amulet' - volta cada slot de item
    que foi de fato alterado pro que estava antes, e as 2 % de ativacao pro
    que estavam antes tambem. Retorna True se tudo voltou (ou ja estava
    como antes); False se algo ficou pra tras."""
    if not changed:
        return True
    ok = True
    try:
        if not open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
            return False
        for field_cls, name in (changed.get("items") or {}).items():
            if not name:
                continue
            if read_helper_amulet(page, field_cls) == name:
                continue  # ja voltou (tentativa anterior)
            if set_helper_amulet(page, field_cls, name, log):
                log(f"  Amuleto {field_cls} do {BOSS_AMULET_CHAR} revertido pra '{name}'.")
            else:
                ok = False
        thresholds = changed.get("thresholds")
        if thresholds is not None:
            if not open_helper_equip_amulet(page, BOSS_AMULET_CHAR, "Boss", log):
                return False
            if set_helper_amulet_thresholds(page, thresholds[0], thresholds[1], log):
                log(f"  % do amuleto do {BOSS_AMULET_CHAR} revertida pra {thresholds[0]}%/{thresholds[1]}%.")
            else:
                ok = False
    finally:
        try:
            close_helper(page)
        except Exception as error:
            if is_connection_dead_error(error):
                raise
    return ok


def merge_amulet_changed(old, new):
    """Junta o que 'equip_boss_amulet' guardou em tentativas diferentes da
    mesma sequencia - o valor ORIGINAL (o mais antigo) sempre vence, senao a
    2a tentativa gravaria o Stone Skin / 85-90% como se fossem o original."""
    if not old:
        return new
    if not new:
        return old
    return {
        "items": {**(new.get("items") or {}), **(old.get("items") or {})},
        "thresholds": old.get("thresholds") or new.get("thresholds"),
    }


def restore_boss_amulet(page, log):
    """Devolve o amuleto do EK ao que era antes do Stone Skin. A lista de
    chefes ('#boss-modal') TEM que estar fechada (CONFIRMADO nos logs: aberta
    por cima, o clique do Helper era interceptado e o amuleto nunca voltava).
    Se nao conseguir, mantem a lembranca do original e tenta de novo no fim
    da proxima sequencia (desiste na 3a falha, avisando)."""
    changed = BOSS_AMULET_MEMORY["changed"]
    BOSS_AMULET_MEMORY["verified"] = False  # o que o Helper tem agora e' incerto ate reler
    if not changed:
        BOSS_AMULET_MEMORY["revert_fails"] = 0
        return
    page.keyboard.press("Escape")
    time.sleep(0.4)
    if revert_boss_amulet(page, log, changed):
        BOSS_AMULET_MEMORY["changed"] = {}
        BOSS_AMULET_MEMORY["revert_fails"] = 0
        return
    BOSS_AMULET_MEMORY["revert_fails"] += 1
    if BOSS_AMULET_MEMORY["revert_fails"] >= 3:
        log(f"  ATENCAO: nao consegui devolver o amuleto original do {BOSS_AMULET_CHAR} (era {changed.get('items')}). Confira o Helper manualmente.")
        BOSS_AMULET_MEMORY["changed"] = {}
        BOSS_AMULET_MEMORY["revert_fails"] = 0
    else:
        log(f"  Nao consegui devolver o amuleto do {BOSS_AMULET_CHAR} agora - tento de novo no fim da proxima sequencia.")


def task_grinding():
    """True enquanto o bot caça a hunt de uma task da guild OU de uma missao do
    passe - nesse tempo nada de avanco de hunt, campanha, reload da pagina etc.
    (mesma prioridade das tasks da guild)."""
    return bool(GUILD_TASK_MEMORY.get("grinding") or PASSE_MEMORY.get("grinding"))


def log_once(key, message, log):
    """Loga 'message' so na primeira vez que 'key' aparece (evita repetir o
    mesmo aviso a cada rodada de uma rotina)."""
    if key in PASSE_MEMORY["logged"]:
        return
    PASSE_MEMORY["logged"].add(key)
    log(message)


def read_active_char_level(page, log):
    """Nivel do personagem ATIVO (o marcado na barra de personagens), lido da
    party que ja fica na tela. A vocacao vem do titulo do botao do
    personagem ('Nome (Druid) · clique p/ configurar...'). None se nao deu."""
    try:
        title = page.eval_on_selector(".bar-char.active", "el => el.title || ''") or ""
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return None
    match = re.search(r"\(([^)]+)\)", title)
    vocation = detect_vocation(match.group(1) if match else title)
    if not vocation:
        return None
    return read_party_levels(page, log).get(vocation)


def get_hunt_levels(page, log):
    """{nome da hunt: nivel recomendado} da lista de Hunts do jogo. So relê
    (abre/fecha o menu de Teleportes) a cada HUNT_LEVELS_MAX_AGE_SECONDS -
    nivel de hunt nao muda."""
    now = time.monotonic()
    if not HUNT_LEVELS_MEMORY["levels"] or now - HUNT_LEVELS_MEMORY["loaded_at"] >= HUNT_LEVELS_MAX_AGE_SECONDS:
        rows = read_hunt_list(page, log)
        if rows:
            HUNT_LEVELS_MEMORY["levels"] = {r["name"]: r["level"] for r in rows if r.get("level") is not None}
        HUNT_LEVELS_MEMORY["loaded_at"] = now
    return HUNT_LEVELS_MEMORY["levels"]


# ---------- Passe de Temporada ----------

# Estado do modal do Passe: 'active' (missao em curso: nome + botoes) ou
# 'choose' (lista "Missoes de hoje": nome, etiqueta de dificuldade, se esta
# bloqueada pelo limite do dia). Nao compara textos com acento de proposito.
PASS_STATE_JS = """() => {
    const m = document.querySelector('#bp-modal');
    if (!m || m.classList.contains('hidden')) return null;
    const bands = ['bp-band-easy', 'bp-band-medium', 'bp-band-hard'];
    const act = m.querySelector('.bp-active-row');
    if (act) {
        return {
            mode: 'active',
            name: ((act.querySelector('.bp-mini-nm') || {}).textContent || '').trim(),
            buttons: Array.from(act.querySelectorAll('.bp-actions button')).map(b => ({
                text: (b.textContent || '').trim(), disabled: !!b.disabled})),
        };
    }
    return {
        mode: 'choose',
        missions: Array.from(m.querySelectorAll('.bp-missions-line .bp-mini')).map(b => ({
            name: ((b.querySelector('.bp-mini-nm') || {}).textContent || '').trim(),
            band: bands.find(c => b.querySelector('.' + c)) || '',
            locked: b.classList.contains('locked') || !!b.disabled})),
    };
}"""


def read_pass_tracker(page):
    """Contador 'Passe' da tela principal (overlay '#bptrack-overlay', ex:
    '13/600'): (atual, meta, pronto) ou None se nao esta na tela. 'pronto' =
    o texto ficou verde (classe 'ok') ou atual >= meta."""
    try:
        data = page.evaluate(
            """() => {
                const el = document.querySelector('#bptrack-overlay .bpk-num');
                return el ? {text: el.textContent || '', ok: el.classList.contains('ok')} : null;
            }"""
        )
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return None
    if not data:
        return None
    match = re.match(r"\s*([\d.]+)\s*/\s*([\d.]+)", data["text"])
    if not match:
        return None
    current, goal = (int(g.replace(".", "")) for g in match.groups())
    return current, goal, bool(data["ok"] or (goal > 0 and current >= goal))


def open_pass_modal(page):
    """Abre o modal do Passe pela aba '#tab-battlepass' (a aba alterna
    aberto/fechado, entao so clica se ainda nao estiver aberto). True se o
    modal ficou visivel."""
    if page.is_visible("#bp-modal .bp-strip"):
        return True
    try:
        try:
            page.click("#tab-battlepass", timeout=3000)
        except Exception:
            page.click("#tab-battlepass", timeout=3000, force=True)  # janelas do HUD podem cobrir a aba
        page.wait_for_selector("#bp-modal .bp-strip", state="visible", timeout=4000)
        return True
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return False


def close_pass_modal(page):
    try:
        if page.is_visible("#bp-modal-close"):
            page.click("#bp-modal-close", timeout=2000)
    except Exception as error:
        if is_connection_dead_error(error):
            raise


def click_pass_deliver(page, log):
    """Com o modal do Passe aberto: clica 'Entregar' (so existe habilitado
    quando a meta foi batida; antes disso o botao dourado diz 'Faltam N') e
    espera a faixa virar a lista de missoes. True se entregou."""
    button = page.locator("#bp-modal .bp-actions button.bp-btn-gold", has_text="Entregar")
    try:
        if button.count() == 0 or not button.first.is_enabled():
            return False
        button.first.click(timeout=3000)
        page.wait_for_selector("#bp-modal .bp-missions-line", timeout=5000)
        return True
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        log(f"  Erro ao entregar a missao do passe: {error}")
        return False


def claim_pass_rewards(page, log):
    """Com o Passe aberto: retira os premios de degrau que estiverem esperando
    ('Retirar tudo · N' quando existe; senao 'Retirar' do degrau selecionado -
    ao ver o botao ele so existe com premio pronto). O grátis cai na hora
    (ex: boost de XP) e o do Premium vai pra Caixa de Entrada. Se aparecer
    uma confirmacao inesperada, cancela e avisa em vez de aceitar no escuro.
    True se clicou em algum."""
    for selector in ("#bp-modal .bp-map-all", "#bp-modal .bp-map-take"):
        button = page.locator(selector)
        try:
            if button.count() == 0 or not button.first.is_visible() or not button.first.is_enabled():
                continue
            label = (button.first.text_content() or "").strip()
            button.first.click(timeout=3000)
            page.wait_for_timeout(800)
            if page.is_visible("#confirm-modal .im-card"):
                log(f"  O jogo pediu confirmacao ao '{label}' no passe - cancelei, retire manualmente.")
                cancel_pass_confirm(page)
                return False
        except Exception as error:
            if is_connection_dead_error(error):
                raise
            log(f"  Erro ao retirar o premio do passe: {error}")
            return False
        log(f"  Premio do passe retirado ('{label}').")
        record_activity("Passe: premio de degrau retirado.")
        return True
    return False


def pass_delivered(name, log):
    log(f"  Missao do passe '{name}' entregue!")
    play_achievement_sound()
    record_activity(f"Missao do passe completa: '{name}' - entregue com sucesso.")
    FORCE_RUN_NOW.add("missoes_passe")  # escolhe a proxima ja na proxima volta do loop


def deliver_pass_if_ready(page, log):
    """Entrega sozinho a missao do Passe quando o contador dela na tela bate a
    meta (texto verde / atual >= meta). Roda a cada tick do loop principal -
    so le o overlay (barato) e so abre o Passe quando ha o que entregar. Vale
    pra qualquer missao em curso, tenha ela sido escolhida pelo bot ou nao."""
    tracker = read_pass_tracker(page)
    if tracker is None or not tracker[2]:
        return False
    if time.monotonic() < PASSE_MEMORY["deliver_retry_at"]:
        return False
    delivered = False
    if open_pass_modal(page):
        state = page.evaluate(PASS_STATE_JS)
        if state and state.get("mode") == "active":
            name = state.get("name") or "?"
            delivered = click_pass_deliver(page, log)
            if delivered:
                pass_delivered(name, log)
                claim_pass_rewards(page, log)  # a entrega pode ter completado um degrau
    close_pass_modal(page)
    if not delivered:
        PASSE_MEMORY["deliver_retry_at"] = time.monotonic() + PASSE_DELIVER_RETRY_SECONDS
    return delivered


def cancel_pass_confirm(page):
    try:
        page.click("#confirm-no", timeout=2000)
        page.wait_for_selector("#confirm-modal .bp-detail", state="detached", timeout=2000)
    except Exception as error:
        if is_connection_dead_error(error):
            raise


def choose_pass_mission(page, state, order, labels, log):
    """Modal do Passe na lista 'Missoes de hoje': percorre as missoes liberadas
    (nao bloqueadas pelo limite do dia) das dificuldades marcadas, na ordem
    de prioridade, abre o detalhe de cada uma, le o nivel da hunt ('nivel N') e
    escolhe a primeira em que o nosso nivel e >= o da hunt - SEMPRE a versao
    Normal (a Endemoniada custa gold). True se escolheu alguma, False se nao
    ha nenhuma elegivel, None se nao deu pra decidir (nivel nao lido)."""
    char_level = read_active_char_level(page, log)
    if char_level is None:
        log_once("passe-sem-nivel", "  Nao consegui ler o nivel do personagem - nao escolho missao do passe agora.", log)
        return None  # nao decidiu (diferente de False = nenhuma elegivel)

    candidates = sorted(
        (m for m in state["missions"] if not m["locked"] and m["band"] in order),
        key=lambda m: order[m["band"]],
    )
    for mission in candidates:
        name = mission["name"]
        safe_name = name.replace('"', '\\"')
        label = labels[mission["band"]]
        try:
            page.locator(f'#bp-modal .bp-missions-line .bp-mini:not(.locked):has(.bp-mini-nm:text-is("{safe_name}"))').first.click(timeout=3000)
            page.wait_for_selector("#confirm-modal .bp-detail", state="visible", timeout=3000)
            detail = page.eval_on_selector("#confirm-modal .bp-detail-l .muted", "el => el.textContent || ''") or ""
        except Exception as error:
            if is_connection_dead_error(error):
                raise
            log(f"  Erro ao abrir a missao do passe '{name}': {error}")
            cancel_pass_confirm(page)
            continue

        match = re.search(r"n[ií]vel\s*(\d+)", detail, re.IGNORECASE)
        hunt_level = int(match.group(1)) if match else None
        if hunt_level is None:
            log_once(f"passe-sem-nivel-{name}", f"  Missao do passe '{name}' ({label}): nao achei o nivel da hunt - pulada.", log)
            cancel_pass_confirm(page)
            continue
        if hunt_level > char_level:
            log_once(
                f"passe-alto-{name}-{hunt_level}-{char_level}",
                f"  Missao do passe '{name}' ({label}) pulada: hunt lvl {hunt_level} > nosso lvl {char_level}.",
                log,
            )
            cancel_pass_confirm(page)
            continue

        try:
            # garante 'Normal' marcado (e nunca confirma com a Endemoniada, que custa gold)
            if page.eval_on_selector("#confirm-modal .bp-dif-c.on", "el => el.classList.contains('dem')"):
                page.locator("#confirm-modal .bp-dif-c:not(.dem)").first.click(timeout=3000)
            if page.eval_on_selector("#confirm-modal .bp-dif-c.on", "el => el.classList.contains('dem')"):
                log(f"  Missao do passe '{name}': nao consegui marcar a versao Normal - cancelada.")
                cancel_pass_confirm(page)
                continue
            page.click("#confirm-yes", timeout=3000)
            page.wait_for_selector("#bp-modal .bp-active-row", timeout=5000)
        except Exception as error:
            if is_connection_dead_error(error):
                raise
            log(f"  Erro ao escolher a missao do passe '{name}': {error}")
            cancel_pass_confirm(page)
            continue
        log(f"  Missao do passe '{name}' ({label}, hunt lvl {hunt_level}) escolhida.")
        PASSE_MEMORY["logged"].clear()
        return True
    if candidates:
        log_once(
            f"passe-nenhuma-{char_level}",
            f"  Nenhuma missao do passe elegivel agora (lvl {char_level}) - escolha manualmente se quiser.",
            log,
        )
    return False


def go_to_pass_hunt(page, name, state, log):
    """Missao em curso: garante o rastreio na tela e leva o personagem pra hunt
    dela com 'Ir pra caçada' (que abandona a hunt atual - ok fora de chefe).
    Marca 'grinding' quando ja esta na hunt da missao."""
    for button in state["buttons"]:
        if "acompanhar" in button["text"].lower() and not button["disabled"]:
            try:
                page.locator("#bp-modal .bp-actions button", has_text="Acompanhar na tela").first.click(timeout=3000)
            except Exception as error:
                if is_connection_dead_error(error):
                    raise
            break

    try:
        current_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        return
    if current_hunt == name:
        PASSE_MEMORY["go_fails"] = 0
        PASSE_MEMORY["grinding"] = True
        PASSE_MEMORY["grinding_since"] = time.monotonic()  # heartbeat (ve ensure_active_hunt)
        if PASSE_MEMORY["previous_hunt"] is None:
            PASSE_MEMORY["previous_hunt"] = GUILD_TASK_MEMORY.get("previous_hunt")
        return

    go_button = next((b for b in state["buttons"] if "ir pra" in b["text"].lower()), None)
    if go_button is None or go_button["disabled"]:
        log_once(f"passe-sem-ir-{name}", f"  Missao do passe '{name}': botao 'Ir pra caçada' indisponivel.", log)
        return
    if PASSE_MEMORY["go_fails"] >= PASSE_MAX_GO_FAILS:
        log_once(f"passe-ir-falhou-{name}", f"  Nao consegui levar o personagem pra hunt '{name}' (missao do passe) - desisti, va manualmente.", log)
        return

    if PASSE_MEMORY["previous_hunt"] is None:
        PASSE_MEMORY["previous_hunt"] = GUILD_TASK_MEMORY.get("previous_hunt") or current_hunt or None
    try:
        page.locator("#bp-modal .bp-actions button", has_text="Ir pra").first.click(timeout=3000)
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        PASSE_MEMORY["go_fails"] += 1
        log(f"  Erro ao clicar em 'Ir pra caçada' ({name}): {error}")
        return
    log(f"  Indo pra hunt '{name}' (missao do passe)...")
    for _ in range(10):
        time.sleep(0.5)
        try:
            now_hunt = (page.eval_on_selector("#wave-title", "el => el.textContent") or "").strip()
        except Exception as error:
            if is_connection_dead_error(error):
                raise
            now_hunt = ""
        if now_hunt == name:
            PASSE_MEMORY["go_fails"] = 0
            PASSE_MEMORY["grinding"] = True
            PASSE_MEMORY["grinding_since"] = time.monotonic()
            return
    PASSE_MEMORY["go_fails"] += 1
    log(f"  'Ir pra caçada' nao levou pra '{name}' ({PASSE_MEMORY['go_fails']}/{PASSE_MAX_GO_FAILS}).")


def finish_pass_grinding(page, log):
    """Acabaram as missoes elegiveis: volta pra hunt base (campanha > padrao >
    de onde saiu) e libera o 'grinding'. Pede pra rotina da guild rodar ja -
    ordem: chefes > passe > tasks da guild."""
    DEFAULT_HUNT_MEMORY["name"] = load_settings().get("default_hunt", "")
    target = CAMPAIGN_MEMORY.get("target_hunt") or DEFAULT_HUNT_MEMORY.get("name") or PASSE_MEMORY["previous_hunt"]
    ok = True
    if target:
        log(f"  Missoes do passe concluidas - voltando para '{target}'...")
        ok = find_and_go_to_hunt(
            page, target, "#wave-title", '.tp-opt[data-tp="hunts"]', ".stage-row", ".stage-name-line b", ".stage-go", log
        )
    if ok:
        PASSE_MEMORY["previous_hunt"] = None
        PASSE_MEMORY["grinding"] = False
        PASSE_MEMORY["grinding_since"] = None
        PASSE_MEMORY["go_fails"] = 0
        FORCE_RUN_NOW.add("tarefas_guild")


def execute_dom_battlepass_step(page, step, log):
    """Passo tipo 'dom_battlepass' (Missoes do Passe): com o Passe aberto,
    entrega a missao pronta, escolhe a proxima (so Normal, nivel da hunt <= o
    nosso, dificuldade marcada - ve choose_pass_mission) e leva o personagem
    pra hunt dela. Quando nao sobra missao elegivel, volta pra hunt base.
    Sem dificuldade marcada nao faz nada (a entrega automatica independe disso)."""
    enabled = [d for d in step["difficulties"] if d.get("enabled")]
    if not enabled:
        return True
    # da DIFICIL pra facil (mais pontos por missao - o passe tem limite de
    # degrau por dia); a regra de nivel faz cair pra menor quando a difícil
    # e alta demais. (Na guild continua da facil pra dificil.)
    order = {d["card_class"]: i for i, d in enumerate(reversed(enabled))}
    labels = {d["card_class"]: d.get("label", d["card_class"]) for d in step["difficulties"]}

    if not open_pass_modal(page):
        log("  Nao consegui abrir o Passe.")
        return True

    finished = False
    try:
        claim_pass_rewards(page, log)  # premios de degrau que ficaram esperando
        for _ in range(4):  # entregar -> escolher -> ir pra hunt, no maximo
            state = page.evaluate(PASS_STATE_JS)
            if not state:
                break
            if state["mode"] == "active":
                deliver = next((b for b in state["buttons"] if "entregar" in b["text"].lower() and not b["disabled"]), None)
                if deliver is not None:
                    if click_pass_deliver(page, log):
                        pass_delivered(state["name"] or "?", log)
                        claim_pass_rewards(page, log)
                        continue
                    break
                go_to_pass_hunt(page, state["name"], state, log)
                break
            chose = choose_pass_mission(page, state, order, labels, log)
            if chose is None:
                break
            if not chose:
                finished = True
                break
    finally:
        close_pass_modal(page)

    if finished and PASSE_MEMORY.get("grinding"):
        finish_pass_grinding(page, log)
    return True


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

    # regra de nivel: so aceita/caça a task cuja hunt tem nivel <= o nosso (nivel
    # nao lido, ou hunt com nome diferente do da task = sem trava, como antes).
    # Lido ANTES de abrir a guild - o overlay dela cobre o menu de Teleportes.
    char_level = read_active_char_level(page, log)
    hunt_levels = get_hunt_levels(page, log) if char_level is not None else {}

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

        task_hunt_level = hunt_levels.get(task_name)
        if char_level is not None and task_hunt_level is not None and task_hunt_level > char_level:
            log_once(
                f"guild-alto-{task_name}-{task_hunt_level}-{char_level}",
                f"  Task da guild '{task_name}' ({diff_label}) pulada: hunt lvl {task_hunt_level} > nosso lvl {char_level}.",
                log,
            )
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

    if PASSE_MEMORY.get("grinding"):
        pass  # missao do passe em andamento vem antes (chefes > passe > guild) - nao troca nem devolve a hunt agora
    elif pending_task is not None:
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
        target_hunt = (
            CAMPAIGN_MEMORY.get("target_hunt") or DEFAULT_HUNT_MEMORY.get("name") or GUILD_TASK_MEMORY["previous_hunt"]
        )
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

    # re-consulta a cada clique: o jogo redesenha o quadro ao desligar um
    # rastreio (referencias antigas das outras linhas ficam 'detached').
    # 'tried' evita insistir na mesma criatura se o clique nao a remover.
    tried = set()
    for _ in range(10):
        target = None
        for row in page.query_selector_all(row_selector):
            count_el = row.query_selector(count_selector)
            if count_el is None or "ok" not in (count_el.get_attribute("class") or "").split():
                continue
            name_el = row.query_selector(name_selector)
            name = (name_el.text_content() or "").strip() if name_el else "?"
            if name in tried:
                continue
            target = (row, name)
            break
        if target is None:
            return
        row, name = target
        tried.add(name)
        try:
            row.click(timeout=2000)
        except Exception as error:
            # CONFIRMADO nos logs: o painel do grupo ('#panel-party', HUD livre)
            # pode ficar POR CIMA do quadro de rastreio e interceptar o clique
            # real. O botao so' escuta 'click' - dispara direto nele.
            try:
                row.dispatch_event("click")
            except Exception as fallback_error:
                log(f"  Erro ao finalizar rastreio de '{name}' no Bestiary: {error} / {fallback_error}")
                continue
        log(f"  Bestiary de '{name}' completo - rastreio finalizado (vaga liberada).")
        time.sleep(0.3)


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
        if task_grinding():
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

    if CAMPAIGN_MEMORY.get("target_hunt"):
        return True  # a Campanha de Codex decide pra qual hunt ir - sem avanco automatico competindo

    if task_grinding():
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
    try:
        site_page = context.new_page()
    except Exception as error:
        if is_connection_dead_error(error):
            raise
        # CONFIRMADO ao vivo: o IdleDeck (Electron) nao suporta abrir aba extra
        # ('Target.createTarget: Not supported'). Consulta o site num Chrome
        # headless a parte, sem tocar no IdleDeck nem nos outros jogos dele.
        return fetch_build_code_headless(vocation, level, config, log)
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


def fetch_build_code_headless(vocation, level, config, log):
    """Mesma consulta de 'fetch_build_code', mas num Chrome INSTALADO em modo
    headless (sem janela), numa thread propria (cada thread precisa do seu
    proprio Playwright). Usado quando o navegador do jogo nao deixa abrir uma
    aba nova (IdleDeck). Retorna o codigo ou None."""
    result = {}

    def work():
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                try:
                    result["code"] = fetch_build_code(browser.new_context(), vocation, level, config, log)
                finally:
                    browser.close()
        except Exception as error:
            log(f"  Erro ao consultar o otimizador de build em segundo plano: {error}")

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(120)
    return result.get("code")


def bring_game_to_front(page):
    """Traz a aba do jogo pro primeiro plano (o Chrome throttla abas em 2o
    plano). No IdleDeck NAO: ele ja desliga o throttling dos slots e o
    'bringToFront' traria a janela do app pra frente (mesmo escondida na
    bandeja/minimizada) e mexeria no foco dos outros jogos dele."""
    if BROWSER_PROFILES[CURRENT_PROFILE].get("launcher") in ("idledeck", "idledeck_copy"):
        return
    page.bring_to_front()


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
        bring_game_to_front(page)
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
        bring_game_to_front(page)

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
    # a hunt do item atual da Campanha de Codex (se houver) vira a 'base' no
    # lugar da hunt padrao - e o avanco automatico nao compete com ela.
    campaign_hunt = CAMPAIGN_MEMORY.get("target_hunt")
    default_hunt = campaign_hunt or DEFAULT_HUNT_MEMORY.get("name")
    if not default_hunt:
        return False
    if not force and ADVANCE_MEMORY.get("enabled") and not campaign_hunt:
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

    base_label = "da campanha" if campaign_hunt else "padrao"
    if current_hunt:
        log(f"  Na hunt '{current_hunt}', mas a hunt {base_label} e '{default_hunt}' - indo pra ela...")
    else:
        log(f"  Sem hunt ativa - indo para a hunt {base_label} '{default_hunt}'...")
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

    if PASSE_MEMORY.get("grinding"):
        # mesma trava da guild: a rotina do passe renova 'grinding_since' a cada
        # rodada em que a missao segue em andamento.
        grinding_since = PASSE_MEMORY.get("grinding_since")
        if grinding_since is None or time.monotonic() - grinding_since < GUILD_TASK_GRINDING_MAX_SECONDS:
            return  # missao do passe em andamento - ela mesma resolve a hunt
        log(f"  'grinding' de missao do passe preso ha mais de {GUILD_TASK_GRINDING_MAX_SECONDS // 60}min - liberando.")
        PASSE_MEMORY["grinding"] = False
        PASSE_MEMORY["previous_hunt"] = None
        PASSE_MEMORY["grinding_since"] = None

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
    if step_type == "dom_battlepass":
        return execute_dom_battlepass_step(page, step, log)
    if step_type == "dom_codex_campaign":
        return execute_dom_codex_campaign_step(page, step, log)
    if step_type == "dom_potion_stock":
        return execute_dom_potion_stock_step(page, step, log)
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

                    # entrega do Passe: so le o contador da tela (barato) a cada
                    # tick e entrega quando bate a meta - independe da rotina
                    # 'Missoes do Passe' estar ligada.
                    try:
                        deliver_pass_if_ready(page, log)
                    except Exception as error:
                        if is_connection_dead_error(error):
                            raise
                        log(f"Erro ao entregar a missao do passe: {error}")

                    if CODEX_REFRESH_REQUEST["pending"]:
                        CODEX_REFRESH_REQUEST["pending"] = False
                        try:
                            CODEX_REFRESH_REQUEST["result"] = read_codex_campaign_data(page, log)
                        except Exception as error:
                            if is_connection_dead_error(error):
                                raise
                            log(f"Erro ao ler os dados da Campanha de Codex: {error}")
                            CODEX_REFRESH_REQUEST["result"] = None
                        CODEX_REFRESH_REQUEST["done"].set()

                    if BOT_CALL_REQUEST["fn"] is not None:
                        call, BOT_CALL_REQUEST["fn"] = BOT_CALL_REQUEST["fn"], None
                        try:
                            BOT_CALL_REQUEST["result"] = call(page, log)
                        except Exception as error:
                            if is_connection_dead_error(error):
                                raise
                            log(f"Erro ao atender o pedido da interface: {error}")
                            BOT_CALL_REQUEST["result"] = None
                        BOT_CALL_REQUEST["done"].set()

                    # reload periodico pra conter o vazamento de memoria do jogo
                    # (ver PAGE_RELOAD_INTERVAL_SECONDS) - so quando nao ha nada
                    # de prioridade maior em andamento, pro reload nao cortar um
                    # chefe ou uma task de guild pela metade.
                    if now >= next_page_reload:
                        if task_grinding() or TRAINING_MEMORY.get("waiting"):
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
                        if active_seconds is not None and routine["id"] == "missoes_passe" and PASSE_MEMORY.get("grinding"):
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
