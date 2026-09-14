import tkinter as tk

import customtkinter as ctk

import theme

FILTER_LABELS = ["Todas", "EXP", "LOOT", "Concluídas", "Pendentes"]


class HuntPicker(ctk.CTkToplevel):
    """Escolhe a 'hunt padrao' - a fase pra onde o bot sempre volta quando
    termina tasks de guild, fica sem hunt ativa apos um chefe etc, e nao tem
    nada mais prioritario pra fazer. A lista de hunts (nome, nivel, se rende
    mais EXP/LOOT, se ja foi concluida) vem do JOGO (botao 'Atualizar lista'),
    nao e fixa no codigo - pode mudar com o progresso da conta. Os filtros
    reproduzem os mesmos que o proprio jogo mostra na tela de Hunts."""

    def __init__(self, master, hunts, selected, on_saved=None, on_refresh=None):
        super().__init__(master)
        self.title("Hunt padrão")
        self.geometry("440x620")
        self.configure(fg_color=theme.BG)
        self.on_saved = on_saved
        self.on_refresh = on_refresh

        self.transient(master)
        self.lift()
        self.focus_force()

        self.hunts = [dict(h) for h in hunts]  # [{"name","locked","level","lean","done","done_count"}]
        self.selected_var = tk.StringVar(value=selected or "")
        self.filter_var = tk.StringVar(value="Todas")

        ctk.CTkLabel(
            self,
            text="Quando terminar tasks de guild, ficar sem hunt ativa apos\num chefe etc, o bot sempre volta pra essa hunt.",
            font=theme.FONT_BODY,
            text_color=theme.MUTED,
            justify="left",
        ).pack(fill="x", padx=12, pady=(12, 6))

        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(top_row, text="Buscar:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.render_list())
        ctk.CTkEntry(top_row, textvariable=self.search_var, fg_color=theme.PANEL_ALT).pack(
            side="left", fill="x", expand=True, padx=(6, 6)
        )
        self.refresh_button = ctk.CTkButton(
            top_row,
            text="Atualizar lista",
            width=110,
            command=self.refresh,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
        )
        self.refresh_button.pack(side="left")

        # filtro por categoria - igual as que o jogo mostra na tela de Hunts.
        filter_row = ctk.CTkFrame(self, fg_color="transparent")
        filter_row.pack(fill="x", padx=12, pady=(0, 6))
        self.filter_menu = ctk.CTkOptionMenu(
            filter_row,
            values=FILTER_LABELS,
            variable=self.filter_var,
            command=lambda _: self.render_list(),
            fg_color=theme.PANEL_ALT,
            button_color=theme.PANEL_ALT,
            button_hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
        )
        self.filter_menu.pack(side="left")

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL, corner_radius=8)
        self.list_frame.pack(fill="both", expand=True, padx=12, pady=6)

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

        self.render_list()
        if not self.hunts:
            self.refresh()  # lista vazia (primeira vez) - ja tenta buscar sozinho

    def refresh(self):
        if not self.on_refresh:
            return
        self.refresh_button.configure(state="disabled", text="Atualizando...")
        self.on_refresh(self.on_refreshed)

    def on_refreshed(self, hunts):
        # pode ser chamado de outra thread (a busca conecta no navegador em
        # background) - agenda a atualizacao de verdade na thread da UI.
        def apply():
            self.hunts = hunts
            self.refresh_button.configure(state="normal", text="Atualizar lista")
            self.render_list()

        self.after(0, apply)

    def hunt_matches_filter(self, hunt):
        category = self.filter_var.get()
        if category == "EXP":
            return hunt.get("lean") == "exp"
        if category == "LOOT":
            return hunt.get("lean") == "loot"
        if category == "Concluídas":
            return bool(hunt.get("done"))
        if category == "Pendentes":
            return not hunt.get("done") and not hunt.get("locked")
        return True  # "Todas"

    def render_list(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        query = self.search_var.get().strip().lower()

        none_row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        none_row.pack(fill="x", padx=4, pady=(2, 8))
        ctk.CTkRadioButton(
            none_row,
            text="Nenhuma (volta pra ultima hunt conhecida, como antes)",
            variable=self.selected_var,
            value="",
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
        ).pack(side="left", padx=4, pady=3)

        if not self.hunts:
            ctk.CTkLabel(
                self.list_frame,
                text="Lista vazia - clique em 'Atualizar lista' com o jogo aberto.",
                font=theme.FONT_BODY,
                text_color=theme.MUTED,
                wraplength=380,
                justify="left",
            ).pack(anchor="w", padx=8, pady=8)
            return

        shown = 0
        for hunt in self.hunts:
            name = hunt["name"]
            if query and query not in name.lower():
                continue
            if not self.hunt_matches_filter(hunt):
                continue
            shown += 1

            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)

            parts = [name]
            if hunt.get("level") is not None:
                parts.append(f"lvl {hunt['level']}")
            if hunt.get("lean"):
                parts.append(hunt["lean"].upper())
            if hunt.get("done"):
                count = hunt.get("done_count", 0)
                parts.append(f"✓ {count}x" if count else "✓")
            if hunt.get("locked"):
                parts.append("(bloqueada)")
            label = " — ".join(parts)

            ctk.CTkRadioButton(
                row,
                text=label,
                variable=self.selected_var,
                value=name,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                text_color=theme.MUTED if hunt.get("locked") else theme.TEXT,
                font=theme.FONT_BODY,
            ).pack(side="left", padx=4, pady=3)

        if shown == 0:
            ctk.CTkLabel(
                self.list_frame,
                text="Nenhuma hunt bate com a busca/filtro atual.",
                font=theme.FONT_BODY,
                text_color=theme.MUTED,
            ).pack(anchor="w", padx=8, pady=8)

    def save(self):
        if self.on_saved:
            self.on_saved(self.selected_var.get())
        self.destroy()
