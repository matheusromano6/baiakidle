import tkinter as tk

import customtkinter as ctk

import theme


class RiftPicker(ctk.CTkToplevel):
    """Escolhe em quais hunts (versao Nightmare) a rotina do Rift junta Death
    Essence - na ordem da lista - e a meta por hora de cada uma. O bot passa
    pra proxima com 1h+ e >= 85% da meta (ou 2h de qualquer jeito)."""

    def __init__(self, master, hunts, on_saved=None):
        super().__init__(master)
        self.title("Rift: hunts Nightmare")
        self.geometry("380x620")
        self.configure(fg_color=theme.BG)
        self.transient(master)
        self.lift()
        self.focus_force()
        self.on_saved = on_saved
        self.hunts = [dict(h) for h in hunts]

        ctk.CTkLabel(
            self, text="Marque as hunts (na ordem) e a meta de Death Essence/h:",
            font=theme.FONT_BODY, text_color=theme.MUTED, wraplength=340,
        ).pack(padx=12, pady=(12, 4), anchor="w")

        frame = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL, corner_radius=8)
        frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.rows = []
        for hunt in self.hunts:
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", padx=6, pady=2)
            enabled = tk.BooleanVar(value=hunt.get("enabled", False))
            ctk.CTkCheckBox(
                row, text=hunt["name"], variable=enabled, font=theme.FONT_BODY, text_color=theme.TEXT,
                fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            ).pack(side="left")
            rate = tk.StringVar(value=str(hunt.get("rate", 0)))
            ctk.CTkLabel(row, text="/h", font=theme.FONT_BODY, text_color=theme.MUTED).pack(side="right")
            ctk.CTkEntry(row, textvariable=rate, width=50, fg_color=theme.PANEL_ALT).pack(side="right", padx=4)
            self.rows.append((hunt, enabled, rate))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            bar, text="Salvar", command=self.save, fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER, text_color="#04140a",
        ).pack(side="right")
        ctk.CTkButton(
            bar, text="Cancelar", command=self.destroy, fg_color=theme.PANEL_ALT,
            text_color=theme.TEXT, border_width=1, border_color=theme.BORDER,
        ).pack(side="right", padx=(0, 8))

    def save(self):
        for hunt, enabled, rate in self.rows:
            hunt["enabled"] = bool(enabled.get())
            digits = "".join(ch for ch in rate.get() if ch.isdigit())
            hunt["rate"] = int(digits) if digits else hunt.get("rate", 0)
        if self.on_saved:
            self.on_saved(self.hunts)
        self.destroy()
