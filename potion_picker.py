import tkinter as tk

import customtkinter as ctk

import bot
import theme


class PotionPicker(ctk.CTkToplevel):
    """Escolhe quais pocoes de boost o bot usa antes de enfrentar os chefes e
    quais compra todo dia pra formar estoque. Tudo comeca DESMARCADO/zerado:
    nada e' comprado nem usado ate' o usuario salvar o que quer. Limites do
    jogo: 1 compra por tipo por dia no Mercador; cada pocao usada soma +30 min
    de boost (empilha); tambem caem de chefes."""

    def __init__(self, master, catalog, stock, config, suggestion, on_saved=None, on_refresh=None):
        super().__init__(master)
        self.title("Poções dos chefes")
        self.geometry("620x680")
        self.configure(fg_color=theme.BG)
        self.on_saved = on_saved
        self.on_refresh = on_refresh

        self.transient(master)
        self.lift()
        self.focus_force()

        self.catalog = catalog
        self.stock = stock or {}
        self.use_var = tk.BooleanVar(value=config["use_in_bosses"])
        self.qty_vars = {}
        self.buy_vars = {}
        for item in bot.POTION_CATALOG_DEFAULT:
            saved = config["items"].get(item["name"], {})
            self.qty_vars[item["name"]] = tk.StringVar(value=str(saved.get("use_qty", 0)))
            self.buy_vars[item["name"]] = tk.BooleanVar(value=bool(saved.get("buy_daily")))

        ctk.CTkLabel(
            self,
            text=(
                "Antes de enfrentar os chefes o bot usa as poções que você escolher (cada uso soma +30 min "
                "de boost na conta e empilha). Se faltar, compra o que o limite do jogo permitir: 1 por tipo "
                "por dia. Também dá pra comprar 1 por dia de cada tipo marcado pra juntar estoque."
            ),
            font=theme.FONT_BODY, text_color=theme.MUTED, justify="left", wraplength=580,
        ).pack(anchor="w", padx=12, pady=(12, 6))

        if suggestion:
            ctk.CTkLabel(
                self,
                text=(
                    f"Média das últimas {suggestion['runs']} sequência(s) de chefes: {suggestion['avg_minutes']:.0f} min. "
                    f"Com 15% de margem, {suggestion['needed']} poção(ões) de cada tipo cobrem a lista inteira."
                ),
                font=theme.FONT_BODY, text_color=theme.ACCENT, justify="left", wraplength=580,
            ).pack(anchor="w", padx=12, pady=(0, 6))

        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkCheckBox(
            top_row, text="Usar poções antes dos chefes", variable=self.use_var,
            font=theme.FONT_BODY, text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        ).pack(side="left")
        self.refresh_button = ctk.CTkButton(
            top_row, text="Atualizar", width=100, command=self.refresh,
            fg_color=theme.PANEL_ALT, hover_color=theme.BORDER, text_color=theme.TEXT, font=theme.FONT_BODY,
        )
        self.refresh_button.pack(side="right")

        self.summary_label = ctk.CTkLabel(
            self, text="", font=theme.FONT_BODY, text_color=theme.TEXT, justify="left", wraplength=580,
        )
        self.summary_label.pack(anchor="w", padx=12, pady=(0, 4))

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

    # ---------- dados ----------

    def qty(self, name):
        raw = self.qty_vars[name].get().strip()
        return int(raw) if raw.isdigit() else 0

    def refresh(self):
        if not self.on_refresh:
            return
        self.refresh_button.configure(state="disabled", text="Atualizando...")
        self.on_refresh(self.on_refreshed)

    def on_refreshed(self, catalog, stock):
        def apply():
            self.refresh_button.configure(state="normal", text="Atualizar")
            if catalog:
                self.catalog = catalog
                self.stock = stock or {}
            self.render_list()

        self.after(0, apply)

    # ---------- interface ----------

    def update_summary(self):
        daily = [n for n, v in self.buy_vars.items() if v.get()]
        price_of = {p["name"]: p["price"] for p in self.catalog}
        daily_cost = sum(price_of.get(n, 10000000) for n in daily)
        use_total = sum(self.qty(n) for n in self.qty_vars)
        parts = [f"Uso por sequência: {use_total} poção(ões)."]
        parts.append(
            f"Compra diária: {len(daily)} tipo(s) = {bot.format_gold(daily_cost)} de gold por dia."
            if daily else "Compra diária: nenhuma marcada."
        )
        self.summary_label.configure(text="  ".join(parts))

    def render_list(self):
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        by_name = {p["name"]: p for p in self.catalog}

        for default in bot.POTION_CATALOG_DEFAULT:
            name = default["name"]
            potion = by_name.get(name, default)
            box = ctk.CTkFrame(self.list_frame, fg_color=theme.PANEL_ALT, corner_radius=6)
            box.pack(fill="x", padx=4, pady=4)

            ctk.CTkLabel(
                box, text=f"{name.replace('potion of ', '').capitalize()}   -   {potion.get('desc') or default['desc']}",
                font=theme.FONT_BODY, text_color=theme.TEXT, anchor="w",
            ).pack(anchor="w", padx=10, pady=(8, 2))

            have = self.stock.get(name, 0)
            status = potion.get("status")
            today = {
                "limit": "hoje: já comprada (limite diário)",
                "buyable": "hoje: disponível pra comprar",
                "in_cart": "hoje: no carrinho",
            }.get(status, "")
            info = f"{bot.format_gold(potion.get('price') or default['price'])} cada   -   na mochila: {have}"
            if today:
                info += f"   -   {today}"
            ctk.CTkLabel(box, text=info, font=theme.FONT_BODY, text_color=theme.MUTED, anchor="w").pack(
                anchor="w", padx=10
            )

            controls = ctk.CTkFrame(box, fg_color="transparent")
            controls.pack(fill="x", padx=10, pady=(4, 8))
            ctk.CTkLabel(controls, text="Usar por sequência:", font=theme.FONT_BODY, text_color=theme.TEXT).pack(side="left")
            entry = ctk.CTkEntry(controls, textvariable=self.qty_vars[name], width=50, fg_color=theme.BG)
            entry.pack(side="left", padx=(6, 16))
            entry.bind("<KeyRelease>", lambda _e: self.update_summary())
            ctk.CTkCheckBox(
                controls, text="Comprar 1 por dia", variable=self.buy_vars[name], command=self.update_summary,
                font=theme.FONT_BODY, text_color=theme.TEXT, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            ).pack(side="left")

        self.update_summary()

    def save(self):
        config = {
            "use_in_bosses": bool(self.use_var.get()),
            "items": {
                name: {"use_qty": self.qty(name), "buy_daily": bool(self.buy_vars[name].get())}
                for name in self.qty_vars
            },
        }
        if self.on_saved:
            self.on_saved(config)
        self.destroy()
