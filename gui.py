import datetime
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk

import customtkinter as ctk

import bot
import theme
from boss_picker import BossPicker, _Tooltip
from attribute_picker import AttributePicker
from build_config_picker import BuildConfigPicker
from hunt_picker import HuntPicker
from codex_campaign_picker import CodexCampaignPicker
from potion_picker import PotionPicker

MAX_LOG_LINES = 500
LOG_DIR = os.path.join(bot.data_dir(), "logs")
# Ao abrir o market o Codex da conta e' mapeado pra ele cruzar com os leiloes
# (market/codex.py), mas no maximo 1x a cada tanto: reabrir o painel varias
# vezes nao fica mexendo no Codex do jogo toda hora (o botao 'Atualizar Codex'
# do painel ignora esse intervalo).
CODEX_AUTO_SYNC_MIN_SECONDS = 10 * 60
# Com o BOT RODANDO o mapa do Codex tambem e' refeito sozinho a cada
# CODEX_BOT_SYNC_SECONDS, sem depender de o painel do market estar aberto (antes
# so' atualizava ao abrir o painel e o cruzamento Codex x leiloes ficava velho
# por horas). Se a leitura falhar (ex: Mercado do jogo aberto por cima), tenta
# de novo apos CODEX_BOT_RETRY_SECONDS.
CODEX_BOT_SYNC_SECONDS = 30 * 60
CODEX_BOT_RETRY_SECONDS = 5 * 60

# painel de mercado (market/server.py) roda junto do bot, na mesma janela -
# antes eram 2 processos separados que o usuario tinha que abrir na mao cada
# um. A pasta 'market' (com o market.db de verdade) so existe na arvore de
# codigo-fonte, nunca dentro de 'dist' - o .exe compilado roda de la, entao
# procura 'market' primeiro do lado do proprio script/exe (modo dev) e, se
# nao achar, um nivel acima (.exe em 'dist', 'market' irmao de 'dist') - sem
# isso, cada modo (dev/.exe) acabaria enxergando (ou criando) uma copia
# diferente do banco, duplicando/perdendo dados.
MARKET_DIR = os.path.join(bot.resource_dir(), "market")
if not os.path.isdir(MARKET_DIR):
    MARKET_DIR = os.path.join(bot.resource_dir(), "..", "market")
if not os.path.isdir(MARKET_DIR):
    # empacotado com --add-data (ver build): viaja DENTRO do proprio
    # executavel/app, nao depende do usuario manter a pasta 'market' do
    # lado dele - ex: arrastar so' o .app pra /Applications no Mac sem
    # levar 'market' junto, causa real ja vista de "Painel de mercado
    # indisponivel" mesmo com o build certo.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        MARKET_DIR = os.path.join(meipass, "market")
if os.path.isdir(MARKET_DIR) and MARKET_DIR not in sys.path:
    sys.path.append(MARKET_DIR)

# trava simples pro botao 'Acompanhar Mercado' - so pede a senha antes de
# abrir a janela, nada alem disso por enquanto (sem hash/criptografia -
# monetizacao/controle de acesso de verdade fica pra depois).
MARKET_PASSWORD = "P@draocafe1"

# 'dom_tier_sort' pode separar loot so por raridade, so por atributo, ou
# exigindo os dois juntos (raridade marcada E pelo menos 1 atributo marcado)
# - o dropdown na linha da rotina usa esses rotulos em portugues, mas o que
# fica salvo em routines.json e a chave (bot.py so conhece as chaves).
MATCH_MODE_LABELS = {
    "rarity_only": "Raridade",
    "attribute_only": "Atributo",
    "rarity_and_attribute": "Raridade + Atributo",
}
MATCH_MODE_BY_LABEL = {label: key for key, label in MATCH_MODE_LABELS.items()}


class BotGUI:
    def __init__(self, root):
        self.root = root
        self.current_profile = bot.CURRENT_PROFILE  # navegador ativo nesta janela (Chrome por padrao)
        root.title(f"BAIAK IDLE BOT  v{bot.VERSION}  —  {bot.BROWSER_PROFILES[self.current_profile]['label']}")
        root.geometry("780x640")
        root.configure(fg_color=theme.BG)
        icon_path = os.path.join(bot.resource_dir(), "icon.ico")
        if os.path.exists(icon_path):
            try:
                root.iconbitmap(icon_path)
            except Exception:
                pass  # ex: icone ausente ou SO sem suporte a .ico - so segue sem

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.thread = None
        self.log_queue = queue.Queue()
        self.activities_shown = 0

        self.routines = bot.load_routines()
        self.flags = {}
        self.routine_rows = {}
        self.expanded_routines = set()
        self.sync_flags_from_routines()

        self.settings = bot.load_settings()
        bot.SOUND_MEMORY["enabled"] = self.settings.get("sound_enabled", True)
        bot.ADVANCE_MEMORY["enabled"] = self.settings.get("auto_advance_hunt", False)
        bot.DEFAULT_HUNT_MEMORY["name"] = self.settings.get("default_hunt", "")
        self.advance_var = tk.BooleanVar(value=bot.ADVANCE_MEMORY.get("enabled", False))
        self.settings_window = None
        self.default_hunt_label = None
        self.hunt_confirm_window = None
        self.close_dialog = None
        self.tray_icon = None
        self.tray_queue = queue.Queue()

        self.market_url = None
        self.codex_sync_lock = threading.Lock()
        self.codex_last_attempt = 0.0
        self.start_market_server()

        self.build_header()
        self.build_controls()
        self.build_status_panel()
        self.build_routines_panel()
        self.build_log_panel()

        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        if sys.platform == "win32":
            self.root.bind("<Unmap>", self.on_window_unmap)
            self.root.after(300, self.poll_tray_queue)
        self.root.after(100, self.poll_log_queue)
        self.root.after(500, self.poll_status)
        self.root.after(60_000, self.codex_market_tick)

    # ---------- layout ----------

    def build_header(self):
        header = ctk.CTkFrame(self.root, fg_color=theme.PANEL, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header, text="BAIAK IDLE BOT", font=theme.FONT_TITLE, text_color=theme.ACCENT
        ).pack(side="left", padx=20)

        ctk.CTkLabel(
            header, text=f"v{bot.VERSION}", font=theme.FONT_BODY, text_color=theme.MUTED
        ).pack(side="left")

        # botao de atualizacao - sempre visivel quando empacotado (frozen).
        # Ao clicar, checa na hora se ha versao nova: se ja estiver
        # atualizado, so' avisa no log; se nao, confirma e baixa. Em modo dev
        # ('python gui.py') nem aparece - nao ha executavel/app pra trocar.
        self.update_button = ctk.CTkButton(
            header,
            text="Atualizar",
            width=110,
            command=self.start_update,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color="#04140a",
            font=theme.FONT_BODY,
        )
        if getattr(sys, "frozen", False):
            self.update_button.pack(side="left", padx=(12, 0))

        # Seletor de navegador/conta ativo - decide QUAL routines.json/settings.json
        # esta em uso (ver bot.BROWSER_PROFILES). Fica separado do botao 'Abrir
        # Jogo': aqui e' configuracao (qual conta estou editando), la e' acao
        # (abrir o navegador escolhido aqui).
        self._profile_key_by_label = {p["label"]: key for key, p in bot.BROWSER_PROFILES.items()}
        self.browser_selector = ctk.CTkSegmentedButton(
            header,
            values=[p["label"] for p in bot.BROWSER_PROFILES.values()],
            command=self.on_browser_selected,
            fg_color=theme.PANEL_ALT,
            selected_color=theme.ACCENT,
            selected_hover_color=theme.ACCENT_HOVER,
            unselected_color=theme.PANEL_ALT,
            text_color=theme.TEXT,
        )
        self.browser_selector.set(bot.BROWSER_PROFILES[self.current_profile]["label"])
        self.browser_selector.pack(side="left", padx=(24, 0))

        # Botao pra abrir uma 2a janela (processo separado) no OUTRO navegador -
        # fica colado no seletor, ja que e' sobre o mesmo assunto (escolha de
        # navegador/conta), separado das acoes de Play/Abrir Jogo/etc.
        new_window_button = ctk.CTkButton(
            header,
            text="+",
            width=28,
            command=self.open_new_window,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_HEADER,
        )
        new_window_button.pack(side="left", padx=(6, 0))
        _Tooltip(
            new_window_button,
            "Abre uma outra janela do bot, ja no outro navegador\n"
            "(Chrome/Opera) - pra rodar os dois ao mesmo tempo,\n"
            "cada um com seu proprio Play/Pause/Stop.",
        )

        self.status_label = ctk.CTkLabel(
            header, text="● PARADO", font=theme.FONT_HEADER, text_color=theme.MUTED
        )
        self.status_label.pack(side="right", padx=20)

    def build_controls(self):
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=(16, 8))

        icon_kwargs = dict(font=theme.FONT_HEADER, width=44)

        self.start_button = ctk.CTkButton(
            bar,
            text="▶",
            command=self.start,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color="#04140a",
            **icon_kwargs,
        )
        self.start_button.pack(side="left", padx=(0, 6))

        self.pause_button = ctk.CTkButton(
            bar,
            text="⏸",
            command=self.pause,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            state="disabled",
            **icon_kwargs,
        )
        self.pause_button.pack(side="left", padx=6)

        self.stop_button = ctk.CTkButton(
            bar,
            text="■",
            command=self.stop,
            fg_color=theme.DANGER,
            hover_color=theme.DANGER_HOVER,
            text_color="#1a0006",
            state="disabled",
            **icon_kwargs,
        )
        self.stop_button.pack(side="left", padx=6)

        self.sound_button = ctk.CTkButton(
            bar,
            text="🔊",
            command=self.toggle_sound,
            **icon_kwargs,
        )
        self.sound_button.pack(side="left", padx=(16, 6))
        self._refresh_sound_button()

        ctk.CTkButton(
            bar,
            text="Abrir Jogo",
            command=self.open_browser,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
            border_width=1,
            border_color=theme.BORDER,
            width=140,
        ).pack(side="left", padx=6)

        # esconde a janela do Chrome/Opera (some da barra de tarefas) - o bot
        # continua jogando normal; o mesmo botao (ou a bandeja) mostra de volta.
        self.browser_button = ctk.CTkButton(
            bar,
            text="Esconder navegador",
            command=self.toggle_browser_hidden,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
            border_width=1,
            border_color=theme.BORDER,
            width=150,
        )
        self.browser_button.pack(side="left", padx=6)

        ctk.CTkButton(
            bar,
            text="Acompanhar Mercado",
            command=self.open_market,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
            border_width=1,
            border_color=theme.BORDER,
            width=160,
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            bar,
            text="⚙",
            command=self.open_settings_window,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
            **icon_kwargs,
        ).pack(side="right", padx=(8, 0))

    def open_settings_window(self):
        """Janela separada reservada pras configuracoes de verdade (hoje so
        'Avancar hunt automaticamente', mais devem entrar aqui com o tempo) -
        o que e atalho/acao direta (som, abrir navegador) fica na barra
        principal, so 'Abrir Logs' ficou aqui por enquanto por falta de outro
        lugar melhor."""
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        win = ctk.CTkToplevel(self.root)
        win.title("Configurações")
        win.geometry("360x300")
        win.configure(fg_color=theme.BG)
        win.transient(self.root)
        self.settings_window = win

        ctk.CTkLabel(win, text="CONFIGURAÇÕES", font=theme.FONT_HEADER, text_color=theme.MUTED).pack(
            anchor="w", padx=20, pady=(20, 10)
        )

        ctk.CTkCheckBox(
            win,
            text="Avançar hunt automaticamente",
            variable=self.advance_var,
            command=self.toggle_advance,
            font=theme.FONT_BODY,
            text_color=theme.TEXT,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
        ).pack(anchor="w", padx=20, pady=6)

        hunt_row = ctk.CTkFrame(win, fg_color="transparent")
        hunt_row.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(hunt_row, text="Hunt padrão:", font=theme.FONT_BODY, text_color=theme.TEXT).pack(
            side="left"
        )
        self.default_hunt_label = ctk.CTkLabel(
            hunt_row,
            text=self.settings.get("default_hunt") or "(nenhuma)",
            font=theme.FONT_BODY,
            text_color=theme.MUTED,
        )
        self.default_hunt_label.pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            win,
            text="Escolher hunt padrão",
            command=self.open_hunt_picker,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
            border_width=1,
            border_color=theme.BORDER,
        ).pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkButton(
            win,
            text="Abrir Logs",
            command=self.open_logs_folder,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
            border_width=1,
            border_color=theme.BORDER,
        ).pack(fill="x", padx=20, pady=(16, 6))

    def open_browser(self):
        """So abre/conecta o navegador do perfil ATUALMENTE selecionado (ver
        seletor de navegador no cabecalho) - escolher qual navegador e uma
        decisao de configuracao, separada da acao de abrir o jogo."""
        threading.Thread(target=bot.launch_browser, args=(self.log,), daemon=True).start()

    def toggle_browser_hidden(self):
        if not bot.browser_hide_supported():
            self.log("Esconder o navegador so funciona com Chrome/Opera no Windows (o IdleDeck ja tem a bandeja dele).")
            return
        hide = not bot.browser_hidden()
        try:
            if bot.set_browser_hidden(hide, self.log):
                self.log("Navegador escondido - o bot continua jogando normal." if hide else "Navegador de volta na tela.")
        except Exception as error:
            self.log(f"Erro ao {'esconder' if hide else 'mostrar'} o navegador: {error}")
        self.browser_button.configure(text="Mostrar navegador" if bot.browser_hidden() else "Esconder navegador")

    def open_new_window(self):
        """Abre uma 2a instancia do bot (processo novo, independente) ja no
        OUTRO navegador. Uma janela so consegue rodar automacao pra UM
        navegador por vez (o bot.py guarda o estado da automacao em
        variaveis globais do processo) - pra Chrome e Opera rodarem ao mesmo
        tempo, cada Play/Pause/Stop precisa ser de um processo separado."""
        # com 3+ perfis, vai pro PROXIMO na ordem (Chrome -> Opera -> IdleDeck -> Chrome)
        keys = list(bot.BROWSER_PROFILES)
        other_profile = keys[(keys.index(self.current_profile) + 1) % len(keys)]
        if getattr(sys, "frozen", False):
            args = [sys.executable, "--profile", other_profile]
        else:
            args = [sys.executable, os.path.abspath(__file__), "--profile", other_profile]
        try:
            subprocess.Popen(args, cwd=bot.resource_dir())
            self.log(f"Abrindo nova janela no {bot.BROWSER_PROFILES[other_profile]['label']}...")
        except Exception as error:
            self.log(f"Erro ao abrir nova janela: {error}")

    def start_update(self):
        """Ao clicar: checa na hora (numa thread, sem travar a tela) se ha
        versao nova. Se ja estiver atualizado, so' avisa no log. Se houver
        uma nova, confirma com o usuario antes de baixar/trocar - troca em
        si (bot.apply_update) nunca mexe em routines.json/settings.json,
        entao a configuracao do usuario fica intacta."""
        if self.thread is not None and self.thread.is_alive():
            self.log("Pare o bot antes de atualizar.")
            return

        self.update_button.configure(state="disabled", text="Verificando...")

        def worker():
            info = bot.check_for_update(log=self.log)
            self.root.after(0, lambda: self._on_check_result(info))

        threading.Thread(target=worker, daemon=True).start()

    def _on_check_result(self, info):
        self.update_button.configure(state="normal", text="Atualizar")
        if not info:
            self.log(f"Voce ja esta na versao mais recente (v{bot.VERSION}).")
            return
        self._show_update_confirm(info)

    def _show_update_confirm(self, info):
        win = ctk.CTkToplevel(self.root)
        win.title("Atualizar")
        win.geometry("360x170")
        win.configure(fg_color=theme.BG)
        win.transient(self.root)
        win.attributes("-topmost", True)

        ctk.CTkLabel(
            win,
            text=(
                f"Nova versao disponivel: v{info['version']}\n"
                f"(atual: v{bot.VERSION})\n\n"
                "O bot vai fechar e reabrir sozinho, ja atualizado."
            ),
            font=theme.FONT_BODY,
            text_color=theme.TEXT,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(20, 10))

        def confirm():
            win.destroy()
            self._begin_update(info)

        ctk.CTkButton(
            win, text="Atualizar agora", command=confirm, fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER, text_color="#04140a",
        ).pack(padx=20, pady=(0, 8), fill="x")
        ctk.CTkButton(
            win, text="Agora nao", command=win.destroy, fg_color=theme.PANEL_ALT,
            text_color=theme.TEXT, border_width=1, border_color=theme.BORDER,
        ).pack(padx=20, fill="x")

    def _begin_update(self, info):
        """Baixa e instala a atualizacao numa thread, com uma tela de
        carregamento por cima de TUDO (ve _show_update_overlay) - nada da
        interface pode ser usado ate o bot fechar e reabrir atualizado."""
        self.update_button.configure(state="disabled", text="Atualizando...")
        self._show_update_overlay(info["version"])
        self.log(f"Baixando atualizacao (v{info['version']})...")

        last_percent = [-1]

        def progress(fraction):
            # o download avisa a cada bloquinho (milhares de vezes) - so' mexe
            # na tela quando o percentual muda.
            percent = int(fraction * 100)
            if percent != last_percent[0]:
                last_percent[0] = percent
                self.root.after(0, lambda: self._set_update_progress(fraction))

        def worker():
            ok = bot.apply_update(info["asset_url"], log=self.log, progress=progress)
            self.root.after(0, lambda: self._on_update_applied(ok))

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_overlay(self, version):
        overlay = ctk.CTkFrame(self.root, fg_color=theme.BG, corner_radius=0)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()

        card = ctk.CTkFrame(overlay, fg_color=theme.PANEL, corner_radius=12)
        card.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(
            card, text=f"ATUALIZANDO PARA v{version}", font=theme.FONT_TITLE, text_color=theme.ACCENT,
        ).pack(padx=40, pady=(28, 10))
        self.update_status_label = ctk.CTkLabel(
            card, text="Baixando a atualizacao...", font=theme.FONT_BODY, text_color=theme.TEXT,
        )
        self.update_status_label.pack(padx=40, pady=(0, 12))
        self.update_progress_bar = ctk.CTkProgressBar(
            card, width=340, progress_color=theme.ACCENT, fg_color=theme.PANEL_ALT,
        )
        self.update_progress_bar.set(0)
        self.update_progress_bar.pack(padx=40, pady=(0, 14))
        ctk.CTkLabel(
            card,
            text="Nao feche nem use o bot agora - ele reabre sozinho\nquando a atualizacao terminar.",
            font=theme.FONT_BODY, text_color=theme.MUTED, justify="center",
        ).pack(padx=40, pady=(0, 28))

        self.update_overlay = overlay
        # grab: todo clique e tecla fica restrito a essa tela - nada por baixo reage.
        overlay.grab_set()
        overlay.focus_set()

    def _set_update_progress(self, fraction):
        if getattr(self, "update_overlay", None) is None:
            return
        self.update_progress_bar.set(fraction)
        self.update_status_label.configure(text=f"Baixando a atualizacao... {int(fraction * 100)}%")

    def _hide_update_overlay(self):
        overlay = getattr(self, "update_overlay", None)
        if overlay is None:
            return
        overlay.grab_release()
        overlay.destroy()
        self.update_overlay = None

    def _on_update_applied(self, ok):
        if not ok:
            self._hide_update_overlay()
            self.update_button.configure(state="normal", text="Atualizar")
            self.log("Nao consegui aplicar a atualizacao - tente de novo mais tarde, ou baixe manualmente na pagina de releases.")
            return
        # o script auxiliar (.bat/.sh) so' continua a troca quando ESTE
        # processo terminar de verdade - mostra "reiniciando" por um instante
        # (pra dar pra ler) e fecha a janela.
        self.update_progress_bar.set(1)
        self.update_status_label.configure(text="Instalando... reiniciando o bot.")
        self.root.after(1500, self.on_close)

    def on_browser_selected(self, label):
        """Chamado pelo seletor de navegador no cabecalho."""
        profile_key = self._profile_key_by_label.get(label)
        if profile_key is None or profile_key == self.current_profile:
            return
        if self.thread is not None and self.thread.is_alive():
            self.log(
                f"Nao troquei pro {label}: pare o bot antes de trocar de navegador."
            )
            self.browser_selector.set(bot.BROWSER_PROFILES[self.current_profile]["label"])
            return
        self.switch_profile(profile_key)

    def switch_profile(self, profile_key):
        """Troca o navegador/conta ativo nesta janela: recarrega routines.json/
        settings.json do perfil escolhido (cada navegador tem os seus, ver
        bot.BROWSER_PROFILES) e atualiza a tela toda pra refletir. So muda a
        CONFIGURACAO carregada - nao abre navegador nenhum sozinho (isso e' o
        botao 'Abrir Jogo')."""
        bot.set_active_profile(profile_key)
        self.current_profile = profile_key
        self.browser_selector.set(bot.BROWSER_PROFILES[profile_key]["label"])

        self.routines = bot.load_routines()
        self.expanded_routines = set()
        self.sync_flags_from_routines()
        self.rebuild_routine_rows()

        self.settings = bot.load_settings()
        bot.SOUND_MEMORY["enabled"] = self.settings.get("sound_enabled", True)
        bot.ADVANCE_MEMORY["enabled"] = self.settings.get("auto_advance_hunt", False)
        bot.DEFAULT_HUNT_MEMORY["name"] = self.settings.get("default_hunt", "")
        self.advance_var.set(bot.ADVANCE_MEMORY.get("enabled", False))
        if self.default_hunt_label is not None and self.default_hunt_label.winfo_exists():
            self.default_hunt_label.configure(text=self.settings.get("default_hunt") or "(nenhuma)")
        self._refresh_sound_button()

        label = bot.BROWSER_PROFILES[profile_key]["label"]
        self.root.title(f"BAIAK IDLE BOT  v{bot.VERSION}  —  {label}")
        self.log(f"Navegador ativo: {label}")

    def start_market_server(self):
        """Sobe o servidor do painel de mercado (market/server.py) junto do
        bot, sem abrir navegador nenhum sozinho - so guarda a URL pro botao
        'Acompanhar Mercado' abrir quando o usuario quiser. So faz algo se a
        pasta 'market' existir (ver MARKET_DIR) - o codigo agora vai junto
        no build compartilhado, entao qualquer instalacao tem essa pasta,
        cada uma com seu proprio banco de dados vazio (nunca o pessoal).

        No Mac instalado num lugar sem permissao de escrita (ex:
        /Applications), o banco/perfil do Chrome dedicado do market NAO
        podem ficar do lado do proprio server.py (mesmo problema ja
        corrigido pro resto do bot - ver bot.data_dir()) - redireciona pra
        uma pasta gravavel ANTES de importar o server, via variavel de
        ambiente (BAIAK_DB/BAIAK_MARKET_PROFILE_DIR).

        So' UMA instancia do bot deve rodar o poller de mercado de verdade
        (ver 'Nova Janela' - com 2 janelas abertas, so a 1a consegue). Se a
        porta ja estiver em uso (2a janela em diante), nao tenta de novo -
        so aponta o botao 'Acompanhar Mercado' pro servidor que ja esta de
        pe na 1a janela, e segue sem tentar rodar um 2o poller."""
        if not os.path.isdir(MARKET_DIR):
            # antes saia calado daqui - "Painel de mercado indisponivel" no
            # botao sem NADA no log pra explicar o motivo. A pasta 'market'
            # precisa estar fisicamente do lado do executavel/app (nao e'
            # recriada sozinha, tem codigo de verdade dentro) - se o usuario
            # moveu so' o .app (ex: arrastou pra /Applications sem levar a
            # pasta 'market' junto), e' exatamente isso que falta.
            self.log(f"Painel de mercado nao iniciou: pasta 'market' nao encontrada (esperada em '{MARKET_DIR}').")
            return
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            # mesma condicao exata de bot.data_dir() - so redireciona no caso
            # que realmente precisa (app empacotado no Mac); no Windows e em
            # modo dev, MARKET_DIR ja e' gravavel (dist/ do usuario, ou a
            # arvore de codigo-fonte), nao mexe em nada.
            market_data_dir = os.path.join(bot.data_dir(), "market")
            os.makedirs(market_data_dir, exist_ok=True)
            os.environ["BAIAK_DB"] = os.path.join(market_data_dir, "market.db")
            os.environ["BAIAK_MARKET_PROFILE_DIR"] = os.path.join(market_data_dir, "chrome_profile")
        try:
            import server as market_server
        except Exception as error:
            self.log(f"Painel de mercado nao iniciou: {error}")
            return
        try:
            _, url = market_server.start_server(open_browser=False)
            self.market_url = url
            try:
                import codex as market_codex
                market_codex.set_refresher(lambda: self.sync_codex_for_market(automatic=False))
            except Exception as error:
                self.log(f"Mapa do Codex indisponivel no painel de mercado: {error}")
        except OSError:
            port = market_server.CFG.get("http_port", 8787)
            self.market_url = f"http://127.0.0.1:{port}/"
            self.log("Painel de mercado ja esta rodando em outra janela do bot - usando ele.")
        except Exception as error:
            self.log(f"Painel de mercado nao iniciou: {error}")

    def open_market(self):
        """Pede senha antes de abrir o painel de mercado - trava simples por
        enquanto (senha fixa no codigo); hash/monetizacao ficam pra depois."""
        if not self.market_url:
            self.log("Painel de mercado indisponivel (nao iniciou - ver log no começo).")
            return

        win = ctk.CTkToplevel(self.root)
        win.title("Acompanhar Mercado")
        win.geometry("340x160")
        win.configure(fg_color=theme.BG)
        win.transient(self.root)
        win.attributes("-topmost", True)

        ctk.CTkLabel(
            win, text="Senha de acesso:", font=theme.FONT_BODY, text_color=theme.TEXT
        ).pack(anchor="w", padx=20, pady=(20, 6))

        pw_var = tk.StringVar()
        entry = ctk.CTkEntry(win, textvariable=pw_var, show="*", fg_color=theme.PANEL_ALT)
        entry.pack(padx=20, fill="x")
        entry.focus()

        error_label = ctk.CTkLabel(win, text="", font=theme.FONT_BODY, text_color=theme.DANGER)
        error_label.pack(anchor="w", padx=20, pady=(4, 0))

        def submit(event=None):
            if pw_var.get() == MARKET_PASSWORD:
                win.destroy()
                self._open_market_dashboard()
            else:
                error_label.configure(text="Senha incorreta.")
                pw_var.set("")

        entry.bind("<Return>", submit)
        ctk.CTkButton(
            win,
            text="Entrar",
            command=submit,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color="#04140a",
        ).pack(padx=20, pady=16, fill="x")

    def codex_market_tick(self):
        """A cada minuto: com o bot rodando, deixa o mapa do Codex do market
        em dia (ver CODEX_BOT_SYNC_SECONDS) - a leitura em si decide se ja
        esta na hora."""
        try:
            if self.market_url and self.thread is not None and self.thread.is_alive():
                threading.Thread(
                    target=lambda: self.sync_codex_for_market(automatic=True, periodic=True),
                    daemon=True,
                ).start()
        finally:
            self.root.after(60_000, self.codex_market_tick)

    def sync_codex_for_market(self, automatic=False, periodic=False):
        """Le o Codex da conta (so' le, nunca entrega nada) e grava o mapa pro
        market cruzar o que falta com os leiloes de empilhaveis. Bloqueia ate
        acabar - chame de uma thread. Com o bot rodando, a leitura e' feita na
        thread dele (mexer no Codex numa segunda conexao briga com as rotinas).

        automatic=True (ao abrir o market): so' le se o jogo ja esta aberto
        (bot rodando ou navegador com depuracao ligada) e se o ultimo mapa tem
        mais de CODEX_AUTO_SYNC_MIN_SECONDS - nunca abre o navegador sozinho."""
        if not self.codex_sync_lock.acquire(blocking=False):
            return  # ja tem uma leitura em andamento
        try:
            import codex as market_codex
            import server as market_server
            path = market_codex.progress_path(market_server.CFG)
            bot_running = self.thread is not None and self.thread.is_alive()
            if automatic:
                if not bot_running and not bot.is_debug_port_open():
                    self.log("Codex nao mapeado pro market: abra o jogo (ou inicie o bot) e use 'Atualizar Codex' no painel.")
                    return
                min_age = CODEX_BOT_SYNC_SECONDS if periodic else CODEX_AUTO_SYNC_MIN_SECONDS
                try:
                    if time.time() - os.path.getmtime(path) < min_age:
                        return
                except OSError:
                    pass
                if periodic and time.time() - self.codex_last_attempt < CODEX_BOT_RETRY_SECONDS:
                    return
            self.codex_last_attempt = time.time()
            self.log("Mapeando o Codex da conta pro market...")
            if bot_running:
                data = bot.request_from_bot(bot.read_codex_progress, timeout=240)
            else:
                data = bot.fetch_codex_progress(log=self.log)
            if data and data.get("entries"):
                bot.save_codex_progress(data, path)
            else:
                retry = f" (nova tentativa em {CODEX_BOT_RETRY_SECONDS // 60} min)" if periodic and bot_running else ""
                self.log(f"  Nao consegui ler o Codex pro market - mantendo o ultimo mapa salvo{retry}.")
        except Exception as error:
            self.log(f"  Erro ao mapear o Codex pro market: {error}")
        finally:
            self.codex_sync_lock.release()

    def _open_market_dashboard(self):
        try:
            import server as market_server
            market_server.open_dashboard(self.market_url)
        except Exception as error:
            self.log(f"Erro ao abrir o painel de mercado: {error}")
        # "quando abrir o market, mapear o Codex" - em segundo plano, o painel
        # mostra o resultado assim que o arquivo aparecer
        threading.Thread(target=lambda: self.sync_codex_for_market(automatic=True), daemon=True).start()

    def _refresh_sound_button(self):
        enabled = bot.SOUND_MEMORY.get("enabled", True)
        self.sound_button.configure(
            text="🔊" if enabled else "🔇",
            fg_color=theme.ACCENT if enabled else theme.PANEL_ALT,
            hover_color=theme.ACCENT_HOVER if enabled else theme.BORDER,
            text_color="#04140a" if enabled else theme.TEXT,
            border_width=0 if enabled else 1,
            border_color=theme.BORDER,
        )

    def toggle_sound(self):
        enabled = not bot.SOUND_MEMORY.get("enabled", True)
        bot.SOUND_MEMORY["enabled"] = enabled
        self.settings["sound_enabled"] = enabled
        bot.save_settings(self.settings)
        self._refresh_sound_button()

    def toggle_advance(self):
        enabled = self.advance_var.get()
        bot.ADVANCE_MEMORY["enabled"] = enabled
        self.settings["auto_advance_hunt"] = enabled
        bot.save_settings(self.settings)

    def open_hunt_picker(self):
        def on_saved(name):
            self.settings["default_hunt"] = name
            bot.save_settings(self.settings)
            bot.DEFAULT_HUNT_MEMORY["name"] = name
            if self.default_hunt_label is not None and self.default_hunt_label.winfo_exists():
                self.default_hunt_label.configure(text=name or "(nenhuma)")

        def on_refresh(callback):
            def worker():
                try:
                    hunts = bot.fetch_hunt_names(log=self.log)
                except Exception as error:
                    self.log(f"  Erro ao capturar lista de Hunts: {error}")
                    hunts = []
                if hunts:
                    # guarda a lista - da proxima vez abre na hora, sem ir ao jogo
                    self.settings["hunts_cache"] = hunts
                    bot.save_settings(self.settings)
                else:
                    hunts = self.settings.get("hunts_cache") or []  # falhou: mantem a salva
                callback(hunts)

            threading.Thread(target=worker, daemon=True).start()

        HuntPicker(
            self.root,
            self.settings.get("hunts_cache") or [],
            self.settings.get("default_hunt", ""),
            on_saved=on_saved,
            on_refresh=on_refresh,
        )

    def open_codex_campaign_picker(self, step):
        def on_saved(queue):
            step["queue"] = queue
            bot.save_routines(self.routines)
            bot.reset_campaign_memory()
            bot.FORCE_RUN_NOW.add("campanha_codex")
            if queue and not self.flags["campanha_codex"].is_set():
                self.log("Campanha de Codex salva - marque a rotina 'Campanha de Codex' pra ativar.")
            self.rebuild_routine_rows()

        def on_refresh(callback):
            def worker():
                data = None
                try:
                    if self.thread is not None and self.thread.is_alive():
                        data = bot.request_codex_refresh_from_bot()  # o bot rodando atende na propria thread
                    else:
                        data = bot.fetch_codex_campaign_data(log=self.log)
                except Exception as error:
                    self.log(f"  Erro ao ler o Codex: {error}")
                if data and data.get("entries"):
                    # guarda - da proxima vez abre na hora, sem ir ao jogo
                    self.settings["codex_entries_cache"] = data["entries"]
                    if data.get("hunts"):
                        self.settings["hunts_cache"] = data["hunts"]
                    bot.save_settings(self.settings)
                    callback(data["entries"], data.get("hunts") or self.settings.get("hunts_cache") or [])
                else:
                    self.log("  Nao consegui ler o Codex - mantendo a lista salva.")
                    callback(None, [])

            threading.Thread(target=worker, daemon=True).start()

        CodexCampaignPicker(
            self.root,
            self.settings.get("codex_entries_cache") or [],
            self.settings.get("hunts_cache") or [],
            step.get("queue", []),
            on_saved=on_saved,
            on_refresh=on_refresh,
        )

    def open_potion_picker(self):
        def on_saved(config):
            self.settings["potions"] = config
            bot.save_settings(self.settings)
            marked = sum(1 for v in config["items"].values() if v["use_qty"] > 0 or v["buy_daily"])
            self.log(f"Poções salvas ({marked} tipo(s) configurado(s); uso nos chefes {'ligado' if config['use_in_bosses'] else 'desligado'}).")
            self.rebuild_routine_rows()

        def on_refresh(callback):
            def worker():
                info = None
                try:
                    if self.thread is not None and self.thread.is_alive():
                        info = bot.request_from_bot(bot.read_potion_info)  # o bot rodando atende na propria thread
                    else:
                        info = bot.fetch_potion_info(log=self.log)
                except Exception as error:
                    self.log(f"  Erro ao ler o Mercador: {error}")
                if info and info.get("potions"):
                    self.settings["potion_catalog"] = info["potions"]
                    self.settings["potion_stock"] = info["stock"]
                    bot.save_settings(self.settings)
                    callback(info["potions"], info["stock"])
                else:
                    self.log("  Nao consegui ler o Mercador - mantendo a lista salva.")
                    callback(None, None)

            threading.Thread(target=worker, daemon=True).start()

        PotionPicker(
            self.root,
            self.settings.get("potion_catalog") or bot.POTION_CATALOG_DEFAULT,
            self.settings.get("potion_stock") or {},
            bot.potion_config(),
            bot.boss_potion_suggestion(),
            on_saved=on_saved,
            on_refresh=on_refresh,
        )

    def build_status_panel(self):
        ctk.CTkLabel(self.root, text="STATUS", font=theme.FONT_HEADER, text_color=theme.MUTED).pack(
            anchor="w", padx=20
        )

        status_bar = ctk.CTkFrame(self.root, fg_color=theme.PANEL, corner_radius=8)
        status_bar.pack(fill="x", padx=20, pady=(4, 12))

        self.status_training_label = ctk.CTkLabel(
            status_bar, text="Treino: —", font=theme.FONT_BODY, text_color=theme.TEXT
        )
        self.status_training_label.pack(side="left", padx=14, pady=10)

        self.status_sell_label = ctk.CTkLabel(
            status_bar, text="Última venda: —", font=theme.FONT_BODY, text_color=theme.TEXT
        )
        self.status_sell_label.pack(side="left", padx=14, pady=10)

        self.status_boss_label = ctk.CTkLabel(
            status_bar, text="Chefes: —", font=theme.FONT_BODY, text_color=theme.TEXT
        )
        self.status_boss_label.pack(side="left", padx=14, pady=10)

        self.status_guild_label = ctk.CTkLabel(
            status_bar, text="Tasks da guild: —", font=theme.FONT_BODY, text_color=theme.TEXT
        )
        self.status_guild_label.pack(side="left", padx=14, pady=10)

        self.status_bestiary_label = ctk.CTkLabel(
            status_bar, text="Bestiary: —", font=theme.FONT_BODY, text_color=theme.TEXT
        )
        self.status_bestiary_label.pack(side="left", padx=14, pady=10)

    def poll_status(self):
        if bot.TRAINING_MEMORY.get("waiting"):
            hunt_name = bot.TRAINING_MEMORY.get("hunt_name") or "?"
            self.status_training_label.configure(text=f"Treino: ativo (voltar pra '{hunt_name}')")
        else:
            self.status_training_label.configure(text="Treino: não")

        last_sell = bot.LAST_ACTION_MEMORY.get("Vender tudo")
        if last_sell:
            self.status_sell_label.configure(text=f"Última venda: {self.format_ago(last_sell)}")
        else:
            self.status_sell_label.configure(text="Última venda: —")

        # usa 'display_next_check' (sem a margem de seguranca interna) pra
        # mostrar o mesmo numero que o jogo mostraria - a margem e so pra
        # controlar QUANDO o bot volta a consultar, nao deve aparecer aqui.
        next_check = bot.BOSS_MEMORY.get("display_next_check", 0.0)
        remaining = next_check - time.monotonic()
        if remaining > 1:
            self.status_boss_label.configure(text=f"Chefes: proxima checagem {self.format_eta(remaining)}")
        else:
            self.status_boss_label.configure(text="Chefes: pronto pra checar")

        last_hunt = bot.BESTIARY_MEMORY.get("last_hunt")
        if last_hunt:
            count = len(bot.BESTIARY_MEMORY.get("monsters") or [])
            self.status_bestiary_label.configure(text=f"Bestiary: rastreando {count} criatura(s) de '{last_hunt}'")
        else:
            self.status_bestiary_label.configure(text="Bestiary: —")

        if bot.GUILD_TASK_MEMORY.get("grinding"):
            previous_hunt = bot.GUILD_TASK_MEMORY.get("previous_hunt") or "?"
            self.status_guild_label.configure(text=f"Tasks da guild: caçando (voltar pra '{previous_hunt}')")
        else:
            self.status_guild_label.configure(text="Tasks da guild: inativo")

        self.poll_activities()
        self.check_hunt_advance_confirm()

        self.root.after(1000, self.poll_status)

    def check_hunt_advance_confirm(self):
        """Se o bot estiver esperando confirmacao pra avancar de hunt (Codex
        e Bestiary da atual completos, ver bot.HUNT_ADVANCE_CONFIRM), mostra
        um popup perguntando - so uma vez por pergunta (nao reabre enquanto
        ja tiver um popup esperando resposta)."""
        current_hunt = bot.HUNT_ADVANCE_CONFIRM.get("current_hunt")
        if current_hunt is None or bot.HUNT_ADVANCE_CONFIRM.get("answer") is not None:
            return
        if self.hunt_confirm_window is not None and self.hunt_confirm_window.winfo_exists():
            return
        self.show_hunt_advance_popup(current_hunt, bot.HUNT_ADVANCE_CONFIRM.get("next_hunt"))

    def show_hunt_advance_popup(self, current_hunt, next_hunt):
        if self.tray_icon is not None:
            self.show_from_tray()  # escondido na bandeja o popup (transient) tambem ficaria invisivel
        win = ctk.CTkToplevel(self.root)
        win.title("Avançar de hunt?")
        win.geometry("380x170")
        win.configure(fg_color=theme.BG)
        win.transient(self.root)
        win.attributes("-topmost", True)
        self.hunt_confirm_window = win

        def answer(value):
            bot.HUNT_ADVANCE_CONFIRM["answer"] = value
            if value:
                self.settings["default_hunt"] = next_hunt
                if self.default_hunt_label is not None and self.default_hunt_label.winfo_exists():
                    self.default_hunt_label.configure(text=next_hunt or "(nenhuma)")
            win.destroy()

        ctk.CTkLabel(
            win,
            text=f"Codex e Bestiary de '{current_hunt}' completos.",
            font=theme.FONT_BODY,
            text_color=theme.TEXT,
            wraplength=340,
        ).pack(padx=20, pady=(20, 6))
        ctk.CTkLabel(
            win,
            text=f"Ir para a próxima hunt: '{next_hunt}'?",
            font=theme.FONT_BODY,
            text_color=theme.MUTED,
            wraplength=340,
        ).pack(padx=20, pady=(0, 16))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(pady=(0, 16))
        ctk.CTkButton(
            btn_row,
            text="Ir",
            command=lambda: answer(True),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color="#04140a",
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            btn_row,
            text="Ficar aqui",
            command=lambda: answer(False),
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            border_width=1,
            border_color=theme.BORDER,
        ).pack(side="left", padx=8)

        win.protocol("WM_DELETE_WINDOW", lambda: answer(False))

    def poll_activities(self):
        """Acrescenta no painel de Atividades Recentes qualquer 'conquista'
        nova (task de guild entregue, build atualizada, Codex completo) - o
        som ja tocou no momento do evento (play_achievement_sound), isso aqui
        so deixa o registro escrito, sem popup bloqueando o bot."""
        items = bot.ACTIVITY_MEMORY.get("items") or []
        new_items = items[self.activities_shown:]
        if not new_items:
            return
        self.activities_shown = len(items)
        try:
            _, bottom = self.activity_box.yview()
            was_at_bottom = bottom >= 0.999
        except Exception:
            was_at_bottom = True
        self.activity_box.configure(state="normal")
        for item in new_items:
            when = datetime.datetime.fromtimestamp(item["timestamp"]).strftime("%H:%M:%S")
            self.activity_box.insert("end", f"[{when}] {item['message']}\n")
        line_count = int(self.activity_box.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.activity_box.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        if was_at_bottom:
            self.activity_box.see("end")
        self.activity_box.configure(state="disabled")

    @staticmethod
    def format_ago(timestamp):
        elapsed = time.time() - timestamp
        if elapsed < 60:
            return f"há {int(elapsed)}s"
        if elapsed < 3600:
            return f"há {int(elapsed // 60)}min"
        return f"há {elapsed / 3600:.1f}h"

    @staticmethod
    def format_eta(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return f"em {seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"em {minutes}min"
        hours, remaining_minutes = divmod(minutes, 60)
        return f"em {hours}h {remaining_minutes}min"

    def build_routines_panel(self):
        ctk.CTkLabel(self.root, text="ROTINAS", font=theme.FONT_HEADER, text_color=theme.MUTED).pack(
            anchor="w", padx=20
        )

        self.routines_frame = ctk.CTkScrollableFrame(
            self.root, fg_color=theme.PANEL, corner_radius=8, height=180
        )
        self.routines_frame.pack(fill="x", padx=20, pady=(4, 12))
        self.rebuild_routine_rows()

    def build_log_panel(self):
        split = ctk.CTkFrame(self.root, fg_color="transparent")
        split.pack(fill="both", expand=True, padx=20, pady=(4, 20))

        log_col = ctk.CTkFrame(split, fg_color="transparent")
        log_col.pack(side="left", fill="both", expand=True, padx=(0, 8))

        log_header = ctk.CTkFrame(log_col, fg_color="transparent")
        log_header.pack(fill="x")
        ctk.CTkLabel(log_header, text="LOG", font=theme.FONT_HEADER, text_color=theme.MUTED).pack(
            side="left"
        )

        # filtro de nivel - reduz a poluicao do log deixando so o que importa
        # (ex: so erro) quando o usuario quiser cacar um problema; 'Todos'
        # continua disponivel pra ver tudo de novo.
        filter_box = ctk.CTkFrame(log_header, fg_color="transparent")
        filter_box.pack(side="right")
        self.log_filter_vars = {
            "normal": tk.BooleanVar(value=True),
            "warning": tk.BooleanVar(value=True),
            "error": tk.BooleanVar(value=True),
        }
        for level, label, color in (
            ("normal", "Normal", theme.MUTED),
            ("warning", "Aviso", theme.WARNING),
            ("error", "Erro", theme.DANGER),
        ):
            ctk.CTkCheckBox(
                filter_box,
                text=label,
                variable=self.log_filter_vars[level],
                command=self.apply_log_filter,
                font=theme.FONT_BODY,
                text_color=color,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                checkbox_width=16,
                checkbox_height=16,
            ).pack(side="left", padx=(8, 0))

        self.log_entries = []  # [(nivel, mensagem)] - guarda TUDO (ate o limite) pra poder reaplicar o filtro sem perder historico
        self.log_box = ctk.CTkTextbox(
            log_col,
            fg_color=theme.PANEL,
            text_color=theme.ACCENT,
            font=theme.FONT_MONO_LOG,
            corner_radius=8,
        )
        self.log_box.pack(fill="both", expand=True, pady=(4, 0))
        self.log_box.tag_config("error", foreground=theme.DANGER)
        self.log_box.tag_config("warning", foreground=theme.WARNING)
        self.log_box.configure(state="disabled")

        activity_col = ctk.CTkFrame(split, fg_color="transparent")
        activity_col.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ctk.CTkLabel(
            activity_col, text="ATIVIDADES RECENTES", font=theme.FONT_HEADER, text_color=theme.MUTED
        ).pack(anchor="w")
        self.activity_box = ctk.CTkTextbox(
            activity_col,
            fg_color=theme.PANEL,
            text_color=theme.ACCENT,
            font=theme.FONT_MONO_LOG,
            corner_radius=8,
        )
        self.activity_box.pack(fill="both", expand=True, pady=(4, 0))
        self.activity_box.configure(state="disabled")

    # ---------- rotinas ----------

    def sync_flags_from_routines(self):
        """Cria um Event por rotina, ja no estado ligado/desligado salvo em routines.json."""
        self.flags = {}
        for routine in self.routines:
            flag = threading.Event()
            if routine.get("enabled"):
                flag.set()
            self.flags[routine["id"]] = flag

    def rebuild_routine_rows(self):
        for widget in self.routines_frame.winfo_children():
            widget.destroy()
        self.routine_rows = {}

        if not self.routines:
            ctk.CTkLabel(
                self.routines_frame, text="Nenhuma rotina configurada.", text_color=theme.MUTED
            ).pack(anchor="w", padx=8, pady=8)
            return

        for routine in self.routines:
            flag = self.flags[routine["id"]]
            container = ctk.CTkFrame(self.routines_frame, fg_color="transparent")
            container.pack(fill="x", padx=6, pady=4)

            row = ctk.CTkFrame(container, fg_color=theme.PANEL_ALT, corner_radius=6)
            row.pack(fill="x")

            var = tk.BooleanVar(value=flag.is_set())
            check = ctk.CTkCheckBox(
                row,
                text=routine["name"],
                variable=var,
                command=lambda routine_id=routine["id"]: self.toggle_routine(routine_id),
                font=theme.FONT_BODY,
                text_color=theme.TEXT,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
            )
            check.pack(side="left", padx=10, pady=8)

            trigger = routine.get("trigger", {})
            if trigger.get("mode") == "interval":
                trigger_text = f"a cada {trigger.get('seconds', '?')}s"
            else:
                trigger_text = "automatico"
            ctk.CTkLabel(
                row,
                text=f"{trigger_text}  ·  {len(routine['steps'])} passo(s)",
                font=theme.FONT_BODY,
                text_color=theme.MUTED,
            ).pack(side="right", padx=10)

            # 'dom_threshold_click' (ex: enviar pro treino) e 'dom_resume_hunt' (ex:
            # voltar a cacar) tem um limite (%) que o usuario deve poder ajustar
            # direto aqui, sem precisar abrir o editor de rotinas.
            for step in routine["steps"]:
                if step.get("type") not in ("dom_threshold_click", "dom_resume_hunt"):
                    continue
                step_label = step.get("label") or ("Enviar" if step.get("type") == "dom_threshold_click" else "Voltar")
                ctk.CTkLabel(row, text="%", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="right", padx=(0, 6))
                threshold_var = tk.StringVar(value=str(step.get("threshold", "")))
                threshold_entry = ctk.CTkEntry(row, textvariable=threshold_var, width=45, fg_color=theme.BG)
                threshold_entry.pack(side="right", padx=(0, 2))
                ctk.CTkLabel(row, text=f"{step_label}:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(
                    side="right", padx=(10, 2)
                )

                def save_threshold(event=None, step=step, var=threshold_var):
                    try:
                        value = int(var.get())
                    except ValueError:
                        var.set(str(step.get("threshold", 0)))
                        return
                    step["threshold"] = value
                    bot.save_routines(self.routines)

                threshold_entry.bind("<Return>", save_threshold)
                threshold_entry.bind("<FocusOut>", save_threshold)

            # 'dom_boss_fight' tem uma lista grande de chefes pra marcar - abre a
            # tela de escolha direto daqui, sem precisar entrar em "Gerenciar Rotinas".
            for step in routine["steps"]:
                if step.get("type") != "dom_boss_fight":
                    continue

                def open_boss_picker(step=step):
                    def on_saved(bosses, slots_config):
                        step["bosses"] = bosses
                        bot.save_routines(self.routines)
                        self.settings["boss_slots"] = slots_config
                        bot.save_settings(self.settings)
                        bot.BOSS_SLOTS_MEMORY["blocked"] = None  # teto/modo novo: pode voltar a trocar
                        # a lista de chefes prontos so e reconsultada quando
                        # 'next_check' vence (pode ser so daqui a varias horas,
                        # calculado com a lista ANTERIOR) - sem resetar aqui, um
                        # chefe recem-marcado so seria percebido na proxima vez
                        # que o bot for reiniciado. Forca reconsulta imediata.
                        bot.BOSS_MEMORY["next_check"] = 0.0
                        bot.BOSS_MEMORY["display_next_check"] = 0.0
                        bot.BOSS_MEMORY["missed_estimate"] = False
                        self.rebuild_routine_rows()

                    def on_refresh(callback):
                        def worker():
                            try:
                                found = bot.fetch_boss_list(log=self.log)
                            except Exception as error:
                                self.log(f"  Erro ao capturar lista de Chefes: {error}")
                                found = []
                            callback(found)

                        threading.Thread(target=worker, daemon=True).start()

                    BossPicker(
                        self.root, step.get("bosses", []), on_saved=on_saved,
                        kills=bot.BOSS_KILLS_MEMORY, on_refresh=on_refresh,
                        slots_config=bot.boss_slots_config(),
                    )

                potion_items = bot.potion_config()["items"]
                potion_count = sum(1 for v in potion_items.values() if v["use_qty"] > 0 or v["buy_daily"])
                ctk.CTkButton(
                    row,
                    text=f"Poções ({potion_count})",
                    width=90,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=self.open_potion_picker,
                ).pack(side="right", padx=4)

                enabled_count = sum(1 for b in step.get("bosses", []) if b.get("enabled"))
                ctk.CTkButton(
                    row,
                    text=f"Chefes ({enabled_count})",
                    width=90,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=open_boss_picker,
                ).pack(side="right", padx=4)

            # 'dom_codex_campaign' tem a fila de entradas do Codex (por
            # recompensa) - mesma ideia dos chefes: tela propria.
            for step in routine["steps"]:
                if step.get("type") != "dom_codex_campaign":
                    continue
                ctk.CTkButton(
                    row,
                    text=f"Campanha ({len(step.get('queue', []))})",
                    width=100,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=lambda step=step: self.open_codex_campaign_picker(step),
                ).pack(side="right", padx=4)

            # 'dom_tier_sort' tambem tem uma lista grande de atributos de raridade
            # (Exp, Loot, Crit Chance...) pra marcar - mesmo padrao dos chefes.
            for step in routine["steps"]:
                if step.get("type") != "dom_tier_sort" or "attributes" not in step:
                    continue

                def open_attribute_picker(step=step):
                    def on_saved(attributes):
                        step["attributes"] = attributes
                        bot.save_routines(self.routines)
                        self.rebuild_routine_rows()

                    AttributePicker(self.root, step.get("attributes", []), on_saved=on_saved)

                enabled_attr_count = sum(1 for a in step.get("attributes", []) if a.get("enabled"))
                ctk.CTkButton(
                    row,
                    text=f"Atributos ({enabled_attr_count})",
                    width=100,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=open_attribute_picker,
                ).pack(side="right", padx=4)

                def save_match_mode(label, step=step):
                    step["match_mode"] = MATCH_MODE_BY_LABEL[label]
                    bot.save_routines(self.routines)

                ctk.CTkOptionMenu(
                    row,
                    values=list(MATCH_MODE_BY_LABEL.keys()),
                    command=save_match_mode,
                    width=150,
                    fg_color=theme.PANEL_ALT,
                    button_color=theme.PANEL_ALT,
                    button_hover_color=theme.BORDER,
                    text_color=theme.TEXT,
                    font=theme.FONT_BODY,
                    variable=tk.StringVar(value=MATCH_MODE_LABELS.get(step.get("match_mode", "rarity_only"))),
                ).pack(side="right", padx=4)

            # 'dom_auto_build' tem uma config por vocacao (modo/foco/XP/Loot/
            # pontos minimos) - mesmo padrao de tela separada dos chefes.
            for step in routine["steps"]:
                if step.get("type") != "dom_auto_build":
                    continue

                def open_build_config_picker(step=step):
                    def on_saved(configs):
                        step["configs"] = configs
                        bot.save_routines(self.routines)
                        # sem isso, a config nova so valeria no jogo no proximo
                        # intervalo da rotina (ate 5min) - forca rodar ja.
                        bot.FORCE_RUN_NOW.add("auto_build")
                        self.rebuild_routine_rows()

                    BuildConfigPicker(self.root, step.get("configs", []), on_saved=on_saved)

                enabled_voc_count = sum(1 for c in step.get("configs", []) if c.get("enabled"))
                ctk.CTkButton(
                    row,
                    text=f"Vocações ({enabled_voc_count})",
                    width=100,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=open_build_config_picker,
                ).pack(side="right", padx=4)

            rarity_items = []
            for step in routine["steps"]:
                if step.get("type") == "dom_tier_sort":
                    rarity_items.extend(step["tiers"])

            if rarity_items:
                ctk.CTkButton(
                    row,
                    text="▾ cores" if routine["id"] in self.expanded_routines else "▸ cores",
                    width=70,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=lambda routine_id=routine["id"]: self.toggle_color_panel(routine_id),
                ).pack(side="right", padx=4)

                if routine["id"] in self.expanded_routines:
                    colors_box = ctk.CTkFrame(container, fg_color=theme.BG, corner_radius=6)
                    colors_box.pack(fill="x", padx=(20, 0), pady=(2, 0))
                    for item in rarity_items:
                        item_var = tk.BooleanVar(value=item.get("enabled", True))
                        ctk.CTkCheckBox(
                            colors_box,
                            text=item["name"],
                            variable=item_var,
                            command=lambda i=item, v=item_var: self.toggle_color(i, v),
                            font=theme.FONT_BODY,
                            text_color=theme.TEXT,
                            fg_color=theme.ACCENT,
                            hover_color=theme.ACCENT_HOVER,
                        ).pack(anchor="w", padx=10, pady=3)

            # 'dom_guild_tasks' e 'dom_battlepass' tem 3 niveis de dificuldade
            # (Facil/Media/Dificil) que o usuario escolhe quais aceitar - mesmo
            # padrao das cores acima.
            difficulty_items = []
            for step in routine["steps"]:
                if step.get("type") in ("dom_guild_tasks", "dom_battlepass"):
                    difficulty_items.extend(step["difficulties"])

            if difficulty_items:
                ctk.CTkButton(
                    row,
                    text="▾ dificuldades" if routine["id"] in self.expanded_routines else "▸ dificuldades",
                    width=100,
                    fg_color=theme.PANEL_ALT,
                    hover_color=theme.BORDER,
                    text_color=theme.MUTED,
                    font=theme.FONT_BODY,
                    command=lambda routine_id=routine["id"]: self.toggle_color_panel(routine_id),
                ).pack(side="right", padx=4)

                if routine["id"] in self.expanded_routines:
                    diff_box = ctk.CTkFrame(container, fg_color=theme.BG, corner_radius=6)
                    diff_box.pack(fill="x", padx=(20, 0), pady=(2, 0))
                    for item in difficulty_items:
                        item_var = tk.BooleanVar(value=item.get("enabled", True))
                        ctk.CTkCheckBox(
                            diff_box,
                            text=item["label"],
                            variable=item_var,
                            command=lambda i=item, v=item_var: self.toggle_color(i, v),
                            font=theme.FONT_BODY,
                            text_color=theme.TEXT,
                            fg_color=theme.ACCENT,
                            hover_color=theme.ACCENT_HOVER,
                        ).pack(anchor="w", padx=10, pady=3)

            self.routine_rows[routine["id"]] = var

    def toggle_routine(self, routine_id):
        flag = self.flags[routine_id]
        if self.routine_rows[routine_id].get():
            flag.set()
        else:
            flag.clear()
        for routine in self.routines:
            if routine["id"] == routine_id:
                routine["enabled"] = self.routine_rows[routine_id].get()
        bot.save_routines(self.routines)
        if routine_id == "campanha_codex":
            # ligar/desligar a campanha: esquece a hunt-alvo (desligada, a hunt
            # padrao volta a ser a base) e, se ligou, ja avalia agora.
            bot.reset_campaign_memory()
            if flag.is_set():
                bot.FORCE_RUN_NOW.add("campanha_codex")

    def toggle_color_panel(self, routine_id):
        if routine_id in self.expanded_routines:
            self.expanded_routines.discard(routine_id)
        else:
            self.expanded_routines.add(routine_id)
        self.rebuild_routine_rows()

    def toggle_color(self, color, var):
        color["enabled"] = var.get()
        bot.save_routines(self.routines)

    # ---------- log / start / stop ----------

    def log(self, message):
        self.log_queue.put(message)
        self._append_to_log_file(message)

    def _append_to_log_file(self, message):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            # sufixo do navegador no nome - evita 2 instancias (Chrome/Opera)
            # rodando ao mesmo tempo intercalando linhas no mesmo arquivo.
            date_part = datetime.date.today().strftime("%Y-%m-%d")
            filename = f"bot_{self.current_profile}_{date_part}.log"
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            with open(os.path.join(LOG_DIR, filename), "a", encoding="utf-8") as file:
                file.write(f"[{timestamp}] {message}\n")
        except OSError:
            pass  # nao deixa um problema de disco derrubar o bot por causa do log

    def open_logs_folder(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(LOG_DIR)
        elif sys.platform == "darwin":
            subprocess.run(["open", LOG_DIR])
        else:
            subprocess.run(["xdg-open", LOG_DIR])

    @staticmethod
    def classify_log_level(message):
        """Classifica a mensagem de log em 'error'/'warning'/'normal' pra
        filtro na UI - reaproveita os MESMOS marcadores de texto que o bot ja
        usa nas mensagens (nao precisa mexer em cada log() do bot.py)."""
        lower = message.lower()
        if "erro" in lower:
            return "error"
        if "bloqueado" in lower:
            return "warning"
        return "normal"

    def apply_log_filter(self):
        """Reconstroi o LOG a partir do historico guardado (self.log_entries),
        mostrando so os niveis marcados nos checkboxes - chamado quando o
        usuario muda o filtro."""
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        for level, message in self.log_entries:
            if not self.log_filter_vars[level].get():
                continue
            start = self.log_box.index("end-1c")
            self.log_box.insert("end", message + "\n")
            if level in ("error", "warning"):
                self.log_box.tag_add(level, start, self.log_box.index("end-1c"))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_box_at_bottom(self):
        """True se o scroll do LOG ja estava no fim antes de inserir linha
        nova - so' nesse caso vale a pena rolar pra baixo de novo depois.
        Sem isso, quem tivesse subido pra ler o historico era jogado de volta
        pro fim toda vez que uma linha nova chegava (a cada 100ms)."""
        try:
            _, bottom = self.log_box.yview()
            return bottom >= 0.999
        except Exception:
            return True

    def poll_log_queue(self):
        if not self.log_queue.empty():
            was_at_bottom = self._log_box_at_bottom()
            self.log_box.configure(state="normal")
            while not self.log_queue.empty():
                message = self.log_queue.get_nowait()
                level = self.classify_log_level(message)
                # data/hora na tela - sem isso nao dava pra saber quando cada
                # linha aconteceu so' olhando o log ao vivo (so' o arquivo em
                # disco tinha hora, e so' hora - sem data, ja' que cada dia
                # tem seu proprio arquivo).
                timestamp = datetime.datetime.now().strftime("%d/%m %H:%M:%S")
                message = f"[{timestamp}] {message}"
                self.log_entries.append((level, message))
                if len(self.log_entries) > MAX_LOG_LINES:
                    del self.log_entries[:-MAX_LOG_LINES]

                if not self.log_filter_vars[level].get():
                    continue  # nivel filtrado - fica so no historico, nao aparece na tela

                start = self.log_box.index("end-1c")
                self.log_box.insert("end", message + "\n")
                if level in ("error", "warning"):
                    self.log_box.tag_add(level, start, self.log_box.index("end-1c"))

            # sem isso, o log cresce pra sempre numa sessao longa (rodando a noite
            # toda) e pode consumir memoria o suficiente pra travar o bot.
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > MAX_LOG_LINES:
                self.log_box.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")

            # so' rola pro fim se quem esta vendo ja estava no fim - senao
            # quem subiu pra analisar o historico fica lendo em paz, sem ser
            # jogado pra baixo a cada linha nova.
            if was_at_bottom:
                self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(100, self.poll_log_queue)

    def start(self):
        if self.thread and self.thread.is_alive():
            if self.pause_event.is_set():
                # estava pausado - so retoma no ponto em que estava (mesma
                # conexao, mesmo estado), nao reconecta nem reseta nada.
                self.pause_event.clear()
                self.status_label.configure(text="● RODANDO", text_color=theme.ACCENT)
                self.start_button.configure(state="disabled")
                self.pause_button.configure(state="normal")
                self.log("Bot retomado.")
            return

        self.stop_event.clear()
        self.pause_event.clear()
        self.thread = threading.Thread(
            target=bot.run,
            args=(self.stop_event, self.flags, self.routines, self.log),
            kwargs={"pause_event": self.pause_event},
            daemon=True,
        )
        self.thread.start()
        self.status_label.configure(text="● RODANDO", text_color=theme.ACCENT)
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        self.stop_button.configure(state="normal")

    def pause(self):
        if not (self.thread and self.thread.is_alive()):
            return
        self.pause_event.set()
        self.status_label.configure(text="● PAUSADO", text_color=theme.MUTED)
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled")
        self.log("Bot pausado (Iniciar retoma de onde parou).")

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()
        self.status_label.configure(text="● PARADO", text_color=theme.MUTED)
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")

    # ---------- fechar / minimizar pra bandeja (Windows) ----------

    def request_close(self):
        """Clique no X: pergunta se fecha de vez ou guarda na bandeja (so no
        Windows - no resto o X fecha direto, como sempre)."""
        if sys.platform != "win32":
            self.on_close()
            return
        if self.close_dialog is not None and self.close_dialog.winfo_exists():
            self.close_dialog.lift()
            return

        win = ctk.CTkToplevel(self.root)
        win.title("Fechar o bot")
        win.geometry("420x200")
        win.configure(fg_color=theme.BG)
        win.transient(self.root)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self.close_dialog = win

        def choose(action):
            win.destroy()
            if action == "tray":
                self.hide_to_tray()
            elif action == "quit":
                self.on_close()

        ctk.CTkLabel(
            win, text="O que deseja fazer?", font=theme.FONT_BODY, text_color=theme.TEXT,
        ).pack(padx=20, pady=(20, 6))
        running = self.thread is not None and self.thread.is_alive()
        hint = (
            "O bot está rodando: na bandeja ele continua; fechar de vez para tudo."
            if running else "Na bandeja o app fica escondido; fechar de vez encerra o programa."
        )
        ctk.CTkLabel(
            win, text=hint, font=theme.FONT_BODY, text_color=theme.MUTED, wraplength=380,
        ).pack(padx=20, pady=(0, 16))

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(pady=(0, 16))
        ctk.CTkButton(
            btn_row, text="Guardar na bandeja", command=lambda: choose("tray"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER, text_color="#04140a",
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_row, text="Fechar de vez", command=lambda: choose("quit"),
            fg_color=theme.PANEL_ALT, hover_color=theme.BORDER, text_color=theme.TEXT,
            border_width=1, border_color=theme.BORDER,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_row, text="Cancelar", command=lambda: choose("cancel"),
            fg_color="transparent", hover_color=theme.BORDER, text_color=theme.MUTED,
        ).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))

    def on_window_unmap(self, event):
        """Clique no '-' (minimizar): em vez de ir pra barra de tarefas, vai
        pra bandeja (escondido). Ignora o Unmap de widgets filhos e o do
        proprio 'withdraw'."""
        if event.widget is not self.root:
            return
        try:
            if self.root.state() == "iconic":
                self.root.after(50, self.hide_to_tray)
        except tk.TclError:
            pass

    def _load_tray_libs(self):
        # importado so aqui (e so no Windows) - o pystray do Mac exige o loop
        # principal e o PyInstaller do Mac nem precisa analisar isso.
        try:
            import importlib
            return importlib.import_module("pystray"), importlib.import_module("PIL.Image")
        except Exception as error:
            self.log(f"Bandeja indisponivel ({error}) - o app fica so minimizado.")
            return None

    def hide_to_tray(self):
        if sys.platform != "win32" or self.tray_icon is not None:
            return
        libs = self._load_tray_libs()
        if libs is None:
            return
        pystray, image_module = libs
        icon_path = os.path.join(bot.resource_dir(), "icon.ico")
        try:
            image = image_module.open(icon_path)
            label = bot.BROWSER_PROFILES[self.current_profile]["label"]
            menu = pystray.Menu(
                pystray.MenuItem("Abrir", lambda icon, item: self.tray_queue.put("open"), default=True),
                pystray.MenuItem(
                    lambda item: "Mostrar navegador" if bot.browser_hidden() else "Esconder navegador",
                    lambda icon, item: self.tray_queue.put("browser"),
                ),
                pystray.MenuItem("Fechar de vez", lambda icon, item: self.tray_queue.put("quit")),
            )
            self.tray_icon = pystray.Icon("BaiakIdleBot", image, f"BAIAK IDLE BOT — {label}", menu)
            self.tray_icon.run_detached()
        except Exception as error:
            self.tray_icon = None
            self.log(f"Nao consegui criar o icone da bandeja ({error}) - o app fica so minimizado.")
            return
        self.root.withdraw()

    def stop_tray_icon(self):
        icon, self.tray_icon = self.tray_icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def show_from_tray(self):
        self.stop_tray_icon()
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def poll_tray_queue(self):
        """Os cliques do icone da bandeja chegam de OUTRA thread (pystray) -
        o Tk so' pode ser mexido na thread principal, entao viram mensagens
        numa fila lida aqui."""
        try:
            while True:
                action = self.tray_queue.get_nowait()
                if action == "open":
                    self.show_from_tray()
                elif action == "browser":
                    self.toggle_browser_hidden()
                elif action == "quit":
                    self.on_close()
                    return
        except queue.Empty:
            pass
        self.root.after(300, self.poll_tray_queue)

    def on_close(self):
        if bot.browser_hidden():
            bot.set_browser_hidden(False, self.log)  # nunca deixa o navegador preso escondido
        self.stop_event.set()
        self.pause_event.clear()
        self.stop_tray_icon()
        self.root.destroy()


if __name__ == "__main__":
    # '--profile chrome'/'--profile opera' - usado pelo botao 'Nova Janela'
    # pra abrir a 2a instancia ja no navegador certo, sem precisar trocar
    # o seletor na mao.
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        requested = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if requested in bot.BROWSER_PROFILES:
            bot.set_active_profile(requested)

    root = ctk.CTk()
    BotGUI(root)
    root.mainloop()
