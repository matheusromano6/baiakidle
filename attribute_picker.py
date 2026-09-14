import tkinter as tk

import customtkinter as ctk

import theme


class AttributePicker(ctk.CTkToplevel):
    """Lista os 32 atributos de raridade (Weapon Attack, Exp, Loot...) pra
    marcar quais valem a pena guardar mesmo que a cor/raridade do item nao
    esteja marcada - ex: guardar qualquer item com 'Exp', seja Uncommon ou
    Mythical. Cada atributo tem um nivel minimo (Lv.N do item) pra contar."""

    def __init__(self, master, attributes, on_saved=None):
        super().__init__(master)
        self.title("Escolher atributos")
        self.geometry("420x600")
        self.configure(fg_color=theme.BG)
        self.on_saved = on_saved

        self.transient(master)
        self.lift()
        self.focus_force()
        self.attributes = [dict(a) for a in attributes]

        ctk.CTkLabel(
            self,
            text="Guarda o item se ele tiver QUALQUER UM destes atributos\nno nivel minimo escolhido - mesmo que a raridade dele\nnao esteja marcada em 'cores'.",
            font=theme.FONT_BODY,
            text_color=theme.MUTED,
            justify="left",
        ).pack(fill="x", padx=12, pady=(12, 6))

        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(search_row, text="Buscar:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.render_list())
        ctk.CTkEntry(search_row, textvariable=self.search_var, fg_color=theme.PANEL_ALT).pack(
            side="left", fill="x", expand=True, padx=(6, 0)
        )

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

    def render_list(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        query = self.search_var.get().strip().lower()
        for attr in self.attributes:
            if query and query not in attr["name"].lower():
                continue

            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)

            var = tk.BooleanVar(value=attr.get("enabled", False))
            var.trace_add("write", lambda *_, a=attr, v=var: a.__setitem__("enabled", v.get()))
            ctk.CTkCheckBox(
                row,
                text=attr["name"],
                variable=var,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                text_color=theme.TEXT,
                font=theme.FONT_BODY,
            ).pack(side="left", padx=4, pady=3)

            ctk.CTkLabel(row, text="nível mín. Lv.", font=theme.FONT_BODY, text_color=theme.MUTED).pack(
                side="left", padx=(8, 2)
            )
            level_var = tk.StringVar(value=str(attr.get("min_level", 1)))

            def save_level(event=None, a=attr, v=level_var):
                try:
                    value = max(1, int(v.get()))
                except ValueError:
                    value = attr.get("min_level", 1)
                v.set(str(value))
                a["min_level"] = value

            level_entry = ctk.CTkEntry(row, textvariable=level_var, width=40, fg_color=theme.BG)
            level_entry.pack(side="left")
            level_entry.bind("<Return>", save_level)
            level_entry.bind("<FocusOut>", save_level)

    def save(self):
        if self.on_saved:
            self.on_saved(self.attributes)
        self.destroy()
