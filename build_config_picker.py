import tkinter as tk

import customtkinter as ctk

import theme

MODE_OPTIONS = [("DPS", "dps"), ("Tank", "tank"), ("Off-tank", "offtank"), ("PvP", "pvp")]

# Mapeado ao vivo no site otimizador (baiakidle-build-optimizer.pages.dev):
# as opcoes de Foco (e se 'pegar XP'/'pegar Loot' sao escolhas de verdade)
# mudam por Vocacao+Modo - PvP sempre trava tudo numa unica opcao; fora
# disso cada vocacao tem seu proprio conjunto (ex: Knight nao tem elemento
# nenhum em Tank igual ao DPS - usa Fogo/Morte/Gelo/Energia; Monk so tem
# Fisico em qualquer modo). Os rotulos sao os mesmos textos exibidos no site.
VOCATION_MODE_FOCUS = {
    "Knight": {
        "dps": [("elemental_weapon", "Elemental"), ("physical", "Físico")],
        "tank": [("tank", "Geral"), ("tank_fire", "Fogo"), ("tank_death", "Morte"), ("tank_ice", "Gelo"), ("tank_energy", "Energia")],
        "offtank": [("offtank_physical", "Físico + sobrevivência")],
        "pvp": [("pvp_core", "Sem elemento")],
    },
    "Paladin": {
        "dps": [("holy", "Sagrado"), ("physical", "Físico")],
        "tank": [("holy", "Sagrado"), ("physical", "Físico")],
        "offtank": [("offtank_holy", "Sagrado + sobrevivência"), ("offtank_physical", "Físico + sobrevivência")],
        "pvp": [("pvp_core", "Sem elemento")],
    },
    "Sorcerer": {
        "dps": [("spell_crit", "Sem elemento"), ("energy", "Energia"), ("fire", "Fogo"), ("death", "Morte")],
        "tank": [("spell_crit", "Sem elemento"), ("energy", "Energia"), ("fire", "Fogo"), ("death", "Morte")],
        "offtank": [("offtank_energy", "Energia + sobrevivência"), ("offtank_fire", "Fogo + sobrevivência"), ("offtank_death", "Morte + sobrevivência")],
        "pvp": [("pvp_core", "Sem elemento")],
    },
    "Druid": {
        "dps": [("spell_crit", "Sem elemento"), ("ice", "Gelo"), ("earth", "Terra"), ("hybrid", "Gelo + terra")],
        "tank": [("spell_crit", "Sem elemento"), ("ice", "Gelo"), ("earth", "Terra"), ("hybrid", "Gelo + terra")],
        "offtank": [("offtank_ice", "Gelo + sobrevivência"), ("offtank_earth", "Terra + sobrevivência"), ("offtank_hybrid", "Gelo + terra + sobrevivência")],
        "pvp": [("pvp_core", "Sem elemento")],
    },
    "Monk": {
        "dps": [("physical", "Físico")],
        "tank": [("physical", "Físico")],
        "offtank": [("offtank_physical", "Físico + sobrevivência")],
        "pvp": [("pvp_core", "Sem elemento")],
    },
}

# (XP disponível, Loot disponível) por vocacao+modo - tambem mapeado ao vivo.
VOCATION_MODE_TOGGLES = {
    "Knight": {"dps": (False, False), "tank": (False, False), "offtank": (False, False), "pvp": (False, False)},
    "Paladin": {"dps": (True, True), "tank": (True, True), "offtank": (True, True), "pvp": (False, False)},
    "Sorcerer": {"dps": (True, False), "tank": (True, False), "offtank": (True, False), "pvp": (False, False)},
    "Druid": {"dps": (True, True), "tank": (True, True), "offtank": (True, True), "pvp": (False, False)},
    "Monk": {"dps": (True, True), "tank": (True, True), "offtank": (True, True), "pvp": (False, False)},
}

# So o Sorcerer (nas vocacoes ja conferidas ao vivo - Knight/Paladin/Druid/Monk
# nao tem isso em nenhuma combinacao) libera combinar mais elementos quando o
# foco primario e um elemento "puro" (nao 'Sem elemento'): apos escolher um,
# o site libera um foco secundario com os elementos QUE SOBRARAM, e depois de
# escolher esse, um terciario com o ultimo que sobrou. Rotulos identicos aos
# do site (minusculos mesmo, diferente do foco primario).
MULTI_ELEMENT_POOL = {
    "Sorcerer": {
        "dps": [("energy", "energia"), ("fire", "fogo"), ("death", "morte")],
        "tank": [("energy", "energia"), ("fire", "fogo"), ("death", "morte")],
    },
}
NO_SECONDARY_LABEL = "Nenhum"


class BuildConfigPicker(ctk.CTkToplevel):
    """Configuracao da build automatica (arvore de talentos) por vocacao -
    modo, foco, XP/Loot obrigatorios e quantos pontos disponiveis esperar
    antes de reaplicar. As opcoes de Foco e a disponibilidade de XP/Loot
    mudam dinamicamente com o Modo, igual ao site otimizador (ve
    VOCATION_MODE_FOCUS/VOCATION_MODE_TOGGLES). O bot detecta sozinho qual
    vocacao esta ativa (o personagem logado agora) e usa a config
    correspondente."""

    def __init__(self, master, configs, on_saved=None):
        super().__init__(master)
        self.title("Build automática por vocação")
        self.geometry("460x620")
        self.configure(fg_color=theme.BG)
        self.on_saved = on_saved

        self.transient(master)
        self.lift()
        self.focus_force()
        self.configs = [dict(c) for c in configs]

        ctk.CTkLabel(
            self,
            text="Atenção: aplica pontos de talento de verdade no personagem.\nSó ligue a vocação depois de conferir o resultado uma vez.",
            font=theme.FONT_BODY,
            text_color=theme.DANGER,
            justify="left",
        ).pack(fill="x", padx=12, pady=(12, 6))

        scroll = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL, corner_radius=8)
        scroll.pack(fill="both", expand=True, padx=12, pady=6)

        for config in self.configs:
            self._build_vocation_section(scroll, config)

        button_bar = ctk.CTkFrame(self, fg_color="transparent")
        button_bar.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            button_bar, text="Salvar", command=self.save, fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER, text_color="#04140a",
        ).pack(side="right")
        ctk.CTkButton(
            button_bar, text="Cancelar", command=self.destroy, fg_color=theme.PANEL_ALT,
            text_color=theme.TEXT, border_width=1, border_color=theme.BORDER,
        ).pack(side="right", padx=(0, 8))

    def _build_vocation_section(self, parent, config):
        box = ctk.CTkFrame(parent, fg_color=theme.PANEL_ALT, corner_radius=6)
        box.pack(fill="x", padx=4, pady=4)

        focus_options = VOCATION_MODE_FOCUS.get(config["vocation"], {})
        toggle_options = VOCATION_MODE_TOGGLES.get(config["vocation"], {})

        enabled_var = tk.BooleanVar(value=config.get("enabled", False))
        enabled_var.trace_add("write", lambda *_, c=config, v=enabled_var: c.__setitem__("enabled", v.get()))
        ctk.CTkCheckBox(
            box,
            text=config["vocation"],
            variable=enabled_var,
            font=theme.FONT_HEADER,
            text_color=theme.TEXT,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
        ).pack(anchor="w", padx=10, pady=(8, 4))

        row1 = ctk.CTkFrame(box, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row1, text="Modo:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        mode_var = tk.StringVar(value=next((label for label, value in MODE_OPTIONS if value == config.get("mode")), "DPS"))

        row_focus = ctk.CTkFrame(box, fg_color="transparent")
        row_focus.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row_focus, text="Foco:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        focus_var = tk.StringVar()
        focus_menu = ctk.CTkOptionMenu(
            row_focus, values=["-"], variable=focus_var,
            fg_color=theme.BG, button_color=theme.PANEL_ALT,
            button_hover_color=theme.BORDER, width=260,
        )
        focus_menu.pack(side="left", padx=(6, 0))

        # foco secundario/terciario: so aparecem quando a combinacao atual
        # libera combinar mais elementos (ex: Sorcerer + DPS + 'Fogo') - ficam
        # ocultos (nem ocupam espaco na tela) no resto do tempo, igual ao site
        # que so mostra esses campos quando fazem sentido.
        row_secondary = ctk.CTkFrame(box, fg_color="transparent")
        ctk.CTkLabel(row_secondary, text="+ Elemento:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        secondary_var = tk.StringVar()
        secondary_menu = ctk.CTkOptionMenu(
            row_secondary, values=["-"], variable=secondary_var,
            fg_color=theme.BG, button_color=theme.PANEL_ALT,
            button_hover_color=theme.BORDER, width=160,
        )
        secondary_menu.pack(side="left", padx=(6, 0))

        row_tertiary = ctk.CTkFrame(box, fg_color="transparent")
        ctk.CTkLabel(row_tertiary, text="+ Elemento:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        tertiary_var = tk.StringVar()
        tertiary_menu = ctk.CTkOptionMenu(
            row_tertiary, values=["-"], variable=tertiary_var,
            fg_color=theme.BG, button_color=theme.PANEL_ALT,
            button_hover_color=theme.BORDER, width=160,
        )
        tertiary_menu.pack(side="left", padx=(6, 0))

        multi_pool = MULTI_ELEMENT_POOL.get(config["vocation"], {})

        def refresh_tertiary(mode_value, secondary_value, keep_tertiary=None):
            pool = multi_pool.get(mode_value, [])
            remaining = [(value, label) for value, label in pool if value != config.get("focus") and value != secondary_value]
            if not secondary_value or not remaining:
                row_tertiary.pack_forget()
                config["tertiary_focus"] = ""
                return
            row_tertiary.pack(fill="x", padx=10, pady=2, after=row_secondary)
            options = [("", NO_SECONDARY_LABEL)] + remaining
            labels = [label for _, label in options]
            tertiary_menu.configure(values=labels)
            current_label = next((label for value, label in options if value == keep_tertiary), labels[0])
            tertiary_var.set(current_label)
            config["tertiary_focus"] = next((value for value, label in options if label == current_label), "")

        def on_tertiary_change(choice, c=config):
            mode_value = c.get("mode", "dps")
            pool = multi_pool.get(mode_value, [])
            options = [("", NO_SECONDARY_LABEL)] + pool
            c["tertiary_focus"] = next((value for value, label in options if label == choice), "")

        tertiary_menu.configure(command=on_tertiary_change)

        def refresh_secondary(mode_value, focus_value, keep_secondary=None, keep_tertiary=None):
            pool = multi_pool.get(mode_value, [])
            remaining = [(value, label) for value, label in pool if value != focus_value]
            if focus_value not in [v for v, _ in pool] or not remaining:
                row_secondary.pack_forget()
                row_tertiary.pack_forget()
                config["secondary_focus"] = ""
                config["tertiary_focus"] = ""
                return
            row_secondary.pack(fill="x", padx=10, pady=2, after=row_focus)
            options = [("", NO_SECONDARY_LABEL)] + remaining
            labels = [label for _, label in options]
            secondary_menu.configure(values=labels)
            current_label = next((label for value, label in options if value == keep_secondary), labels[0])
            secondary_var.set(current_label)
            secondary_value = next((value for value, label in options if label == current_label), "")
            config["secondary_focus"] = secondary_value
            refresh_tertiary(mode_value, secondary_value, keep_tertiary=keep_tertiary)

        def on_secondary_change(choice, c=config):
            mode_value = c.get("mode", "dps")
            focus_value = c.get("focus", "")
            pool = multi_pool.get(mode_value, [])
            remaining = [(value, label) for value, label in pool if value != focus_value]
            options = [("", NO_SECONDARY_LABEL)] + remaining
            secondary_value = next((value for value, label in options if label == choice), "")
            c["secondary_focus"] = secondary_value
            refresh_tertiary(mode_value, secondary_value)

        secondary_menu.configure(command=on_secondary_change)

        row2 = ctk.CTkFrame(box, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=2)
        xp_var = tk.BooleanVar(value=config.get("force_xp", False))
        xp_check = ctk.CTkCheckBox(
            row2, text="Forçar XP", variable=xp_var, font=theme.FONT_BODY, text_color=theme.TEXT,
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        xp_check.pack(side="left")

        loot_var = tk.BooleanVar(value=config.get("force_loot", False))
        loot_check = ctk.CTkCheckBox(
            row2, text="Forçar Loot", variable=loot_var, font=theme.FONT_BODY, text_color=theme.TEXT,
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        )
        loot_check.pack(side="left", padx=(16, 0))

        def refresh_for_mode(mode_value, keep_focus=None, keep_secondary=None, keep_tertiary=None):
            """Reconstroi as opcoes de Foco e liga/desliga XP/Loot conforme o
            Modo escolhido - igual ao comportamento do site otimizador."""
            options = focus_options.get(mode_value, [("", "-")])
            labels = [label for _, label in options]
            focus_menu.configure(values=labels)
            if keep_focus is not None:
                current_label = next((label for value, label in options if value == keep_focus), labels[0])
            else:
                current_label = labels[0]
            focus_var.set(current_label)
            config["focus"] = next((value for value, label in options if label == current_label), options[0][0])
            # so uma opcao disponivel (ex: Off-tank/PvP travam nisso) - sem
            # escolha de verdade, deixa igual ao site: campo desabilitado.
            focus_menu.configure(state="normal" if len(options) > 1 else "disabled")

            xp_enabled, loot_enabled = toggle_options.get(mode_value, (False, False))
            xp_check.configure(state="normal" if xp_enabled else "disabled")
            loot_check.configure(state="normal" if loot_enabled else "disabled")
            if not xp_enabled:
                xp_var.set(False)
                config["force_xp"] = False
            if not loot_enabled:
                loot_var.set(False)
                config["force_loot"] = False

            refresh_secondary(mode_value, config["focus"], keep_secondary=keep_secondary, keep_tertiary=keep_tertiary)

        def on_focus_change(choice, c=config, options_ref=focus_options):
            options = options_ref.get(c.get("mode", "dps"), [])
            focus_value = next((value for value, label in options if label == choice), c.get("focus", ""))
            c["focus"] = focus_value
            refresh_secondary(c.get("mode", "dps"), focus_value)

        focus_menu.configure(command=on_focus_change)

        def save_mode(choice, c=config):
            new_mode = next(value for label, value in MODE_OPTIONS if label == choice)
            c["mode"] = new_mode
            refresh_for_mode(new_mode, keep_focus=c.get("focus"))

        ctk.CTkOptionMenu(
            row1, values=[label for label, _ in MODE_OPTIONS], variable=mode_var,
            command=save_mode, fg_color=theme.BG, button_color=theme.PANEL_ALT,
            button_hover_color=theme.BORDER, width=110,
        ).pack(side="left", padx=(6, 0))

        def on_xp_toggle(*_a, c=config, v=xp_var):
            if str(xp_check.cget("state")) == "normal":
                c["force_xp"] = v.get()

        def on_loot_toggle(*_a, c=config, v=loot_var):
            if str(loot_check.cget("state")) == "normal":
                c["force_loot"] = v.get()

        xp_var.trace_add("write", on_xp_toggle)
        loot_var.trace_add("write", on_loot_toggle)

        # estado inicial - respeita o modo/foco/foco extra ja salvos, dentro
        # do que a combinacao atual realmente permite.
        refresh_for_mode(
            config.get("mode", "dps"),
            keep_focus=config.get("focus"),
            keep_secondary=config.get("secondary_focus"),
            keep_tertiary=config.get("tertiary_focus"),
        )

        row3 = ctk.CTkFrame(box, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkLabel(
            row3, text="Atualizar a cada quantos pontos disponíveis:", font=theme.FONT_BODY, text_color=theme.MUTED
        ).pack(side="left")
        min_points_var = tk.StringVar(value=str(config.get("min_points", 5)))

        def save_min_points(event=None, c=config, v=min_points_var):
            try:
                value = max(1, int(v.get()))
            except ValueError:
                value = c.get("min_points", 5)
            v.set(str(value))
            c["min_points"] = value

        min_points_entry = ctk.CTkEntry(row3, textvariable=min_points_var, width=45, fg_color=theme.BG)
        min_points_entry.pack(side="left", padx=(6, 0))
        min_points_entry.bind("<Return>", save_min_points)
        min_points_entry.bind("<FocusOut>", save_min_points)

    def save(self):
        if self.on_saved:
            self.on_saved(self.configs)
        self.destroy()
