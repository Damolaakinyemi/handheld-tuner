"""The overlay window: a small always-on-top Tk window that draws what OverlayModel says."""
from __future__ import annotations

import queue
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Tuple

from . import winapi
from .client import EventReader
from .input import DEFAULT_CHORD, HoldTrigger, chord_pressed
from .model import OverlayModel, Panel, View

KEY = "#010101"  # drawn as transparent on Windows, so the rounded corners are see-through
BG, BORDER = "#141414", "#3b3b3b"
TEXT, MUTED, TOAST = "#ffffff", "#b8b6ab", "#b5d4f4"
CHIP, CHIP_HIT = "#2f2f2f", "#185fa5"
TONES = {"ok": "#5dcaa5", "warn": "#ef9f27", "muted": "#888780", "error": "#e24b4a"}
LINE = "#5dcaa5"
TICK_MS = 100


def _rrect(c: tk.Canvas, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
           x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, **kw)


class Renderer:
    def __init__(self, root: tk.Tk, scale: float) -> None:
        self.s = scale
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=KEY)
        self.canvas.pack()
        family = "Segoe UI" if sys.platform == "win32" else "Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans"

        def font(px: float, bold: bool = False) -> tkfont.Font:
            return tkfont.Font(family=family, size=-max(1, round(px * scale)), weight="bold" if bold else "normal")

        self.f_main, self.f_big, self.f_small = font(13), font(26, True), font(11)

    def px(self, v: float) -> int:
        return round(v * self.s)

    def draw(self, view: View) -> Tuple[int, int]:
        self.canvas.delete("all")
        w, h = self._panel(view) if view.panel else self._pill(view)
        self.canvas.config(width=w, height=h)
        return w, h

    def _pill(self, v: View) -> Tuple[int, int]:
        c, px = self.canvas, self.px
        pad_x, pad_y, dot = px(12), px(7), px(4)
        main_h, small_h = self.f_main.metrics("linespace"), self.f_small.metrics("linespace")
        text_w = self.f_main.measure(v.headline)
        w = pad_x * 2 + dot * 2 + px(7) + text_w
        h = pad_y * 2 + main_h
        if v.toast:
            w = max(w, pad_x * 2 + self.f_small.measure(v.toast))
            h += small_h + px(1)
        _rrect(c, 1, 1, w - 1, h - 1, px(10), fill=BG, outline=BORDER)
        cy = pad_y + main_h / 2
        c.create_oval(pad_x, cy - dot, pad_x + 2 * dot, cy + dot, fill=TONES[v.tone], outline="")
        c.create_text(pad_x + 2 * dot + px(7), cy, text=v.headline, anchor="w", font=self.f_main,
                      fill=TEXT if v.tone != "muted" else MUTED)
        if v.toast:
            c.create_text(pad_x, pad_y + main_h + px(1) + small_h / 2, text=v.toast, anchor="w",
                          font=self.f_small, fill=TOAST)
        return w, h

    def _chip(self, x: int, y: int, text: str, hit: bool) -> int:
        c, px = self.canvas, self.px
        w = self.f_small.measure(text) + px(14)
        h = self.f_small.metrics("linespace") + px(4)
        _rrect(c, x, y, x + w, y + h, px(6), fill=CHIP_HIT if hit else CHIP, outline="")
        c.create_text(x + w / 2, y + h / 2, text=text, font=self.f_small, fill=TEXT)
        return w

    def _panel(self, v: View) -> Tuple[int, int]:
        c, px = self.canvas, self.px
        p: Panel = v.panel
        w, pad = px(244), px(12)
        small_h = self.f_small.metrics("linespace")
        y = pad
        row1 = self.f_big.metrics("linespace")
        # height is known up front so the background can be drawn first
        spark_h = px(30)
        h = pad + row1 + px(2) + small_h + px(8) + (small_h + px(4)) + px(8) + spark_h + px(8) \
            + small_h + px(4) + small_h + pad
        _rrect(c, 1, 1, w - 1, h - 1, px(12), fill=BG, outline=BORDER)

        dot = px(5)
        cy = y + row1 / 2
        c.create_oval(pad, cy - dot, pad + 2 * dot, cy + dot, fill=TONES[p.tone], outline="")
        x = pad + 2 * dot + px(8)
        c.create_text(x, cy, text=p.fps, anchor="w", font=self.f_big, fill=TEXT)
        x += self.f_big.measure(p.fps) + px(5)
        c.create_text(x, cy + px(4), text="fps", anchor="w", font=self.f_main, fill=MUTED)
        c.create_text(w - pad, cy, text=p.badge or p.status, anchor="e", font=self.f_small,
                      fill=TONES["warn"] if p.badge else TONES[p.tone])
        y += row1 + px(2)

        c.create_text(pad, y + small_h / 2, text=p.detail, anchor="w", font=self.f_small, fill=MUTED)
        y += small_h + px(8)

        cx = pad
        cx += self._chip(cx, y, p.tdp, p.tdp_changed) + px(6)
        self._chip(cx, y, p.res, p.res_changed)
        y += small_h + px(4) + px(8)

        self._spark(p.spark, pad, y, w - 2 * pad, spark_h)
        y += spark_h + px(8)

        c.create_text(pad, y + small_h / 2, text=p.decision, anchor="w", font=self.f_small, fill=TEXT)
        y += small_h + px(4)
        c.create_text(pad, y + small_h / 2, text=p.goal, anchor="w", font=self.f_small, fill=MUTED)
        return w, h

    def _spark(self, values: Tuple[float, ...], x: int, y: int, w: int, h: int) -> None:
        c = self.canvas
        c.create_line(x, y + h, x + w, y + h, fill=BORDER)
        if len(values) < 2:
            return
        top = max(16.0, max(values))
        step = w / (len(values) - 1)
        pts = []
        for i, v in enumerate(values):
            pts += [x + i * step, y + h - (v / top) * (h - 2)]
        c.create_line(*pts, fill=LINE, width=max(1, self.px(1.5)))


class OverlayApp:
    def __init__(self, port: int, token_file, corner: str, scale: float, margin: int, quiet: bool,
                 start_expanded: bool, hold: float, use_input: bool, click_toggle: bool) -> None:
        winapi.enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("Tuner overlay")
        self.corner, self.margin = corner, margin
        screen_h = self.root.winfo_screenheight()
        self.scale = scale * max(1.0, screen_h / 1000)  # designed at 1000 px tall; grows on the 1600 px Legion Go
        self.model = OverlayModel(quiet=quiet, start_expanded=start_expanded)
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.reader = EventReader(self.events, port, token_file)
        self.hold = HoldTrigger(hold)
        self.xinput = winapi.XInputReader() if use_input else None
        self.use_input = use_input
        self._last_view = None
        self._last_top = 0.0

        self._setup_window()
        self.renderer = Renderer(self.root, self.scale)
        if click_toggle:
            self.root.bind("<Button-1>", lambda _e: self.model.toggle())
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _setup_window(self) -> None:
        r = self.root
        r.overrideredirect(True)
        r.configure(bg=KEY)
        r.attributes("-topmost", True)
        try:
            if sys.platform == "win32":
                r.attributes("-transparentcolor", KEY)
            elif sys.platform == "darwin":
                r.attributes("-transparent", True)
                r.configure(bg="systemTransparent")
        except tk.TclError:
            pass
        r.update_idletasks()
        winapi.make_click_through(r)

    def run(self) -> None:
        self.reader.start()
        self.root.after(TICK_MS, self._tick)
        try:
            self.root.mainloop()
        finally:
            self.reader.stop()

    def close(self) -> None:
        self.reader.stop()
        self.root.destroy()

    def _tick(self) -> None:
        now = time.monotonic()
        try:
            while True:
                self.model.apply(self.events.get_nowait(), now)
        except queue.Empty:
            pass

        if self.use_input and self.hold.update(self._input_active(), now):
            self.model.toggle()

        view = self.model.view(now)
        if view != self._last_view:
            self._render(view)
            self._last_view = view
        if now - self._last_top > 2.0:
            winapi.keep_on_top(self.root)
            self._last_top = now
        self.root.after(TICK_MS, self._tick)

    def _input_active(self) -> bool:
        if winapi.shortcut_down():
            return True
        return bool(self.xinput and chord_pressed(self.xinput.buttons(), DEFAULT_CHORD))

    def _render(self, view: View) -> None:
        if not view.visible:
            self.root.withdraw()
            return
        w, h = self.renderer.draw(view)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        m = round(self.margin * self.scale)
        x = m if "left" in self.corner else sw - w - m
        y = m if "top" in self.corner else sh - h - m
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.deiconify()
        winapi.make_click_through(self.root)
