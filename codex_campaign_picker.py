import tkinter as tk

import customtkinter as ctk

import bot
import theme

ATTR_LABEL = dict(bot.CODEX_ATTRIBUTES)


def short_name(entry_name):
    return entry_name.split(":", 1)[1].strip() if ":" in entry_name else entry_name.strip()


def format_pct(value):
    return f"{value:.3f}".replace(".", ",")


class CodexCampaignPicker(ctk.CTkToplevel):
    """Escolhe quais entradas de Hunts do Codex fazer, agrupadas pela
    recompensa (atributo). Lista, do maior pro menor %, as entradas que dao o
    atributo escolhido; o usuario marca uma ou mais - dá pra trocar de
    atributo e marcar mais. A fila final segue a ordem em que os atributos
    foram marcados, e dentro de cada atributo o maior % primeiro (percentuais
    de atributos diferentes nao se comparam). Entradas bloqueadas pedem pagar
    o desbloqueio em gold: so' paga se o usuario marcar isso na entrada."""

    def __init__(self, master, entries, hunts, queue, on_saved=None, on_refresh=None):
        super().__init__(master)
        self.title("Campanha de Codex")
        self.geometry("640x700")
        self.configure(fg_color=theme.BG)
        self.on_saved = on_saved
        self.on_refresh = on_refresh

        self.transient(master)
        self.lift()
        self.focus_force()

        self.entries = entries
        self.hunts_by_name = {h["name"]: h for h in hunts}
        self.hunt_names = list(self.hunts_by_name)
        # ordem de marcacao + atributo em que cada uma foi marcada + se paga desbloqueio
        self.order = [item["name"] for item in queue]
        self.selected_attr = {item["name"]: item.get("attr", "") for item in queue}
        self.unlock_ok = {item["name"]: bool(item.get("unlock_ok")) for item in queue}
        self.known_costs = {item["name"]: item.get("unlock_cost", 0) for item in queue}

        labels = [label for _, label in bot.CODEX_ATTRIBUTES]
        first_attr = self.selected_attr[self.order[0]] if self.order else bot.CODEX_ATTRIBUTES[0][0]
        self.attr_var = tk.StringVar(value=ATTR_LABEL.get(first_attr, labels[0]))

        ctk.CTkLabel(
            self,
            text=(
                "Escolha as entradas do Codex a completar, agrupadas pela recompensa. O bot vai de hunt em "
                "hunt seguindo a fila (atributos na ordem em que voce marcar; dentro de cada um, maior % "
                "primeiro) e, quando acabar, volta pra hunt padrao (ou treino online se nao houver)."
            ),
            font=theme.FONT_BODY, text_color=theme.MUTED, justify="left", wraplength=600,
        ).pack(anchor="w", padx=12, pady=(12, 6))

        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(top_row, text="Recompensa:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        ctk.CTkOptionMenu(
            top_row, values=labels, variable=self.attr_var, command=lambda _: self.render_list(),
            fg_color=theme.PANEL_ALT, button_color=theme.PANEL_ALT, button_hover_color=theme.BORDER,
            text_color=theme.TEXT, font=theme.FONT_BODY, width=200,
        ).pack(side="left", padx=(6, 12))
        self.refresh_button = ctk.CTkButton(
            top_row, text="Atualizar lista", width=110, command=self.refresh,
            fg_color=theme.PANEL_ALT, hover_color=theme.BORDER, text_color=theme.TEXT, font=theme.FONT_BODY,
        )
        self.refresh_button.pack(side="left")

        self.summary_label = ctk.CTkLabel(
            self, text="", font=theme.FONT_BODY, text_color=theme.ACCENT, justify="left", wraplength=600,
        )
        self.summary_label.pack(fill="x", padx=12, pady=(0, 4))

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
        ctk.CTkButton(
            button_bar, text="Limpar fila", command=self.clear_queue, fg_color=theme.PANEL_ALT,
            text_color=theme.TEXT, border_width=1, border_color=theme.BORDER,
        ).pack(side="left")

        self.render_list()
        if not self.entries:
            self.refresh()  # primeira vez - ja tenta buscar sozinho

    # ---------- dados ----------

    def current_attr(self):
        label = self.attr_var.get()
        return next((value for value, text in bot.CODEX_ATTRIBUTES if text == label), "")

    def entry_by_name(self, name):
        return next((e for e in self.entries if e["name"] == name), None)

    def hunt_for(self, entry):
        return bot.match_hunt_for_codex_base(entry["base"], self.hunt_names)

    def hunt_level(self, hunt_name):
        return (self.hunts_by_name.get(hunt_name) or {}).get("level")

    def refresh(self):
        if not self.on_refresh:
            return
        self.refresh_button.configure(state="disabled", text="Atualizando...")
        self.on_refresh(self.on_refreshed)

    def on_refreshed(self, entries, hunts):
        # pode vir de outra thread (a leitura conecta no navegador em
        # background) - aplica na thread da interface.
        def apply():
            self.refresh_button.configure(state="normal", text="Atualizar lista")
            if entries:
                self.entries = entries
                self.hunts_by_name = {h["name"]: h for h in hunts}
                self.hunt_names = list(self.hunts_by_name)
            self.render_list()

        self.after(0, apply)

    # ---------- interface ----------

    def toggle_entry(self, name, attr, var):
        if var.get():
            if name not in self.order:
                self.order.append(name)
            self.selected_attr[name] = attr
        else:
            if name in self.order:
                self.order.remove(name)
            self.selected_attr.pop(name, None)
        self.update_summary()

    def toggle_unlock(self, name, cost, var):
        self.unlock_ok[name] = var.get()
        self.known_costs[name] = cost

    def clear_queue(self):
        self.order.clear()
        self.selected_attr.clear()
        self.render_list()

    def build_queue(self):
        group_order = []
        for name in self.order:
            attr = self.selected_attr.get(name, "")
            if attr not in group_order:
                group_order.append(attr)
        queue = []
        for attr in group_order:
            members = []
            for name in self.order:
                entry = self.entry_by_name(name)
                if entry is None or self.selected_attr.get(name) != attr:
                    continue
                members.append((entry["bonuses"].get(attr, 0.0), entry))
            members.sort(key=lambda pair: -pair[0])
            for pct, entry in members:
                hunt = self.hunt_for(entry)
                if not hunt:
                    continue
                name = entry["name"]
                queue.append({
                    "name": name,
                    "hunt": hunt,
                    "attr": attr,
                    "pct": pct,
                    "unlock_ok": bool(self.unlock_ok.get(name)),
                    "unlock_cost": entry.get("unlock_cost") or self.known_costs.get(name, 0),
                })
        return queue

    def update_summary(self):
        queue = self.build_queue()
        if not queue:
            self.summary_label.configure(text="Fila vazia - marque entradas abaixo.")
            return
        parts = []
        for q in queue:
            level = self.hunt_level(q["hunt"])
            level_text = f" - lvl {level}" if level is not None else ""
            parts.append(f"{short_name(q['name'])} ({ATTR_LABEL.get(q['attr'], q['attr'])} {format_pct(q['pct'])}%{level_text})")
        self.summary_label.configure(text=f"Fila ({len(queue)}): " + "  >  ".join(parts))

    def render_list(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()

        if not self.entries:
            ctk.CTkLabel(
                self.list_frame, text="Lista vazia - clique em 'Atualizar lista' com o jogo aberto.",
                font=theme.FONT_BODY, text_color=theme.MUTED, wraplength=560, justify="left",
            ).pack(anchor="w", padx=8, pady=8)
            self.update_summary()
            return

        attr = self.current_attr()
        rows = [e for e in self.entries if attr in e["bonuses"] and e["status"] != "done"]
        rows.sort(key=lambda e: -e["bonuses"][attr])
        if not rows:
            ctk.CTkLabel(
                self.list_frame, text="Nenhuma entrada pendente com essa recompensa.",
                font=theme.FONT_BODY, text_color=theme.MUTED,
            ).pack(anchor="w", padx=8, pady=8)

        for entry in rows:
            name = entry["name"]
            hunt = self.hunt_for(entry)
            locked = entry["status"] == "locked"
            queued_elsewhere = name in self.selected_attr and self.selected_attr[name] != attr

            box = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            box.pack(fill="x", padx=4, pady=3)

            var = tk.BooleanVar(value=name in self.selected_attr and self.selected_attr[name] == attr)
            level = self.hunt_level(hunt) if hunt else None
            level_text = f"   lvl {level}" if level is not None else ""
            title = f"{format_pct(entry['bonuses'][attr])}%   {short_name(name)}   [{entry['progress']:.0f}%]{level_text}"
            check = ctk.CTkCheckBox(
                box, text=title, variable=var,
                command=lambda n=name, a=attr, v=var: self.toggle_entry(n, a, v),
                font=theme.FONT_BODY, text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            )
            check.pack(anchor="w", padx=4)
            if not hunt or queued_elsewhere:
                check.configure(state="disabled")

            notes = [entry["bonus_text"]]
            if not hunt:
                notes.append("sem hunt correspondente na lista do jogo")
            if hunt and (self.hunts_by_name.get(hunt) or {}).get("locked"):
                notes.append(f"hunt '{hunt}' (lvl {level}) ainda bloqueada no jogo")
            if queued_elsewhere:
                notes.append(f"ja na fila em '{ATTR_LABEL.get(self.selected_attr[name], '')}'")
            ctk.CTkLabel(
                box, text="  -  ".join(notes),
                font=theme.FONT_BODY, text_color=theme.MUTED, justify="left", wraplength=500, anchor="w",
            ).pack(anchor="w", padx=30)

            if locked:
                cost = entry.get("unlock_cost", 0)
                unlock_var = tk.BooleanVar(value=bool(self.unlock_ok.get(name)))
                ctk.CTkCheckBox(
                    box,
                    text=f"Bloqueada - pagar desbloqueio automaticamente ({bot.format_gold(cost)} de gold)",
                    variable=unlock_var,
                    command=lambda n=name, c=cost, v=unlock_var: self.toggle_unlock(n, c, v),
                    font=theme.FONT_BODY, text_color=theme.WARNING, fg_color=theme.WARNING,
                    hover_color=theme.ACCENT_HOVER,
                ).pack(anchor="w", padx=30, pady=(2, 0))

        self.update_summary()

    def save(self):
        if self.on_saved:
            self.on_saved(self.build_queue())
        self.destroy()
