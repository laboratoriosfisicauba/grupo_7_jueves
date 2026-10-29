#!/usr/bin/env python3
"""
Visor de mediciones de tiempo de vuelo (drs_tof) para la terminal.

Es la version para el formato de drs_tof del visor.py de drs_exam. Lee los
archivos *_ondas.txt (formas de onda, con un eje de tiempo por canal) y
*_eventos.csv (una linea por evento con t_cfd, amplitudes y dt31) y los
muestra con caracteres braille. Solo usa la biblioteca estandar de Python
(y visor.py, que tiene que estar en la misma carpeta), asi que funciona por
SSH sin entorno grafico.

Uso:
    python3 visor_tof.py                       # recorre ./medidas/tof
    python3 visor_tof.py medidas/tof/tof_d100cm_20261029_153000_idx0_ondas.txt
    python3 visor_tof.py medidas/tof -f        # en vivo: sigue el archivo mas nuevo

Vista de ONDAS (*_ondas.txt):
    <- / ->    evento anterior / siguiente     PgUp/PgDn  -/+ 10 eventos
    Home/End   primer / ultimo evento          1 2 3      mostrar/ocultar canal
    s          superpuesto / separado          + / -      zoom (centrado en el pulso de CH1)
    u          linea de umbral on/off          c          marcas de t_cfd on/off
    e / Enter  ir a la tabla de eventos de esta corrida (mismo evento)

Vista de EVENTOS (*_eventos.csv):
    arriba/abajo  evento anterior / siguiente  PgUp/PgDn  -/+ 10 eventos
    Home/End      primer / ultimo evento       h          variable del histograma
    b / B         menos / mas bins             o / Enter  ver la onda de este evento

Comunes:
    [ / ]      archivo anterior / siguiente    f          modo en vivo on/off
    r          releer archivos                 q          salir
"""
import argparse
import curses
import locale
import math
import os
import re
import time
from datetime import datetime

from visor import Canvas, GLYPHS, POLL_SECS, natural_key, nice_ticks

CHANNELS = (1, 2, 3)
THR_DEFAULT = -50.0     # mV, igual que el default de --thr en drs_tof
HIST_VARS = ("dt31_ns", "dt21_ns", "dt32_ns", "amp1_mV", "amp2_mV", "amp3_mV")
SUF_ONDAS, SUF_EVENTOS = "_ondas.txt", "_eventos.csv"

RE_META = re.compile(r"^#\s*([A-Za-z_0-9]+):\s*(.*)$")
EVENT_RE = re.compile(r"^Event #(-?\d+)(?:\s+ts=(\S+ \S+))?")


def a_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return math.nan


def fmt(v, dec=2, w=0):
    s = "-" if v is None or not math.isfinite(v) else f"{v:.{dec}f}"
    return f"{s:>{w}}" if w else s


def leer_meta(path):
    """Lineas '# clave: valor' del encabezado que escribe drs_tof."""
    meta = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("#"):
                    break
                m = RE_META.match(line.rstrip())
                if m and m.group(1) not in meta:
                    meta[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return meta


def kind_of(path):
    if path.endswith(SUF_ONDAS):
        return "ondas"
    if path.endswith(SUF_EVENTOS):
        return "eventos"
    return None


def sibling(path):
    """*_ondas.txt <-> *_eventos.csv de la misma corrida y el mismo idx."""
    if path.endswith(SUF_ONDAS):
        return path[: -len(SUF_ONDAS)] + SUF_EVENTOS
    if path.endswith(SUF_EVENTOS):
        return path[: -len(SUF_EVENTOS)] + SUF_ONDAS
    return None


def parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
    except (TypeError, ValueError):
        return None


def rate_per_hour(stamps):
    stamps = [parse_ts(s) for s in stamps]
    stamps = [s for s in stamps if s]
    if len(stamps) < 2:
        return None
    dt = (stamps[-1] - stamps[0]).total_seconds()
    return (len(stamps) - 1) / dt * 3600 if dt > 0 else None


# ---------------------------------------------------------------- lectura

class OndasFile:
    """Indexa los eventos de un *_ondas.txt de forma incremental (sirve mientras se escribe)."""

    def __init__(self, path):
        self.path = path
        self.meta = leer_meta(path)
        self.canales = []
        self.events = []        # (offset, numero, ts) de eventos completos
        self.by_num = {}
        self._pos = 0
        self._pending = None
        self._has_data = False

    def scan(self):
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
                if line.startswith(b"# Columns:"):
                    self.canales = [int(x) for x in re.findall(rb"t(\d+)\[ns\]", line)]
                elif line.startswith(b"Event #"):
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
        if self._pending and time.time() - mtime > 2.0:
            self._close_pending()
        return len(self.events) > n_before

    def _close_pending(self):
        if self._pending and self._has_data:
            self.by_num[self._pending[1]] = len(self.events)
            self.events.append(self._pending)
        self._pending = None
        self._has_data = False

    def load(self, k):
        """dict canal -> (t, u) (listas de float) del evento k."""
        offset = self.events[k][0]
        nch = len(self.canales)
        out = {ch: ([], []) for ch in self.canales}
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            f.readline()
            for line in f:
                s = line.strip()
                if not s or s.startswith("Event #"):
                    break
                try:
                    vals = [float(p) for p in s.split()]
                except ValueError:
                    continue
                if len(vals) < 2 * nch:
                    continue
                for i, ch in enumerate(self.canales):
                    out[ch][0].append(vals[2 * i])
                    out[ch][1].append(vals[2 * i + 1])
        return out

    def rate(self, last_n=50):
        return rate_per_hour([e[2] for e in self.events[-last_n:] if e[2]])


class EventosFile:
    """Lee un *_eventos.csv de forma incremental. rows: lista de dict columna -> valor."""

    def __init__(self, path):
        self.path = path
        self.meta = leer_meta(path)
        self.cols = None
        self.rows = []
        self.by_num = {}
        self._pos = 0

    @property
    def events(self):
        return self.rows

    def scan(self):
        n_before = len(self.rows)
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return False
        if size < self._pos:
            self.__init__(self.path)
        with open(self.path, "rb") as f:
            f.seek(self._pos)
            while True:
                raw = f.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                self._pos = f.tell()
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith("#"):
                    continue
                if self.cols is None:
                    self.cols = [c.strip() for c in line.split(",")]
                    continue
                parts = line.split(",")
                if len(parts) != len(self.cols):
                    continue            # linea cortada
                row = {}
                for c, v in zip(self.cols, parts):
                    if c == "timestamp":
                        row[c] = v
                    elif c == "evento":
                        row[c] = int(v) if v.strip().lstrip("-").isdigit() else -1
                    else:
                        row[c] = a_float(v)
                self.by_num[row.get("evento", -1)] = len(self.rows)
                self.rows.append(row)
        return len(self.rows) > n_before

    def row_for(self, num):
        k = self.by_num.get(num)
        return self.rows[k] if k is not None else None

    def values(self, col):
        return [r[col] for r in self.rows if isinstance(r.get(col), float) and math.isfinite(r[col])]

    def rate(self, last_n=50):
        return rate_per_hour([r.get("timestamp") for r in self.rows[-last_n:]])


def stats(vals):
    n = len(vals)
    if n == 0:
        return None
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else math.nan
    srt = sorted(vals)
    med = srt[n // 2] if n % 2 else 0.5 * (srt[n // 2 - 1] + srt[n // 2])
    return {"n": n, "mean": mean, "sd": sd, "err": sd / math.sqrt(n) if n > 1 else math.nan, "med": med}


def list_files(root):
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if kind_of(n):
                found.append(os.path.join(dirpath, n))
    return sorted(found, key=natural_key)


# ---------------------------------------------------------------- dibujo

def draw_waves(win, top, left, height, width, series, thr, ascii_mode, xlim=None, marks=()):
    """series: lista de (color, t, u), cada canal con su propio eje de tiempo.
    marks: lista de (color, x) -> lineas verticales punteadas (t_cfd)."""
    g = {k: v[ascii_mode] for k, v in GLYPHS.items()}
    ylab_w = 7
    pc, pr = width - ylab_w - 1, height - 2
    if pc < 10 or pr < 3:
        return
    cut = []
    for color, t, u in series:
        if not t:
            continue
        if xlim:
            pts = [(x, y) for x, y in zip(t, u) if xlim[0] <= x <= xlim[1]]
        else:
            pts = list(zip(t, u))
        if pts:
            cut.append((color, pts))
    if not cut:
        return
    xmin = min(p[0][0] for _, p in cut)
    xmax = max(p[-1][0] for _, p in cut)
    ymin = min(y for _, p in cut for _, y in p)
    ymax = max(y for _, p in cut for _, y in p)
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
    for color, x in marks:
        if x is not None and math.isfinite(x) and xmin <= x <= xmax:
            xp = px(x)
            for y in range(0, cv.h, 2):
                cv.set(xp, y, color)
    for color, pts in cut:
        pp = [(px(x), py(y)) for x, y in pts]
        for (x0, y0), (x1, y1) in zip(pp, pp[1:]):
            cv.line(x0, y0, x1, y1, color)

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


def draw_hist(win, top, left, height, width, vals, nbins, ascii_mode, mark=None, sel=None):
    """Histograma de vals con nbins. mark: linea vertical (media). sel: bin resaltado."""
    g = {k: v[ascii_mode] for k, v in GLYPHS.items()}
    ylab_w = 6
    pc, pr = width - ylab_w - 1, height - 2
    if pc < 10 or pr < 3 or not vals:
        return
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        lo, hi = lo - 0.5, hi + 0.5
    span = hi - lo
    lo, hi = lo - span * 0.05, hi + span * 0.05
    bw = (hi - lo) / nbins
    counts = [0] * nbins
    for v in vals:
        counts[min(max(int((v - lo) / bw), 0), nbins - 1)] += 1
    cmax = max(counts)

    cv = Canvas(pc, pr, ascii_mode)
    sx = cv.w / (hi - lo)

    def px(x):
        return min(cv.w - 1, max(0, int((x - lo) * sx)))

    for k, cnt in enumerate(counts):
        if not cnt:
            continue
        x0 = int(k * cv.w / nbins)
        x1 = max(x0, int((k + 1) * cv.w / nbins) - 1)
        h = max(1, int(round(cnt / cmax * (cv.h - 1))))
        color = 2 if (sel is not None and math.isfinite(sel) and x0 <= px(sel) <= x1) else 1
        for x in range(x0, x1 + 1):
            for y in range(cv.h - 1, cv.h - 1 - h, -1):
                cv.set(x, y, color)
    if mark is not None and math.isfinite(mark):
        xp = px(mark)
        for y in range(0, cv.h, 2):
            cv.set(xp, y, 4)

    plot_left = left + ylab_w + 1
    for r in range(pr):
        win.addstr(top + r, plot_left - 1, g["vert"], curses.color_pair(5))
    win.addstr(top + pr, plot_left - 1, g["corner"] + g["horiz"] * pc, curses.color_pair(5))
    for v in nice_ticks(0, cmax, max(2, pr // 3)):
        r = int(round((cmax - v) / cmax * (cv.h - 1))) // cv.ch if cmax else 0
        if 0 <= r < pr:
            win.addstr(top + r, left, f"{v + 0.0:>{ylab_w}.0f}", curses.color_pair(5))
    for v in nice_ticks(lo, hi, max(2, pc // 10)):
        c = int((v - lo) * sx) // cv.cw
        lab = f"{v + 0.0:.1f}"
        cpos = min(plot_left + c, plot_left + pc - len(lab))
        if 0 <= c < pc:
            win.addstr(top + pr + 1, cpos, lab, curses.color_pair(5))
    cv.blit(win, top, plot_left)


# ---------------------------------------------------------------- app

class App:
    def __init__(self, root, follow, thr, ascii_mode):
        self.root = root
        self.follow = follow
        self.thr_arg = thr
        self.show_thr = True
        self.show_cfd = True
        self.ascii = ascii_mode
        self.chans = {1, 2, 3}
        self.split = False
        self.zoom = 1
        self.hist_var = 0
        self.nbins = 0          # 0 = automatico
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
        self.poll()

    # --- archivos
    def refresh_files(self):
        self.files = list_files(self.root)

    def newest_index(self, kind=None):
        def mtime(p):
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0
        idx = [i for i, p in enumerate(self.files) if kind is None or kind_of(p) == kind]
        if not idx:
            idx = range(len(self.files))
        return max(idx, key=lambda i: mtime(self.files[i]))

    def open_file(self, path):
        f = self.cache.get(path)
        if f is None:
            f = self.cache[path] = OndasFile(path) if kind_of(path) == "ondas" else EventosFile(path)
        f.scan()
        return f

    def current(self):
        return self.open_file(self.files[self.fi]) if self.files else None

    def kind(self):
        return kind_of(self.files[self.fi]) if self.files else None

    def goto_file(self, delta):
        if not self.files:
            return
        self.fi = (self.fi + delta) % len(self.files)
        self.ei = 0
        self.follow = False

    def goto_sibling(self):
        """Salta entre la onda y la tabla de eventos de la misma corrida, mismo evento."""
        if not self.files:
            return
        cur = self.current()
        other = sibling(cur.path)
        if other not in self.files:
            self.refresh_files()
        if other not in self.files:
            self.msg = f"no existe {os.path.basename(other)}"
            return
        num = self.current_num(cur)
        self.fi = self.files.index(other)
        self.follow = False
        f = self.current()
        k = f.by_num.get(num)
        self.ei = k if k is not None else 0
        if k is None:
            self.msg = f"evento #{num} no esta en {os.path.basename(other)}"

    def current_num(self, f):
        if not f.events or not (0 <= self.ei < len(f.events)):
            return -1
        e = f.events[self.ei]
        return e[1] if isinstance(e, tuple) else e.get("evento", -1)

    def thr(self, f):
        if self.thr_arg is not None:
            return self.thr_arg
        return a_float(f.meta["thr_mV"]) if f.meta.get("thr_mV") else THR_DEFAULT

    def poll(self):
        if not self.follow:
            return
        self.refresh_files()
        if not self.files:
            return
        self.fi = self.newest_index(self.kind())
        f = self.current()
        if f and f.events:
            self.ei = len(f.events) - 1

    # --- dibujo
    def draw_header(self, scr, W, f):
        rel = min(os.path.relpath(f.path), os.path.abspath(f.path), key=len)
        g = {k: v[self.ascii] for k, v in GLYPHS.items()}
        m = f.meta
        extra = []
        if m.get("dist_cm"):
            extra.append(f"d={m['dist_cm']}cm")
        if m.get("freq_real_GSPS"):
            extra.append(f"{a_float(m['freq_real_GSPS']):.2f}GSPS")
        if m.get("calib_temporal_valida"):
            extra.append(f"calib:{m['calib_temporal_valida']}")
        if m.get("nota"):
            extra.append(m["nota"])
        live = f"  {g['live']} EN VIVO" if self.follow else ""
        head = f" [{self.fi + 1}/{len(self.files)}] {rel}  " + "  ".join(extra) + live
        scr.addstr(0, 0, head[:W - 1], curses.A_REVERSE | curses.A_BOLD)
        scr.chgat(0, min(len(head), W - 1), curses.A_REVERSE)

    def draw(self, scr):
        scr.erase()
        H, W = scr.getmaxyx()
        f = self.current()
        if f is None:
            scr.addstr(0, 0, f"No hay archivos {SUF_ONDAS} ni {SUF_EVENTOS} en {self.root}"[:W - 1])
            scr.addstr(1, 0, "Esperando datos... (q para salir)"[:W - 1])
            scr.refresh()
            return
        n = len(f.events)
        self.ei = max(0, min(self.ei, n - 1))
        self.draw_header(scr, W, f)
        if n == 0:
            scr.addstr(2, 2, "Archivo sin eventos todavia.")
            self.draw_footer(scr, H, W)
            scr.refresh()
            return
        if self.kind() == "ondas":
            self.draw_ondas(scr, H, W, f, n)
        else:
            self.draw_eventos(scr, H, W, f, n)
        self.draw_footer(scr, H, W)
        scr.refresh()

    def draw_ondas(self, scr, H, W, f, n):
        g = {k: v[self.ascii] for k, v in GLYPHS.items()}
        key = (f.path, self.ei)
        if key != self.ev_key:
            self.ev = f.load(self.ei)
            self.ev_key = key
        ev = self.ev
        _, num, ts = f.events[self.ei]

        # datos del evento en el *_eventos.csv hermano (si existe)
        row = None
        other = sibling(f.path)
        if os.path.exists(other):
            row = self.open_file(other).row_for(num)

        info = f" Evento {self.ei + 1}/{n}  (#{num})"
        if ts:
            info += f"  ts={ts}"
        if row:
            info += (f"  dt31={fmt(row.get('dt31_ns'), 3)} ns"
                     f"  dt21={fmt(row.get('dt21_ns'), 3)}  dt32={fmt(row.get('dt32_ns'), 3)}")
        rate = f.rate()
        if rate:
            info += f"  tasa~{rate:.1f}/h"
        if self.zoom > 1:
            info += f"  zoom x{self.zoom}"
        scr.addstr(1, 0, info[:W - 1], curses.A_BOLD)

        x = 1
        for ch in CHANNELS:
            if ch not in ev:
                continue
            t, u = ev[ch]
            mark = g["on"] if ch in self.chans else g["off"]
            if row and math.isfinite(row.get(f"amp{ch}_mV", math.nan)):
                txt = f"{mark} CH{ch} {row[f'amp{ch}_mV']:.0f} mV  t_cfd {fmt(row.get(f't_cfd{ch}_ns'), 2)} ns   "
            elif u:
                k = min(range(len(u)), key=u.__getitem__)
                txt = f"{mark} CH{ch} min {u[k]:.0f} mV @ {t[k]:.1f} ns   "
            else:
                continue
            if x + len(txt) < W:
                scr.addstr(2, x, txt, curses.color_pair(ch) | curses.A_BOLD)
            x += len(txt)

        thr = self.thr(f) if self.show_thr else None
        marks = []
        if self.show_cfd and row:
            marks = [(ch, row.get(f"t_cfd{ch}_ns")) for ch in CHANNELS if ch in self.chans]
        xlim = self.zoom_window(ev, row)
        top, height = 4, H - 6
        shown = [ch for ch in sorted(self.chans) if ch in ev]
        if self.split:
            if shown:
                h = height // len(shown)
                for j, ch in enumerate(shown):
                    draw_waves(scr, top + j * h, 0, h, W - 1, [(ch,) + ev[ch]], thr, self.ascii, xlim,
                               [m for m in marks if m[0] == ch])
        else:
            draw_waves(scr, top, 0, height, W - 1, [(ch,) + ev[ch] for ch in shown], thr, self.ascii,
                       xlim, marks)

    def zoom_window(self, ev, row):
        """Ventana centrada en t_cfd de CH1 (o en el minimo del primer canal)."""
        if self.zoom <= 1 or not ev:
            return None
        ch = 1 if 1 in ev else min(ev)
        t, u = ev[ch]
        if not t:
            return None
        half = (t[-1] - t[0]) / self.zoom / 2
        tc = row.get("t_cfd1_ns") if row else None
        if tc is None or not math.isfinite(tc):
            tc = t[min(range(len(u)), key=u.__getitem__)]
        tc = max(t[0] + half, min(tc, t[-1] - half))
        return (tc - half, tc + half)

    def draw_eventos(self, scr, H, W, f, n):
        var = HIST_VARS[self.hist_var]
        vals = f.values(var)
        st = stats(vals)
        unit = "ns" if var.endswith("_ns") else "mV"
        nbins = self.nbins or (max(5, min(60, int(round(math.sqrt(len(vals)) * 1.5)))) if vals else 5)
        line = f" Evento {self.ei + 1}/{n}  |  {var[:-3]}:"
        if st:
            line += (f" N={st['n']}  media {st['mean']:.3f} +- {fmt(st['err'], 3)} {unit}"
                     f"  sd {fmt(st['sd'], 3)}  mediana {st['med']:.3f}  ({nbins} bins, media en rojo)")
        else:
            line += " sin valores"
        rate = f.rate()
        if rate:
            line += f"  tasa~{rate:.1f}/h"
        scr.addstr(1, 0, line[:W - 1], curses.A_BOLD)

        hh = max(6, (H - 7) // 2)
        row = f.events[self.ei]
        if vals:
            draw_hist(scr, 2, 0, hh, W - 1, vals, nbins, self.ascii,
                      st["mean"] if st else None, row.get(var))

        # tabla
        cols = [("evento", 6, lambda r: str(r.get("evento", ""))),
                ("hora", 12, lambda r: (r.get("timestamp") or "")[11:23]),
                ("dt31", 8, lambda r: fmt(r.get("dt31_ns"), 3)),
                ("dt21", 8, lambda r: fmt(r.get("dt21_ns"), 3)),
                ("dt32", 8, lambda r: fmt(r.get("dt32_ns"), 3)),
                ("amp1", 6, lambda r: fmt(r.get("amp1_mV"), 0)),
                ("amp2", 6, lambda r: fmt(r.get("amp2_mV"), 0)),
                ("amp3", 6, lambda r: fmt(r.get("amp3_mV"), 0)),
                ("tcfd1", 8, lambda r: fmt(r.get("t_cfd1_ns"), 2)),
                ("tcfd2", 8, lambda r: fmt(r.get("t_cfd2_ns"), 2)),
                ("tcfd3", 8, lambda r: fmt(r.get("t_cfd3_ns"), 2)),
                ("base1", 6, lambda r: fmt(r.get("base1_mV"), 0)),
                ("base2", 6, lambda r: fmt(r.get("base2_mV"), 0)),
                ("base3", 6, lambda r: fmt(r.get("base3_mV"), 0))]
        ttop = 2 + hh + 1
        nrows = H - 1 - ttop - 1
        if nrows < 1:
            return
        hdr = " ".join(f"{name:>{w}}" for name, w, _ in cols)
        scr.addstr(ttop, 0, hdr[:W - 1], curses.A_UNDERLINE | curses.color_pair(5))
        first = max(0, min(self.ei - nrows // 2, n - nrows))
        for j in range(nrows):
            k = first + j
            if k >= n:
                break
            r = f.events[k]
            txt = " ".join(f"{fn(r):>{w}}" for _, w, fn in cols)
            attr = curses.A_REVERSE | curses.A_BOLD if k == self.ei else curses.A_NORMAL
            try:
                scr.addstr(ttop + 1 + j, 0, txt[:W - 1], attr)
            except curses.error:
                pass

    def draw_footer(self, scr, H, W):
        if self.kind() == "eventos":
            help_ = (" arriba/abajo evento  PgUp/PgDn +-10  [ ] archivo  h variable  b/B bins"
                     "  Enter onda  f vivo  q salir ")
        else:
            help_ = (" </> evento  PgUp/PgDn +-10  [ ] archivo  1/2/3 canal  s separar  +/- zoom"
                     "  u umbral  c cfd  Enter eventos  f vivo  q salir ")
        if self.msg:
            help_ = f" {self.msg} |" + help_
        try:
            scr.addstr(H - 1, 0, help_[:W - 1], curses.A_REVERSE)
        except curses.error:
            pass

    # --- teclado
    def key(self, k):
        f = self.current()
        n = len(f.events) if f else 0
        self.msg = ""
        if k in (ord("q"), ord("Q"), 27):
            return False
        moves = {curses.KEY_RIGHT: 1, curses.KEY_DOWN: 1, ord(" "): 1, ord("l"): 1, ord("j"): 1,
                 curses.KEY_LEFT: -1, curses.KEY_UP: -1, curses.KEY_BACKSPACE: -1, ord("k"): -1,
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
        elif k in (ord("\n"), ord("\r"), curses.KEY_ENTER, ord("e"), ord("o")):
            self.goto_sibling()
        elif k in (ord("1"), ord("2"), ord("3")):
            self.chans ^= {k - ord("0")}
        elif k in (ord("+"), ord("=")):
            self.zoom = min(self.zoom * 2, 64)
        elif k == ord("-"):
            self.zoom = max(self.zoom // 2, 1)
        elif k == ord("s"):
            self.split = not self.split
        elif k == ord("u"):
            self.show_thr = not self.show_thr
        elif k == ord("c"):
            self.show_cfd = not self.show_cfd
        elif k == ord("h"):
            self.hist_var = (self.hist_var + 1) % len(HIST_VARS)
        elif k == ord("b"):
            self.nbins = max(5, (self.nbins or 20) // 2)
        elif k == ord("B"):
            self.nbins = min(200, (self.nbins or 20) * 2)
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
    ap = argparse.ArgumentParser(description="Visor de mediciones de tiempo de vuelo (drs_tof) en la terminal.")
    ap.add_argument("ruta", nargs="?", default=os.path.join("medidas", "tof"),
                    help="archivo *_ondas.txt / *_eventos.csv o carpeta (default: medidas/tof)")
    ap.add_argument("-f", "--follow", action="store_true",
                    help="modo en vivo: sigue el archivo mas nuevo mientras se mide")
    ap.add_argument("--umbral", type=float, default=None,
                    help=f"linea de umbral en mV (default: thr_mV del encabezado, o {THR_DEFAULT})")
    ap.add_argument("--ascii", action="store_true",
                    help="dibujar con '*' en vez de braille (terminales sin UTF-8)")
    args = ap.parse_args()

    if not os.path.exists(args.ruta):
        if not args.follow:
            ap.error(f"no existe {args.ruta}")
        os.makedirs(args.ruta, exist_ok=True)   # la crea para esperar a drs_tof

    locale.setlocale(locale.LC_ALL, "")
    ascii_mode = args.ascii or "utf" not in (locale.getpreferredencoding() or "").lower()
    app = App(args.ruta, args.follow, args.umbral, ascii_mode)
    curses.wrapper(app.run)


if __name__ == "__main__":
    main()
