import csv
import os
import warnings
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

warnings.filterwarnings("ignore")

CSV_PATH = "query_results/resultado_arbitraje.csv"
OUT_PATH = "assets/top_episodes_by_spread.png"
DPI = 200
HIGHLIGHT_DATE = "2017-12-07"

SANS = "DejaVu Sans"
MONO = "DejaVu Sans Mono"

COL_SPECS = [
    ("Rank",              5.0),
    ("Inicio (Santiago)", 19.0),
    ("Fin (Santiago)",    19.0),
    ("Duración",          11.0),
    ("Spread prom.",      14.0),
    ("Spread máx.",       14.0),
    ("Área",              13.0),
]

SUB_LABELS = ["", "", "", "", "(%)", "(%)", "(pct × min)"]
TOTAL_CW = sum(c[1] for c in COL_SPECS)

BODY_FS = 11.5
HEAD_FS = 12.5
SUB_FS = 9.5
CAP_FS = 10.0


def load(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def clean_ts(raw):
    return raw.replace(" America/Santiago", "").strip()


def fmt_ts(raw):
    c = clean_ts(raw)
    try:
        dt = datetime.strptime(c, "%Y-%m-%d %H:%M:%S.%f")
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return c


def fmt_dur(m):
    m = int(m)
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    if r:
        return f"{h}h {r:02d}m"
    return f"{h}h"


def fmt_pct(v):
    return f"{float(v):.2f}%"


def fmt_area(v):
    return f"{float(v):,.1f}"


def is_highlight(ts):
    return ts.startswith(HIGHLIGHT_DATE)


def build(rows):
    """Sort rows by spread_promedio_pct descending, then build table data."""
    sorted_rows = sorted(
        rows, key=lambda r: float(r["spread_promedio_pct"]), reverse=True
    )
    data = []
    hl = set()
    for i, r in enumerate(sorted_rows):
        ini = fmt_ts(r["inicio_scl"])
        fin = fmt_ts(r["fin_scl"])
        dur = fmt_dur(r["duracion_minutos"])
        avg = fmt_pct(r["spread_promedio_pct"])
        mx = fmt_pct(r["spread_maximo_pct"])
        area = fmt_area(r["area_pct_minutos"])
        data.append([str(i + 1), ini, fin, dur, avg, mx, area])
        if is_highlight(ini):
            hl.add(i)
    return data, hl


def render(fig, data, hl):
    N = len(data)

    # Layout en coordenadas relativas (0-1)
    left = 0.04
    right = 0.04
    top_pad = 0.03
    bot_pad = 0.10          

    head_main_h = 0.050     
    head_sub_h = 0.038     
    total_head = head_main_h + head_sub_h

    data_area = 1.0 - top_pad - bot_pad - total_head
    data_row_h = data_area / N

    tw = 1.0 - left - right

    # Bordes X de cada columna
    cum = left
    x_edges = [cum]
    for c in COL_SPECS:
        cum += tw * (c[1] / TOTAL_CW)
        x_edges.append(cum)
    x_ctr = [(x_edges[i] + x_edges[i + 1]) / 2 for i in range(len(x_edges) - 1)]

    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor("white")

    # Paleta
    grid_c = "#E2E2E2"
    hdr_bg = "#2E2E2E"
    hdr_fg = "#F2F2F2"
    sub_fg = "#C0C0C0"
    bg_even = "#FFFFFF"
    bg_odd = "#F6F6F7"
    hl_bg = "#FFF6EA"
    hl_edge = "#D0A020"
    r1_bg = "#FFF0F0"
    r1_edge = "#D08080"
    body_c = "#1A1A1A"
    rank_c = "#808080"
    rank_hl_c = "#8C6E12"
    rank_r1_c = "#9C3030"

    # ---- Header principal ----
    y_top = 1.0 - top_pad
    y_bot = y_top - head_main_h
    ax.add_patch(Rectangle((left, y_bot), tw, head_main_h,
                           linewidth=0, facecolor=hdr_bg, zorder=3))
    y_mid = (y_top + y_bot) / 2
    for ci, (label, _) in enumerate(COL_SPECS):
        ax.text(x_ctr[ci], y_mid, label,
                family=SANS, size=HEAD_FS, weight="bold", color=hdr_fg,
                ha="center", va="center", zorder=5)

    # ---- Header secundario (unidades) ----
    y_top = y_bot
    y_bot = y_top - head_sub_h
    ax.add_patch(Rectangle((left, y_bot), tw, head_sub_h,
                           linewidth=0, facecolor=hdr_bg, zorder=3))
    y_mid = (y_top + y_bot) / 2
    for ci, txt in enumerate(SUB_LABELS):
        if not txt:
            continue
        ax.text(x_ctr[ci], y_mid, txt,
                family=SANS, size=SUB_FS, color=sub_fg,
                ha="center", va="center", zorder=5)

    # ---- Filas de datos ----
    data_top = y_bot
    for di in range(N):
        y_top = data_top - di * data_row_h
        y_bot = y_top - data_row_h
        y_mid = (y_top + y_bot) / 2

        if di == 15:               # Rank 16 – episodio de +5 h
            bg = "#FFF5F5"
            edge = "#C89898"
        elif di in hl:
            bg = hl_bg
            edge = hl_edge
        elif di % 2 == 0:
            bg = bg_even
            edge = None
        else:
            bg = bg_odd
            edge = None

        if edge:
            px = 0.004
            py = data_row_h * 0.08
            ax.add_patch(Rectangle((left - px, y_bot - py),
                                   tw + 2 * px, data_row_h + 2 * py,
                                   linewidth=1.3, edgecolor=edge,
                                   facecolor=bg, zorder=3, clip_on=False))
        else:
            ax.add_patch(Rectangle((left, y_bot), tw, data_row_h,
                                   linewidth=0, facecolor=bg, zorder=3))

        # Línea separadora entre filas (no antes de la primera)
        if di > 0:
            ax.plot([left, left + tw], [y_top, y_top],
                    linewidth=0.5, color=grid_c, zorder=4)

        row = data[di]
        for ci, txt in enumerate(row):
            if ci == 0:
                if di in hl:
                    color = rank_hl_c
                    weight = "bold"
                else:
                    color = rank_c
                    weight = "normal"
                ax.text(x_ctr[0], y_mid, txt,
                        family=SANS, size=BODY_FS, color=color, weight=weight,
                        ha="center", va="center", zorder=5)
            else:
                ax.text(x_ctr[ci], y_mid, txt,
                        family=MONO, size=BODY_FS, color=body_c,
                        ha="center", va="center", zorder=5)

    # ---- Caption (dos líneas para no exceder el ancho) ----
    caption_y = y_bot - 0.030
    caption = (
        "Top 20 episodios sostenidos de precio BTC en Buda > Binance ≥ 1% "
        "ordenados por spread promedio.\n"
        "Período 2017-08 a 2026-04.  "
        "Granularidad minutal.  Filtra velas interpoladas en Buda."
    )
    ax.text(0.5, caption_y, caption,
            family=SANS, size=CAP_FS, color="#909090", style="italic",
            ha="center", va="top", linespacing=1.4, zorder=5)


def main():
    rows = load(CSV_PATH)
    assert len(rows) == 20, f"Expected 20 rows, got {len(rows)}"
    data, hl = build(rows)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    w_in = 2000 / DPI
    h_in = 1250 / DPI
    fig = plt.figure(figsize=(w_in, h_in), dpi=DPI)
    fig.patch.set_facecolor("white")

    render(fig, data, hl)

    # SIN bbox_inches="tight": evita recortes inesperados del header
    fig.savefig(OUT_PATH, dpi=DPI, facecolor="white", edgecolor="none")
    plt.close(fig)

    sz = os.path.getsize(OUT_PATH) / 1024
    print(f"Saved → {OUT_PATH}  ({sz:.0f} KB)")
    print(f"Data rows: {len(data)}")
    print(f"Highlighted (7-dic-2017): {len(hl)} episodes")
    for idx in sorted(hl):
        print(f"   #{idx + 1:>2}   {data[idx][1]}   "
              f"spread prom. {data[idx][4]}   máx. {data[idx][5]}")


if __name__ == "__main__":
    main()
