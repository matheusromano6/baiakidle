import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

import theme

IMPORT_TOOLTIP_TEXT = (
    "Importa um arquivo .txt com um chefe por linha, na ordem que devem\n"
    "ser enfrentados - o bot escolhe o PRIMEIRO da lista que estiver\n"
    "pronto no momento, entao a ordem das linhas e a prioridade.\n\n"
    "Termine a linha com * pra marcar 'precisa de Stone Skin Amulet'\n"
    "(ex: 'Ferumbras *') - sem o *, o chefe importa sem essa marcacao.\n\n"
    "Linhas em branco sao ignoradas. Nomes que nao baterem com nenhum\n"
    "chefe conhecido (grafia diferente, texto extra na linha etc) sao\n"
    "avisados no final e ficam de fora - o resto da lista importa normal."
)


class _Tooltip:
    """Balao simples que aparece ao passar o mouse por cima de um widget e
    some ao tirar - usado pra explicar o formato esperado do arquivo de
    importacao sem poluir a tela com um texto sempre visivel."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event=None):
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip,
            text=self.text,
            justify="left",
            background=theme.PANEL_ALT,
            foreground=theme.TEXT,
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
            font=(theme.FONT_FAMILY, 11),
        ).pack()

    def hide(self, _event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class BossPicker(ctk.CTkToplevel):
    """Lista os chefes (nome + level) pra marcar quais o bot deve enfrentar
    quando estiverem prontos (nivel suficiente e fora da recarga de 13h - o
    proprio jogo cuida disso, o bot so escolhe entre os marcados). A ordem da
    lista e a prioridade de luta - entre varios prontos ao mesmo tempo, o
    primeiro da lista e escolhido. Pode ser reordenada importando um .txt
    (botao 'Importar', ve o tooltip)."""

    def __init__(self, master, bosses, on_saved=None, kills=None, on_refresh=None):
        super().__init__(master)
        self.title("Escolher chefes")
        self.geometry("420x600")
        self.configure(fg_color=theme.BG)
        self.on_saved = on_saved
        self.on_refresh = on_refresh

        # sem isso a janela pode abrir atras da principal em vez de por cima.
        self.transient(master)
        self.lift()
        self.focus_force()
        self.bosses = [dict(b) for b in bosses]
        self.check_vars = []
        # placar de vitorias por chefe (lido do Bosstiary do jogo) - ajuda o
        # usuario a decidir manter ou tirar um chefe pela eficiencia real.
        self.kills = kills or {}

        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(search_row, text="Buscar:", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.render_list())
        ctk.CTkEntry(search_row, textvariable=self.search_var, fg_color=theme.PANEL_ALT).pack(
            side="left", fill="x", expand=True, padx=(6, 6)
        )
        import_button = ctk.CTkButton(
            search_row,
            text="Importar",
            width=90,
            command=self.import_from_file,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
        )
        import_button.pack(side="left")
        _Tooltip(import_button, IMPORT_TOOLTIP_TEXT)
        self.refresh_button = ctk.CTkButton(
            search_row,
            text="Atualizar",
            width=80,
            command=self.refresh,
            fg_color=theme.PANEL_ALT,
            hover_color=theme.BORDER,
            text_color=theme.TEXT,
            font=theme.FONT_BODY,
        )
        self.refresh_button.pack(side="left", padx=(6, 0))
        _Tooltip(
            self.refresh_button,
            "Busca no jogo chefes NOVOS (ex: apos uma atualizacao) e\n"
            "adiciona no fim da lista, desmarcados. A lista salva e' usada\n"
            "sempre - so' vai ao jogo quando voce clica aqui.",
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
        self.check_vars = []

        query = self.search_var.get().strip().lower()
        for boss in self.bosses:
            if query and query not in boss["name"].lower():
                continue
            var = tk.BooleanVar(value=boss.get("enabled", False))
            # atualiza o boss na hora (nao so ao salvar) - senao trocar o filtro de
            # busca antes de salvar perderia a marcacao que ainda nao foi sincronizada.
            var.trace_add("write", lambda *_, b=boss, v=var: b.__setitem__("enabled", v.get()))
            kills = self.kills.get(boss["name"])
            kills_text = f" — {kills} vitória(s)" if kills is not None else ""

            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=3)

            ctk.CTkCheckBox(
                row,
                text=f"{boss['name']} — lvl {boss['level']}{kills_text}",
                variable=var,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                text_color=theme.TEXT,
                font=theme.FONT_BODY,
            ).pack(side="left")
            self.check_vars.append((boss, var))

            # 'stone_skin': troca o amuleto do EK pro Stone Skin Amulet so' pra
            # esse chefe (ver equip_boss_amulet em bot.py) - reverte pro que
            # estava antes assim que o combate termina. So' pros chefes que de
            # fato precisam de mais resistencia, marcados um por um aqui.
            ss_var = tk.BooleanVar(value=boss.get("stone_skin", False))
            ss_var.trace_add("write", lambda *_, b=boss, v=ss_var: b.__setitem__("stone_skin", v.get()))
            ss_check = ctk.CTkCheckBox(
                row,
                text="🛡 Stone Skin",
                variable=ss_var,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                text_color=theme.MUTED,
                font=theme.FONT_BODY,
                width=1,
            )
            ss_check.pack(side="right")
            _Tooltip(
                ss_check,
                "Antes de enfrentar esse chefe, troca o amuleto\n"
                "(emergencial e padrão) do EK pro Stone Skin Amulet\n"
                "no Helper - fica trocado ate o FIM de toda a\n"
                "sequencia de chefes prontos (nao so' desse aqui),\n"
                "depois volta pro que estava antes. So' funciona se\n"
                "o item estiver na pouch/mochila; se nao tiver, segue\n"
                "sem trocar.",
            )

    def refresh(self):
        if not self.on_refresh:
            return
        self.refresh_button.configure(state="disabled", text="Buscando...")
        self.on_refresh(self.on_refreshed)

    def on_refreshed(self, found):
        # chamado de outra thread (a busca conecta no navegador) - aplica na thread da UI.
        def apply():
            self.refresh_button.configure(state="normal", text="Atualizar")
            if not found:
                messagebox.showwarning("Atualizar lista", "Nao consegui ler a lista de chefes do jogo.", parent=self)
                return
            known = {b["name"]: b for b in self.bosses}
            new_names = []
            for entry in found:
                existing = known.get(entry["name"])
                if existing is None:
                    self.bosses.append({"name": entry["name"], "level": entry.get("level") or 0, "enabled": False})
                    new_names.append(entry["name"])
                elif entry.get("level") is not None:
                    existing["level"] = entry["level"]
            self.render_list()
            if new_names:
                messagebox.showinfo(
                    "Atualizar lista",
                    f"{len(new_names)} chefe(s) novo(s) adicionado(s) no fim da lista (desmarcados):\n\n"
                    + "\n".join(new_names) + "\n\nClique em Salvar pra guardar.",
                    parent=self,
                )
            else:
                messagebox.showinfo("Atualizar lista", "Nenhum chefe novo - a lista ja esta atualizada.", parent=self)

        self.after(0, apply)

    def import_from_file(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="Importar lista de chefes",
            filetypes=[("Texto", "*.txt"), ("Todos os arquivos", "*.*")],
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as file:
                lines = file.readlines()
        except Exception as error:
            messagebox.showerror("Erro ao importar", f"Não consegui ler o arquivo:\n{error}", parent=self)
            return

        by_lower = {b["name"].strip().lower(): b for b in self.bosses}
        matched, not_found, seen = [], [], set()
        for raw_line in lines:
            name = raw_line.strip()
            if not name:
                continue  # linha em branco - ignora
            # '*' no final da linha marca 'precisa de Stone Skin Amulet' (ve
            # IMPORT_TOOLTIP_TEXT) - tira antes de comparar o nome.
            needs_stone_skin = name.endswith("*")
            if needs_stone_skin:
                name = name[:-1].strip()
            key = name.lower()
            if key in seen:
                continue  # nome repetido no arquivo - ja processado, ignora
            boss = by_lower.get(key)
            if boss is None:
                not_found.append(name)
                continue
            seen.add(key)
            # arquivo de importacao e autoritativo pra essa marcacao tambem -
            # linha sem '*' = nao precisa, mesmo que estivesse marcado antes.
            boss["stone_skin"] = needs_stone_skin
            matched.append(boss)

        if not matched:
            messagebox.showwarning(
                "Nada importado",
                "Nenhuma linha do arquivo bateu com um chefe conhecido."
                + ("\n\nNão reconhecido(s):\n" + "\n".join(not_found) if not_found else ""),
                parent=self,
            )
            return

        matched_ids = {id(b) for b in matched}
        for boss in self.bosses:
            boss["enabled"] = id(boss) in matched_ids
        # reordena: os importados primeiro (na ordem do arquivo - essa ordem
        # e a prioridade de luta), o resto da lista continua depois, sem marcar.
        rest = [b for b in self.bosses if id(b) not in matched_ids]
        self.bosses = matched + rest
        self.render_list()

        summary = f"{len(matched)} chefe(s) importado(s) e ordenado(s) pela lista do arquivo."
        if not_found:
            summary += f"\n\n{len(not_found)} linha(s) nao reconhecida(s) (confira a grafia):\n" + "\n".join(
                not_found
            )
            messagebox.showwarning("Importação concluída com avisos", summary, parent=self)
        else:
            messagebox.showinfo("Importação concluída", summary, parent=self)

    def save(self):
        if self.on_saved:
            self.on_saved(self.bosses)
        self.destroy()
