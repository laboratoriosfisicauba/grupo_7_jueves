#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tof_analisis.py - Analisis de tiempo de vuelo (TOF) de muones.

Lee uno o mas archivos *_eventos.csv generados por `drs_tof medir`, calcula
la estadistica de dt31 = t_cfd(CH3) - t_cfd(CH1) por archivo y por grupo
(por defecto, por corrida = run_id; los archivos rotados de una misma
corrida se juntan), dibuja histogramas y, si hay grupos con distintas
separaciones dist_cm, ajusta dt31 = offset + d / v (ajuste lineal pesado)
y da v, beta = v/c y el offset.

Opcionalmente recalcula los tiempos CFD a partir de las formas de onda
(*_ondas.txt) con otra fraccion.

Ejemplos:
    python3 tof_analisis.py medidas/tof/tof_d100cm_*_eventos.csv
    python3 tof_analisis.py medidas/tof/ --agrupar dist --guardar figuras/
    python3 tof_analisis.py medidas/tof/*_eventos.csv --recalcular-cfd 0.3 --amp-min 60
    python3 tof_analisis.py medidas/tof/ --dt-rango -5 20 --no-mostrar --guardar figs/
"""

import argparse
import csv
import glob
import math
import os
import re
import sys

import numpy as np

C_M_POR_NS = 0.299792458  # velocidad de la luz [m/ns]

COLUMNAS = ["evento", "timestamp",
            "t_cfd1_ns", "t_cfd2_ns", "t_cfd3_ns",
            "t_le1_ns", "t_le2_ns", "t_le3_ns",
            "amp1_mV", "amp2_mV", "amp3_mV",
            "base1_mV", "base2_mV", "base3_mV",
            "dt31_ns", "dt21_ns", "dt32_ns"]

RE_META = re.compile(r"^#\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------

def a_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


def leer_eventos(ruta):
    """Devuelve (meta, datos). meta: dict del encabezado '#'.
    datos: dict columna -> np.array (timestamp como lista de str)."""
    meta = {}
    filas = []
    encabezado = None
    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        for linea in f:
            linea = linea.rstrip("\r\n")
            if not linea.strip():
                continue
            if linea.startswith("#"):
                m = RE_META.match(linea)
                if m and m.group(1) not in meta:
                    meta[m.group(1)] = m.group(2)
                continue
            if encabezado is None:
                encabezado = [c.strip() for c in linea.split(",")]
                continue
            filas.append(linea)
    if encabezado is None:
        encabezado = COLUMNAS
    lector = csv.reader(filas)
    cols = {c: [] for c in encabezado}
    for fila in lector:
        if len(fila) != len(encabezado):
            continue  # linea cortada (p.ej. corte de luz en plena escritura)
        for c, v in zip(encabezado, fila):
            cols[c].append(v)
    datos = {}
    for c, vals in cols.items():
        if c == "timestamp":
            datos[c] = vals
        else:
            datos[c] = np.array([a_float(v) for v in vals], dtype=float)
    return meta, datos


def leer_ondas(ruta):
    """Lee *_ondas.txt. Devuelve dict evento -> dict canal -> (t, u)."""
    canales = None
    eventos = {}
    actual = None
    filas = []

    def cerrar():
        if actual is None or not filas or canales is None:
            return
        arr = np.array(filas, dtype=float)
        if arr.ndim != 2 or arr.shape[1] != 2 * len(canales):
            return
        eventos[actual] = {ch: (arr[:, 2 * k], arr[:, 2 * k + 1]) for k, ch in enumerate(canales)}

    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        for linea in f:
            s = linea.strip()
            if not s:
                continue
            if s.startswith("# Columns:"):
                canales = [int(x) for x in re.findall(r"t(\d+)\[ns\]", s)]
                continue
            if s.startswith("#"):
                continue
            if s.startswith("Event #"):
                cerrar()
                m = re.match(r"Event #(\d+)", s)
                actual = int(m.group(1)) if m else None
                filas = []
                continue
            try:
                filas.append([float(x) for x in s.split()])
            except ValueError:
                pass
    cerrar()
    return eventos


# --------------------------------------------------------------------------
# CFD (mismo algoritmo que drs_tof.cpp)
# --------------------------------------------------------------------------

def cruce_hacia_atras(t, u, imin, istart, nivel):
    for k in range(imin - 1, istart - 1, -1):
        if u[k] > nivel:
            du = u[k + 1] - u[k]
            if du == 0:
                return t[k]
            return t[k] + (nivel - u[k]) * (t[k + 1] - t[k]) / du
    return float("nan")


def analizar_canal(t, u, thr, fraccion, nskip=3, nbase=30):
    """Devuelve (base, amp, t_le, t_cfd); NaN si no hay pulso."""
    n = len(u)
    # minimo (no depende de la base), fuera de la region inicial
    i0, i1 = nskip + nbase, n - 3
    imin = i0 + int(np.argmin(u[i0:i1]))
    # base: mediana de todo lo anterior al pulso (hasta 25 muestras antes del
    # minimo, y como minimo nbase muestras). Igual que en drs_tof.cpp.
    fin_base = max(nskip + nbase, imin - 25)
    base = float(np.median(u[nskip:fin_base]))
    amp = float(u[imin] - base)
    nan = float("nan")
    if amp > thr:
        return base, amp, nan, nan
    nivel_le = base + thr
    nbajo = 0
    k = imin
    while k >= 0 and u[k] <= nivel_le:
        nbajo += 1
        k -= 1
    k = imin + 1
    while k < n and u[k] <= nivel_le:
        nbajo += 1
        k += 1
    if nbajo < 3:
        return base, amp, nan, nan
    t_le = cruce_hacia_atras(t, u, imin, nskip, nivel_le)
    t_cfd = cruce_hacia_atras(t, u, imin, nskip, base + fraccion * amp)
    return base, amp, t_le, t_cfd


def recalcular_cfd(ruta_ev, meta, datos, fraccion, nbase=None):
    ruta_ondas = re.sub(r"_eventos\.csv$", "_ondas.txt", ruta_ev)
    if ruta_ondas == ruta_ev or not os.path.exists(ruta_ondas):
        print(f"  AVISO: no encuentro {os.path.basename(ruta_ondas)}; se usan los tiempos del CSV")
        return datos
    thr = a_float(meta.get("thr_mV", "-50"))
    nskip = int(a_float(meta.get("nskip", "3")))
    if nbase is None:
        nbase = int(a_float(meta.get("nbase", "30")))
    ondas = leer_ondas(ruta_ondas)
    ev = datos["evento"].astype(int)
    n_ok = 0
    for i, e in enumerate(ev):
        if e not in ondas:
            continue
        n_ok += 1
        for ch in (1, 2, 3):
            if ch not in ondas[e]:
                continue
            t, u = ondas[e][ch]
            base, amp, t_le, t_cfd = analizar_canal(t, u, thr, fraccion, nskip, nbase)
            datos[f"t_cfd{ch}_ns"][i] = t_cfd
            datos[f"t_le{ch}_ns"][i] = t_le
            datos[f"amp{ch}_mV"][i] = amp
            datos[f"base{ch}_mV"][i] = base
    datos["dt31_ns"] = datos["t_cfd3_ns"] - datos["t_cfd1_ns"]
    datos["dt21_ns"] = datos["t_cfd2_ns"] - datos["t_cfd1_ns"]
    datos["dt32_ns"] = datos["t_cfd3_ns"] - datos["t_cfd2_ns"]
    print(f"  CFD recalculado con fraccion {fraccion} en {n_ok} eventos ({os.path.basename(ruta_ondas)})")
    return datos


# --------------------------------------------------------------------------
# Seleccion y estadistica
# --------------------------------------------------------------------------

def seleccionar_dt(datos, args, col="dt31_ns"):
    dt = datos[col]
    ok = np.isfinite(dt)
    if args.amp_min is not None:
        for ch in (1, 3):
            a = datos[f"amp{ch}_mV"]
            ok &= np.isfinite(a) & (np.abs(a) >= args.amp_min)
    if args.amp_max is not None:
        for ch in (1, 3):
            a = datos[f"amp{ch}_mV"]
            ok &= np.isfinite(a) & (np.abs(a) <= args.amp_max)
    if args.dt_rango is not None:
        ok &= (dt >= args.dt_rango[0]) & (dt <= args.dt_rango[1])
    return dt[ok]


def estadistica(x):
    n = len(x)
    if n == 0:
        return dict(n=0, media=np.nan, std=np.nan, sem=np.nan, mediana=np.nan)
    media = float(np.mean(x))
    std = float(np.std(x, ddof=1)) if n > 1 else np.nan
    sem = std / math.sqrt(n) if n > 1 else np.nan
    return dict(n=n, media=media, std=std, sem=sem, mediana=float(np.median(x)))


def fmt(x, p=3):
    return "nan" if x is None or not np.isfinite(x) else f"{x:.{p}f}"


# --------------------------------------------------------------------------
# Ajuste lineal pesado
# --------------------------------------------------------------------------

def ajuste_lineal(x, y, sy):
    """y = a + b x con errores sy. Devuelve a, b, sa, sb, cov_ab, chi2, ndf."""
    x, y, sy = map(np.asarray, (x, y, sy))
    try:
        from scipy.optimize import curve_fit
        popt, pcov = curve_fit(lambda xx, a, b: a + b * xx, x, y, p0=[0.0, 3.3],
                               sigma=sy, absolute_sigma=True)
        a, b = popt
        metodo = "scipy.optimize.curve_fit"
    except ImportError:
        # np.polyfit pesa con w = 1/sigma; la covarianza se calcula en forma
        # analitica con los errores absolutos (equivale a absolute_sigma=True)
        b, a = np.polyfit(x, y, 1, w=1.0 / sy)
        w = 1.0 / sy ** 2
        S, Sx, Sxx = w.sum(), (w * x).sum(), (w * x * x).sum()
        D = S * Sxx - Sx ** 2
        pcov = np.array([[Sxx / D, -Sx / D], [-Sx / D, S / D]])
        metodo = "numpy.polyfit (scipy no disponible)"
    sa, sb = math.sqrt(pcov[0, 0]), math.sqrt(pcov[1, 1])
    chi2 = float(np.sum(((y - (a + b * x)) / sy) ** 2))
    ndf = len(x) - 2
    return a, b, sa, sb, pcov[0, 1], chi2, ndf, metodo


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------

def expandir_archivos(entradas):
    archivos = []
    for e in entradas:
        if os.path.isdir(e):
            archivos += sorted(glob.glob(os.path.join(e, "*_eventos.csv")))
        elif any(c in e for c in "*?["):
            archivos += sorted(glob.glob(e))
        else:
            archivos.append(e)
    vistos, salida = set(), []
    for a in archivos:
        if a not in vistos:
            vistos.add(a)
            salida.append(a)
    return salida


def main():
    ap = argparse.ArgumentParser(
        description="Analisis de tiempo de vuelo de muones a partir de los *_eventos.csv de drs_tof.",
        epilog="Ejemplo: python3 tof_analisis.py medidas/tof/ --agrupar dist --guardar figs/")
    ap.add_argument("archivos", nargs="+",
                    help="archivos *_eventos.csv, patrones (con comillas) o directorios")
    ap.add_argument("--agrupar", choices=["run", "dist", "archivo"], default="run",
                    help="como juntar archivos: 'run' = misma corrida (default; junta los archivos "
                         "rotados), 'dist' = misma dist_cm, 'archivo' = cada archivo por separado")
    ap.add_argument("--recalcular-cfd", type=float, metavar="FRACCION", default=None,
                    help="recalcula los tiempos CFD desde los *_ondas.txt con esta fraccion (0-1)")
    ap.add_argument("--nbase", type=int, default=None,
                    help="muestras para la linea de base al recalcular (default: la del encabezado)")
    ap.add_argument("--amp-min", type=float, default=None, metavar="MV",
                    help="exigir |amp1| y |amp3| >= MV (mV)")
    ap.add_argument("--amp-max", type=float, default=None, metavar="MV",
                    help="exigir |amp1| y |amp3| <= MV (mV), p.ej. para sacar saturados")
    ap.add_argument("--dt-rango", type=float, nargs=2, default=None, metavar=("MIN", "MAX"),
                    help="quedarse solo con MIN <= dt31 <= MAX (ns), para sacar eventos espurios")
    ap.add_argument("--bins", type=int, default=40, help="bines de los histogramas (default 40)")
    ap.add_argument("--guardar", metavar="DIR", default=None,
                    help="guardar las figuras (PNG) en este directorio")
    ap.add_argument("--no-mostrar", action="store_true",
                    help="no abrir ventanas de graficos (util por ssh)")
    ap.add_argument("--sin-graficos", action="store_true", help="solo imprimir numeros")
    args = ap.parse_args()

    if args.recalcular_cfd is not None and not (0 < args.recalcular_cfd < 1):
        ap.error("--recalcular-cfd debe estar entre 0 y 1")

    archivos = expandir_archivos(args.archivos)
    if not archivos:
        ap.error("no se encontro ningun archivo")

    # ---------------- lectura y estadistica por archivo ----------------
    print("\n=== Por archivo (dt31 = t_cfd3 - t_cfd1) ===")
    print(f"{'archivo':50s} {'dist_cm':>8s} {'Ntot':>6s} {'N':>6s} {'media':>9s} {'std':>8s} {'err':>8s}")
    registros = []
    for ruta in archivos:
        if not os.path.exists(ruta):
            print(f"  AVISO: no existe {ruta}")
            continue
        meta, datos = leer_eventos(ruta)
        if "dt31_ns" not in datos:
            print(f"  AVISO: {ruta} no tiene columna dt31_ns; se ignora")
            continue
        if args.recalcular_cfd is not None:
            datos = recalcular_cfd(ruta, meta, datos, args.recalcular_cfd, args.nbase)
        dist = a_float(meta.get("dist_cm"))
        dt = seleccionar_dt(datos, args)
        st = estadistica(dt)
        ntot = len(datos["dt31_ns"])
        print(f"{os.path.basename(ruta)[:50]:50s} {fmt(dist, 1):>8s} {ntot:6d} {st['n']:6d} "
              f"{fmt(st['media']):>9s} {fmt(st['std']):>8s} {fmt(st['sem']):>8s}")
        if meta.get("calib_temporal_valida", "si").lower() != "si":
            print("    AVISO: medido SIN calibracion temporal valida (--forzar)")
        registros.append(dict(ruta=ruta, meta=meta, dist=dist, dt=dt,
                              run=meta.get("run_id", os.path.basename(ruta))))

    if not registros:
        print("No hay datos.")
        return 1

    # ---------------- agrupamiento ----------------
    grupos = {}
    for r in registros:
        if args.agrupar == "dist":
            clave = f"d={r['dist']:g} cm"
        elif args.agrupar == "run":
            clave = r["run"]
        else:
            clave = os.path.basename(r["ruta"])
        g = grupos.setdefault(clave, dict(dist=r["dist"], dts=[], archivos=[], notas=set()))
        if np.isfinite(g["dist"]) and np.isfinite(r["dist"]) and g["dist"] != r["dist"]:
            print(f"  AVISO: el grupo {clave} mezcla distancias distintas")
        g["dts"].append(r["dt"])
        g["archivos"].append(r["ruta"])
        nota = r["meta"].get("nota", "")
        if nota:
            g["notas"].add(nota)

    print(f"\n=== Por grupo (agrupar = {args.agrupar}) ===")
    print(f"{'grupo':40s} {'dist_cm':>8s} {'arch':>4s} {'N':>6s} {'media':>9s} {'std':>8s} {'err':>8s} {'mediana':>9s}")
    filas_grupo = []
    for clave, g in sorted(grupos.items(), key=lambda kv: (kv[1]["dist"], kv[0])):
        dt = np.concatenate(g["dts"]) if g["dts"] else np.array([])
        st = estadistica(dt)
        g["dt"] = dt
        g["st"] = st
        print(f"{clave[:40]:40s} {fmt(g['dist'], 1):>8s} {len(g['archivos']):4d} {st['n']:6d} "
              f"{fmt(st['media']):>9s} {fmt(st['std']):>8s} {fmt(st['sem']):>8s} {fmt(st['mediana']):>9s}")
        if g["notas"]:
            print(f"    nota: {' | '.join(sorted(g['notas']))}")
        filas_grupo.append((clave, g))

    # ---------------- ajuste dt31 vs distancia ----------------
    ajuste = None
    pts = [(g["dist"] / 100.0, g["st"]["media"], g["st"]["sem"], clave)
           for clave, g in filas_grupo
           if np.isfinite(g["dist"]) and g["st"]["n"] >= 2 and np.isfinite(g["st"]["sem"]) and g["st"]["sem"] > 0]
    distintas = sorted(set(p[0] for p in pts))
    if len(distintas) >= 2:
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        sy = np.array([p[2] for p in pts])
        a, b, sa, sb, cab, chi2, ndf, metodo = ajuste_lineal(x, y, sy)
        print("\n=== Ajuste lineal pesado: dt31 = offset + d / v ===")
        print(f"  metodo: {metodo}, {len(x)} puntos")
        print(f"  pendiente 1/v = {b:.3f} +- {sb:.3f} ns/m   (luz: {1 / C_M_POR_NS:.3f} ns/m)")
        print(f"  offset        = {a:.3f} +- {sa:.3f} ns")
        if ndf > 0:
            print(f"  chi2/ndf      = {chi2:.2f}/{ndf} = {chi2 / ndf:.2f}")
        else:
            print("  chi2/ndf      = (sin grados de libertad: 2 puntos)")
        if b > 0:
            v = 1.0 / b             # m/ns
            sv = sb / b ** 2
            beta, sbeta = v / C_M_POR_NS, sv / C_M_POR_NS
            print(f"  v             = {v:.4f} +- {sv:.4f} m/ns = ({v * 1e9 / 1e8:.3f} +- {sv * 1e9 / 1e8:.3f}) x 1e8 m/s")
            print(f"  beta = v/c    = {beta:.3f} +- {sbeta:.3f}")
        else:
            print("  AVISO: pendiente <= 0, no se puede calcular v (revisar signo/canales).")
        ajuste = (x, y, sy, a, b, sa, sb, [p[3] for p in pts])
    else:
        print("\n(No hay al menos dos distancias distintas con datos: no se ajusta dt31 vs d.)")

    # ---------------- graficos ----------------
    if args.sin_graficos:
        return 0
    try:
        import matplotlib
        if args.no_mostrar or (os.name != "nt" and not os.environ.get("DISPLAY")
                               and sys.platform != "darwin"):
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nAVISO: matplotlib no esta instalado; no se hacen graficos.")
        return 0

    if args.guardar:
        os.makedirs(args.guardar, exist_ok=True)

    ng = len(filas_grupo)
    ncol = min(3, ng)
    nfil = int(math.ceil(ng / ncol))
    fig, ejes = plt.subplots(nfil, ncol, figsize=(5 * ncol, 3.8 * nfil), squeeze=False)
    for k, (clave, g) in enumerate(filas_grupo):
        ax = ejes[k // ncol][k % ncol]
        dt, st = g["dt"], g["st"]
        if st["n"] > 0:
            ax.hist(dt, bins=args.bins, color="C0", alpha=0.8, edgecolor="white", linewidth=0.5)
            ax.axvline(st["media"], color="C3", lw=1.5,
                       label=f"media {fmt(st['media'], 2)} ± {fmt(st['sem'], 2)} ns")
            ax.legend(fontsize=8, frameon=False)
        else:
            ax.text(0.5, 0.5, "sin eventos", ha="center", va="center", transform=ax.transAxes)
        ax.set_title(f"{clave}\nd = {fmt(g['dist'], 1)} cm, N = {st['n']}, σ = {fmt(st['std'], 2)} ns",
                     fontsize=9)
        ax.set_xlabel("dt31 = t3 − t1 [ns]")
        ax.set_ylabel("eventos")
    for k in range(ng, nfil * ncol):
        ejes[k // ncol][k % ncol].axis("off")
    fig.tight_layout()
    if args.guardar:
        p = os.path.join(args.guardar, "tof_histogramas.png")
        fig.savefig(p, dpi=150)
        print(f"\nFigura guardada: {p}")

    if ajuste is not None:
        x, y, sy, a, b, sa, sb, etiquetas = ajuste
        fig2, (ax1, ax2) = plt.subplots(2, 1, figsize=(6.5, 6), sharex=True,
                                        gridspec_kw=dict(height_ratios=[3, 1]))
        ax1.errorbar(x * 100, y, yerr=sy, fmt="o", color="C0", capsize=3, label="datos (media ± error)")
        xx = np.linspace(0, max(x) * 1.1, 100)
        etiqueta = f"ajuste: 1/v = {b:.2f} ± {sb:.2f} ns/m, offset = {a:.2f} ± {sa:.2f} ns"
        ax1.plot(xx * 100, a + b * xx, color="C3", label=etiqueta)
        ax1.plot(xx * 100, a + xx / C_M_POR_NS, color="gray", ls="--", lw=1, label="β = 1 (mismo offset)")
        ax1.set_ylabel("dt31 [ns]")
        ax1.legend(fontsize=8, frameon=False)
        ax2.errorbar(x * 100, y - (a + b * x), yerr=sy, fmt="o", color="C0", capsize=3)
        ax2.axhline(0, color="C3", lw=1)
        ax2.set_xlabel("separación d [cm]")
        ax2.set_ylabel("residuo [ns]")
        fig2.tight_layout()
        if args.guardar:
            p = os.path.join(args.guardar, "tof_ajuste.png")
            fig2.savefig(p, dpi=150)
            print(f"Figura guardada: {p}")

    if not args.no_mostrar and matplotlib.get_backend().lower() != "agg":
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
