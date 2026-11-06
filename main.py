"""
Text Grabber - select and copy text ANYWHERE on your Windows screen.

How it works
  1. You press the hotkey (default Ctrl+Alt+S) while "Selection mode" is enabled
     (tray icon -> "Selection mode enabled").
  2. The program takes a screenshot of all monitors and runs the built-in
     Windows OCR on it (works for ANY app, image, video, button, remote desktop...).
  3. A frozen full-screen overlay appears. Drag with the mouse to select text
     like in a normal text editor. The selection is copied to the clipboard.

Controls inside the overlay
  Drag            select text (reading order)
  Ctrl + Drag     select a rectangular area
  Double-click    select one word
  Ctrl + C        copy again
  Esc / Right-click   close overlay
"""
import asyncio
import ctypes
import queue
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox

# --- DPI awareness must be set BEFORE any window / screenshot is created ----
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import keyboard  # noqa: E402
import pystray  # noqa: E402
from PIL import Image, ImageDraw, ImageGrab, ImageTk  # noqa: E402

# --- Windows OCR (WinRT) -----------------------------------------------------
import winrt.windows.foundation.collections  # noqa: F401,E402  (needed for iterating results)
from winrt.windows.globalization import Language  # noqa: E402
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap  # noqa: E402
from winrt.windows.media.ocr import OcrEngine  # noqa: E402
from winrt.windows.storage.streams import DataWriter  # noqa: E402

HOTKEY = "ctrl+alt+s"
HINT = "Drag = select  |  Ctrl+Drag = box  |  Double-click = word  |  Esc = close"


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int


# ----------------------------------------------------------------------------
# OCR
# ----------------------------------------------------------------------------
def make_engine():
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is not None:
        return engine
    for tag in ("en-US", "en-GB", "de-DE"):
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
            if engine is not None:
                return engine
        except Exception:
            pass
    return None


async def _recognize(engine, img):
    r, g, b, a = img.convert("RGBA").split()
    bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()
    writer = DataWriter()
    writer.write_bytes(bytearray(bgra))
    buf = writer.detach_buffer()
    bmp = SoftwareBitmap.create_copy_from_buffer(
        buf, BitmapPixelFormat.BGRA8, img.width, img.height
    )
    return await engine.recognize_async(bmp)


def run_ocr(engine, img):
    maxd = OcrEngine.max_image_dimension
    s = min(2.0, maxd / max(img.size))  # upscale x2 helps small text; downscale if huge
    work = img if s == 1.0 else img.resize(
        (int(img.width * s), int(img.height * s)), Image.LANCZOS
    )
    result = asyncio.run(_recognize(engine, work))
    words = []
    for li, line in enumerate(result.lines):
        for w in line.words:
            r = w.bounding_rect
            words.append(
                Word(w.text, r.x / s, r.y / s, (r.x + r.width) / s, (r.y + r.height) / s, li)
            )
    return words


# ----------------------------------------------------------------------------
# Overlay
# ----------------------------------------------------------------------------
class Overlay:
    def __init__(self, root, img, words, origin, joiner, on_close):
        self.root, self.words, self.joiner, self.on_close = root, words, joiner, on_close
        self.sel = []
        self.anchor = None
        self.box = False
        self.start = (0, 0)

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{img.width}x{img.height}{origin[0]:+d}{origin[1]:+d}")
        self.canvas = tk.Canvas(
            self.win, width=img.width, height=img.height,
            highlightthickness=0, bd=0, cursor="xterm",
        )
        self.canvas.pack(fill="both", expand=True)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        # hint bar
        cx = img.width // 2
        t = self.canvas.create_text(cx, 22, text=HINT, fill="white", font=("Segoe UI", 11))
        x0, y0, x1, y1 = self.canvas.bbox(t)
        bg = self.canvas.create_rectangle(x0 - 12, y0 - 6, x1 + 12, y1 + 6,
                                          fill="#111827", outline="#3b82f6")
        self.canvas.tag_lower(bg, t)

        c = self.canvas
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<Motion>", self.on_hover)
        c.bind("<Button-3>", lambda e: self.close())
        for w in (self.win, c):
            w.bind("<Escape>", lambda e: self.close())
            w.bind("<Control-c>", lambda e: self.copy())
        self.win.update_idletasks()
        self.win.focus_force()
        self.canvas.focus_set()

    # --- helpers
    def nearest(self, x, y, limit=None):
        best, bd = None, 1e18
        for i, w in enumerate(self.words):
            dx = max(w.x0 - x, 0, x - w.x1)
            dy = max(w.y0 - y, 0, y - w.y1)
            d = dx * dx + dy * dy
            if d < bd:
                best, bd = i, d
        if limit is not None and bd > limit * limit:
            return None
        return best

    def redraw(self):
        self.canvas.delete("sel")
        for i in self.sel:
            w = self.words[i]
            self.canvas.create_rectangle(
                w.x0 - 1, w.y0 - 1, w.x1 + 1, w.y1 + 1,
                fill="#3b82f6", outline="", stipple="gray50", tags="sel",
            )

    def text(self):
        out, last = [], None
        for i in sorted(self.sel):
            w = self.words[i]
            if last is not None and w.line != last:
                out.append("\n")
            elif last is not None:
                out.append(self.joiner)
            out.append(w.text)
            last = w.line
        return "".join(out)

    def copy(self):
        if not self.sel:
            return
        txt = self.text()
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.root.update()
        self.toast(f"Copied {len(self.sel)} word(s)")

    def toast(self, msg):
        self.canvas.delete("toast")
        cx = self.canvas.winfo_width() // 2
        t = self.canvas.create_text(cx, 62, text=msg, fill="white",
                                    font=("Segoe UI", 11, "bold"), tags="toast")
        x0, y0, x1, y1 = self.canvas.bbox(t)
        bg = self.canvas.create_rectangle(x0 - 12, y0 - 5, x1 + 12, y1 + 5,
                                          fill="#16a34a", outline="", tags="toast")
        self.canvas.tag_lower(bg, t)
        self.win.after(1500, lambda: self.canvas.delete("toast"))

    # --- events
    def on_hover(self, e):
        self.canvas.delete("hover")
        i = self.nearest(e.x, e.y, 6)
        if i is not None:
            w = self.words[i]
            self.canvas.create_rectangle(w.x0 - 1, w.y0 - 1, w.x1 + 1, w.y1 + 1,
                                         outline="#f59e0b", width=1, tags="hover")

    def on_press(self, e):
        self.box = bool(e.state & 0x4)  # Ctrl held
        self.start = (e.x, e.y)
        self.sel = []
        self.anchor = None if self.box else self.nearest(e.x, e.y, 80)
        if self.anchor is not None:
            self.sel = [self.anchor]
        self.canvas.delete("box")
        self.redraw()

    def on_drag(self, e):
        if self.box:
            x0, y0 = self.start
            lo_x, hi_x = sorted((x0, e.x))
            lo_y, hi_y = sorted((y0, e.y))
            self.canvas.delete("box")
            self.canvas.create_rectangle(lo_x, lo_y, hi_x, hi_y, outline="#3b82f6",
                                         dash=(4, 3), tags="box")
            self.sel = [
                i for i, w in enumerate(self.words)
                if w.x1 >= lo_x and w.x0 <= hi_x and w.y1 >= lo_y and w.y0 <= hi_y
            ]
        elif self.anchor is not None:
            cur = self.nearest(e.x, e.y)
            lo, hi = sorted((self.anchor, cur))
            self.sel = list(range(lo, hi + 1))
        self.redraw()

    def on_release(self, e):
        self.canvas.delete("box")
        self.copy()

    def on_double(self, e):
        i = self.nearest(e.x, e.y, 30)
        if i is not None:
            self.sel = [i]
            self.redraw()
            self.copy()

    def close(self):
        try:
            self.win.destroy()
        finally:
            self.on_close()


# ----------------------------------------------------------------------------
# App (tray + hotkey + main loop)
# ----------------------------------------------------------------------------
def make_icon_image():
    img = Image.new("RGBA", (64, 64), (17, 24, 39, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((8, 8, 56, 56), outline=(59, 130, 246, 255), width=4)
    d.text((22, 20), "T", fill=(255, 255, 255, 255))
    d.line((16, 46, 48, 46), fill=(245, 158, 11, 255), width=4)
    return img


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.q = queue.Queue()
        self.enabled = True
        self.overlay = None
        self.engine = make_engine()
        self.icon = pystray.Icon(
            "TextGrabber", make_icon_image(), "Text Grabber",
            pystray.Menu(
                pystray.MenuItem(
                    "Selection mode enabled",
                    lambda i, it: self.q.put("toggle"),
                    checked=lambda it: self.enabled,
                ),
                pystray.MenuItem(f"Capture now ({HOTKEY})",
                                 lambda i, it: self.q.put("capture")),
                pystray.MenuItem("Quit", lambda i, it: self.q.put("quit")),
            ),
        )

    def capture(self):
        if not self.enabled or self.overlay is not None:
            return
        if self.engine is None:
            messagebox.showerror(
                "Text Grabber",
                "No Windows OCR language found.\n"
                "Install a language pack: Settings > Time & language > Language & region.",
            )
            return
        img = ImageGrab.grab(all_screens=True)
        origin = (
            ctypes.windll.user32.GetSystemMetrics(76),  # virtual screen left
            ctypes.windll.user32.GetSystemMetrics(77),  # virtual screen top
        )
        try:
            words = run_ocr(self.engine, img)
        except Exception as ex:  # keep the tray app alive
            messagebox.showerror("Text Grabber", f"OCR failed:\n{ex}")
            return
        tag = (self.engine.recognizer_language.language_tag or "").lower()
        joiner = "" if tag[:2] in ("ja", "zh") else " "
        self.overlay = Overlay(self.root, img, words, origin, joiner, self.overlay_closed)

    def overlay_closed(self):
        self.overlay = None

    def poll(self):
        try:
            while True:
                cmd = self.q.get_nowait()
                if cmd == "capture":
                    self.capture()
                elif cmd == "toggle":
                    self.enabled = not self.enabled
                    self.icon.update_menu()
                elif cmd == "quit":
                    self.icon.stop()
                    keyboard.unhook_all()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def run(self):
        keyboard.add_hotkey(HOTKEY, lambda: self.q.put("capture"))
        self.icon.run_detached()
        self.root.after(40, self.poll)
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
