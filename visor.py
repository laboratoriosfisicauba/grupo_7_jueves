#!/usr/bin/env python3
"""
Visor de mediciones de muones para la terminal (pensado para la Raspberry Pi).

Lee los archivos que escribe drs_exam (run_*_idxN[_selected].txt) y dibuja las
formas de onda de los 3 canales con caracteres braille. Solo usa la biblioteca
estandar de Python, asi que funciona por SSH sin entorno grafico.

Uso:
    python3 visor.py                  # recorre ./medidas
    python3 visor.py medidas/polar_0_deg
    python3 visor.py archivo.txt
    python3 visor.py medidas/nueva -f # modo en vivo: sigue el archivo mas nuevo

Teclas:
    <- / ->    evento anterior / siguiente     PgUp/PgDn  -/+ 10 eventos
    Home/End   primer / ultimo evento          [ / ]      archivo anterior / siguiente
    1 2 3      mostrar/ocultar canal           s          superpuesto / separado
    + / -      zoom en el tiempo (centrado en el pulso de u1)
    f          modo en vivo on/off             u          linea de umbral on/off
    r          releer archivos                 q          salir
"""
import argparse
import curses
import locale
import os
import re
import time
from datetime import datetime

KTHR_DEFAULT = -120.0   # mV, igual que kThr en src/drs_exam.cpp
POLL_SECS = 0.5
CHANNELS = ("u1", "u2", "u3")

BRAILLE_BASE = 0x2800
# Bit de cada punto braille segun (columna, fila) dentro de la celda de 2x4
BRAILLE_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

# Caracteres de los ejes: (unicode, ascii)
GLYPHS = {"vert": ("│", "|"), "corner": ("└", "+"), "horiz": ("─", "-"),
          "on": ("■", "#"), "off": ("□", "."), "live": ("●", "*")}

EVENT_RE = re.compile(r"^Event #(-?\d+)(?:\s+ts=(\S+ \S+))?")


def natural_key(s):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- lectura

class EventFile:
    """Indexa los eventos de un archivo de forma incremental (sirve mientras se escribe)."""

    def __init__(self, path):
        self.path = path
        self.events = []        # (offset, numero, ts) de eventos completos
        self._pos = 0           # hasta donde se leyo el archivo
        self._pending = None    # evento empezado pero sin terminar
        self._has_data = False

    def scan(self):
        """Lee lo nuevo del archivo. Devuelve True si aparecieron eventos."""
        n_before = len(self.events)
        try:
            size = os.path.getsize(self.path)
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if size < self._pos:            # archivo truncado/reescrito
            self.__init__(self.path)
        with open(self.path, "rb") as f:
            f.seek(self._pos)
            while True:
                offset = f.tell()
                raw = f.readline()
                if not raw or not raw.endswith(b"\n"):
                    break               # linea incompleta: se relee en el proximo scan
                self._pos = f.tell()
                line = raw.strip()
                if line.startswith(b"Event #"):
                    self._close_pending()
                    m = EVENT_RE.match(line.decode("utf-8", "replace"))
                    num = int(m.group(1)) if m else -1
                    ts = m.group(2) if m and m.group(2) else None
                    self._pending = (offset, num, ts)
                    self._has_data = False
                elif not line:
                    self._close_pending()
                elif self._pending and line[:1] in b"-0123456789":
                    self._has_data = True
        # Si el archivo no cambia hace rato, el ultimo evento (sin linea vacia) esta completo
        if self._pending and time.time() - mtime > 2.0:
            self._close_pending()
        return len(self.events) > n_before

    def _close_pending(self):
        if self._pending and self._has_data:
            self.events.append(self._pending)
        self._pending = None
        self._has_data = False

    def load(self, k):
        """Devuelve dict con t, u1, u2, u3 (listas de float) del evento k."""
        offset = self.events[k][0]
        t, u = [], ([], [], [])
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            f.readline()                # encabezado "Event #..."
            for line in f:
                s = line.strip()
                if not s or s.startswith("Event #"):
                    break
                parts = s.split()
                try:
                    vals = [float(p) for p in parts]
                except ValueError:
                    continue            # lineas de encabezado de columnas, "Board #", etc.
                if len(vals) >= 8:      # formato viejo: t1 u1 t2 u2 t3 u3 t4 u4
                    vals = [vals[0], vals[1], vals[3], vals[5]]
                if len(vals) < 4:
                    continue
                t.append(vals[0])
                for c in range(3):
                    u[c].append(vals[1 + c])
        return {"t": t, "u1": u[0], "u2": u[1], "u3": u[2]}

    def rate(self, last_n=50):
        """Eventos por segundo segun los timestamps de los ultimos eventos."""
        stamps = [e[2] for e in self.events[-last_n:] if e[2]]
        if len(stamps) < 2:
            return None
        try:
            t0 = datetime.strptime(stamps[0], "%Y-%m-%d %H:%M:%S.%f")
            t1 = datetime.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return None
        dt = (t1 - t0).total_seconds()
        return (len(stamps) - 1) / dt if dt > 0 else None


def list_files(root):
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith(".txt"):
                found.append(os.path.join(dirpath, n))
    return sorted(found, key=natural_key)


# ---------------------------------------------------------------- dibujo

class Canvas:
    """Grilla de pixeles; en modo braille cada caracter tiene 2x4 pixeles."""

    def __init__(self, cols, rows, ascii_mode=False):
        self.cw, self.ch = (1, 1) if ascii_mode else (2, 4)
        self.cols, self.rows = cols, rows
        self.w, self.h = cols * self.cw, rows * self.ch
        self.ascii = ascii_mode
        self.bits = [[0] * cols for _ in range(rows)]
        self.color = [[0] * cols for _ in range(rows)]

    def set(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            cx, cy = x // self.cw, y // self.ch
            if self.ascii:
                self.bits[cy][cx] = 1
            else:
                self.bits[cy][cx] |= BRAILLE_BITS[x % 2][y % 4]
            self.color[cy][cx] = color

    def line(self, x0, y0, x1, y1, color):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.set(x0, y0, color)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def blit(self, win, top, left):
        for r in range(self.rows):
            for c in range(self.cols):
                b = self.bits[r][c]
                if not b:
                    continue
                ch = "*" if self.ascii else chr(BRAILLE_BASE + b)
                try:
                    win.addstr(top + r, left + c, ch, curses.color_pair(self.color[r][c]))
                except curses.error:
                    pass


def nice_ticks(lo, hi, n=5):
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** int(f"{raw:e}".split("e")[1])
    step = min((m * mag for m in (1, 2, 5, 10) if m * mag >= raw), default=raw)
    first = -(-lo // step) * step
    ticks, v = [], first
    while v <= hi + 1e-9:
        ticks.append(v)
        v += step
    return ticks


def draw_plot(win, top, left, height, width, ev, chans, thr, ascii_mode, xlim=None):
    """Dibuja un grafico con ejes en el rectangulo dado."""
    g = {k: v[ascii_mode] for k, v in GLYPHS.items()}
    ylab_w = 7
    pc, pr = width - ylab_w - 1, height - 2          # columnas/filas para la curva
    if pc < 10 or pr < 3:
        return
    t = ev["t"]
    idx = range(len(t))
    if xlim:
        idx = [k for k in idx if xlim[0] <= t[k] <= xlim[1]]
    t = [t[k] for k in idx]
    series = [(i, [ev[name][k] for k in idx]) for i, name in enumerate(CHANNELS)
              if i in chans and ev[name]]
    if not t or not series:
        return
    xmin, xmax = t[0], t[-1]
    ymin = min(min(s) for _, s in series)
    ymax = max(max(s) for _, s in series)
    if thr is not None:
        ymin, ymax = min(ymin, thr), max(ymax, thr)
    pad = (ymax - ymin) * 0.05 or 1.0
    ymin, ymax = ymin - pad, ymax + pad
    if xmax <= xmin:
        xmax = xmin + 1

    cv = Canvas(pc, pr, ascii_mode)
    sx = (cv.w - 1) / (xmax - xmin)
    sy = (cv.h - 1) / (ymax - ymin)

    def px(x):
        return int(round((x - xmin) * sx))

    def py(y):
        return int(round((ymax - y) * sy))

    if thr is not None:
        yt = py(thr)
        for x in range(0, cv.w, 3):
            cv.set(x, yt, 4)
    for i, s in series:
        pts = [(px(x), py(y)) for x, y in zip(t, s)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            cv.line(x0, y0, x1, y1, i + 1)

    # Ejes y etiquetas
    plot_left = left + ylab_w + 1
    for r in range(pr):
        win.addstr(top + r, plot_left - 1, g["vert"], curses.color_pair(5))
    win.addstr(top + pr, plot_left - 1, g["corner"] + g["horiz"] * pc, curses.color_pair(5))
    for v in nice_ticks(ymin, ymax, max(2, pr // 3)):
        r = int(round((ymax - v) * sy)) // cv.ch
        if 0 <= r < pr:
            win.addstr(top + r, left, f"{v + 0.0:>{ylab_w}.0f}", curses.color_pair(5))
    for v in nice_ticks(xmin, xmax, max(2, pc // 10)):
        c = int(round((v - xmin) * sx)) // cv.cw
        lab = f"{v + 0.0:.0f}"
        cpos = min(plot_left + c, plot_left + pc - len(lab))
        if c < pc:
            win.addstr(top + pr + 1, cpos, lab, curses.color_pair(5))
    cv.blit(win, top, plot_left)


# ---------------------------------------------------------------- app

class App:
    def __init__(self, root, follow, thr, ascii_mode):
        self.root = root
        self.follow = follow
        self.thr = thr
        self.show_thr = True
        self.ascii = ascii_mode
        self.chans = {0, 1, 2}
        self.split = False
        self.zoom = 1
        self.files = []
        self.cache = {}
        self.fi = 0
        self.ei = 0
        self.ev = None
        self.ev_key = None
        self.msg = ""
        self.refresh_files()
        if self.files:
            self.fi = self.newest_index() if follow else 0

    # --- archivos
    def refresh_files(self):
        self.files = list_files(self.root)

    def newest_index(self):
        def mtime(p):
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0
        return max(range(len(self.files)), key=lambda i: mtime(self.files[i]))

    def current(self):
        if not self.files:
            return None
        path = self.files[self.fi]
        ef = self.cache.get(path)
        if ef is None:
            ef = self.cache[path] = EventFile(path)
        ef.scan()
        return ef

    def goto_file(self, delta):
        if not self.files:
            return
        self.fi = (self.fi + delta) % len(self.files)
        self.ei = 0
        self.follow = False

    def poll(self):
        """En modo en vivo busca archivos nuevos y salta al ultimo evento."""
        if not self.follow:
            return
        self.refresh_files()
        if not self.files:
            return
        self.fi = self.newest_index()
        ef = self.current()
        if ef and ef.events:
            self.ei = len(ef.events) - 1

    # --- dibujo
    def draw(self, scr):
        scr.erase()
        H, W = scr.getmaxyx()
        ef = self.current()
        if ef is None:
            scr.addstr(0, 0, f"No hay archivos .txt en {self.root}"[:W - 1])
            scr.addstr(1, 0, "Esperando datos... (q para salir)"[:W - 1])
            scr.refresh()
            return
        n = len(ef.events)
        self.ei = max(0, min(self.ei, n - 1))

        rel = min(os.path.relpath(ef.path), os.path.abspath(ef.path), key=len)
        g = {k: v[self.ascii] for k, v in GLYPHS.items()}
        live = f"  {g['live']} EN VIVO" if self.follow else ""
        head = f" [{self.fi + 1}/{len(self.files)}] {rel}{live}"
        scr.addstr(0, 0, head[:W - 1], curses.A_REVERSE | curses.A_BOLD)
        scr.chgat(0, min(len(head), W - 1), curses.A_REVERSE)

        if n == 0:
            scr.addstr(2, 2, "Archivo sin eventos todavia.")
            self.draw_footer(scr, H, W)
            scr.refresh()
            return

        key = (ef.path, self.ei)
        if key != self.ev_key:
            self.ev = ef.load(self.ei)
            self.ev_key = key
        ev = self.ev
        _, num, ts = ef.events[self.ei]
        rate = ef.rate()
        info = f" Evento {self.ei + 1}/{n}  (#{num})"
        if ts:
            info += f"  ts={ts}"
        if rate:
            info += f"  tasa~{rate * 60:.1f}/min"
        if self.zoom > 1:
            info += f"  zoom x{self.zoom}"
        scr.addstr(1, 0, info[:W - 1], curses.A_BOLD)

        # Minimo (pico negativo) de cada canal
        x = 1
        for i, name in enumerate(CHANNELS):
            s = ev[name]
            if not s:
                continue
            k = min(range(len(s)), key=s.__getitem__)
            mark = g["on"] if i in self.chans else g["off"]
            txt = f"{mark} {name} {s[k]:.0f} mV @ {ev['t'][k]:.0f} ns   "
            if x + len(txt) < W:
                scr.addstr(2, x, txt, curses.color_pair(i + 1) | curses.A_BOLD)
            x += len(txt)

        thr = self.thr if self.show_thr else None
        xlim = self.zoom_window(ev)
        top, height = 4, H - 6
        if self.split:
            shown = sorted(self.chans)
            if shown:
                h = height // len(shown)
                for j, c in enumerate(shown):
                    draw_plot(scr, top + j * h, 0, h, W - 1, ev, {c}, thr, self.ascii, xlim)
        else:
            draw_plot(scr, top, 0, height, W - 1, ev, self.chans, thr, self.ascii, xlim)
        self.draw_footer(scr, H, W)
        scr.refresh()

    def zoom_window(self, ev):
        """Ventana de tiempo centrada en el minimo de u1 (el canal que dispara)."""
        t, u1 = ev["t"], ev["u1"]
        if self.zoom <= 1 or not t or not u1:
            return None
        half = (t[-1] - t[0]) / self.zoom / 2
        tc = t[min(range(len(u1)), key=u1.__getitem__)]
        tc = max(t[0] + half, min(tc, t[-1] - half))
        return (tc - half, tc + half)

    def draw_footer(self, scr, H, W):
        help_ = " </> evento  PgUp/PgDn +-10  [ ] archivo  1/2/3 canal  s separar  +/- zoom  f vivo  u umbral  q salir "
        if self.msg:
            help_ = f" {self.msg} │" + help_
        try:
            scr.addstr(H - 1, 0, help_[:W - 1], curses.A_REVERSE)
        except curses.error:
            pass

    # --- teclado
    def key(self, k):
        ef = self.current()
        n = len(ef.events) if ef else 0
        self.msg = ""
        if k in (ord("q"), ord("Q"), 27):
            return False
        moves = {curses.KEY_RIGHT: 1, ord(" "): 1, ord("l"): 1,
                 curses.KEY_LEFT: -1, curses.KEY_BACKSPACE: -1, ord("h"): -1,
                 curses.KEY_NPAGE: 10, curses.KEY_PPAGE: -10}
        if k in moves:
            self.ei = max(0, min(self.ei + moves[k], n - 1))
            self.follow = False
        elif k in (curses.KEY_HOME, ord("g")):
            self.ei, self.follow = 0, False
        elif k in (curses.KEY_END, ord("G")):
            self.ei = max(0, n - 1)
        elif k == ord("]"):
            self.goto_file(1)
        elif k == ord("["):
            self.goto_file(-1)
        elif k in (ord("1"), ord("2"), ord("3")):
            self.chans ^= {k - ord("1")}
        elif k in (ord("+"), ord("=")):
            self.zoom = min(self.zoom * 2, 64)
        elif k == ord("-"):
            self.zoom = max(self.zoom // 2, 1)
        elif k == ord("s"):
            self.split = not self.split
        elif k == ord("u"):
            self.show_thr = not self.show_thr
        elif k == ord("f"):
            self.follow = not self.follow
            self.msg = "modo en vivo" if self.follow else "en vivo apagado"
        elif k == ord("r"):
            self.cache.clear()
            self.ev_key = None
            self.refresh_files()
            self.fi = min(self.fi, max(0, len(self.files) - 1))
            self.msg = f"{len(self.files)} archivos"
        return True

    def run(self, scr):
        curses.curs_set(0)
        curses.use_default_colors()
        for i, c in enumerate((curses.COLOR_CYAN, curses.COLOR_YELLOW,
                               curses.COLOR_MAGENTA, curses.COLOR_RED,
                               curses.COLOR_WHITE), start=1):
            curses.init_pair(i, c, -1)
        scr.timeout(int(POLL_SECS * 1000))
        while True:
            self.poll()
            try:
                self.draw(scr)
            except curses.error:
                pass                    # terminal demasiado chica
            k = scr.getch()
            if k == curses.KEY_RESIZE:
                continue
            if k != -1 and not self.key(k):
                return


def main():
    ap = argparse.ArgumentParser(description="Visor de mediciones de muones en la terminal.")
    ap.add_argument("ruta", nargs="?", default="medidas",
                    help="archivo .txt o carpeta con mediciones (default: medidas)")
    ap.add_argument("-f", "--follow", action="store_true",
                    help="modo en vivo: sigue el archivo mas nuevo mientras se mide")
    ap.add_argument("--umbral", type=float, default=KTHR_DEFAULT,
                    help=f"linea de umbral en mV (default: {KTHR_DEFAULT})")
    ap.add_argument("--ascii", action="store_true",
                    help="dibujar con '*' en vez de braille (terminales sin UTF-8)")
    args = ap.parse_args()

    if not os.path.exists(args.ruta):
        if not args.follow:
            ap.error(f"no existe {args.ruta}")
        os.makedirs(args.ruta, exist_ok=True)   # la crea para esperar a drs_exam

    locale.setlocale(locale.LC_ALL, "")
    ascii_mode = args.ascii or "utf" not in (locale.getpreferredencoding() or "").lower()
    app = App(args.ruta, args.follow, args.umbral, ascii_mode)
    curses.wrapper(app.run)


if __name__ == "__main__":
    main()
