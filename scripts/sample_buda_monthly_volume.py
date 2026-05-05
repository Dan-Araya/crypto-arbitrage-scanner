#!/usr/bin/env python3
"""
sample_buda_monthly_volume.py

Mide el volumen REAL de trades BTC/CLP por mes en Buda mediante muestreo:
descarga el día 15 de cada mes (sample point representativo) y multiplica
por 30 para estimar volumen mensual.

Justificación del método:
  El método anterior (delta de trade_ids con measure_buda_monthly_volume.py)
  fue refutado empíricamente: los trade_ids en Buda son globales (compartidos
  entre todos los markets), por lo que delta de IDs sobreestima dramáticamente
  el volumen específico de BTC/CLP cuando otros markets tienen actividad alta.

  Validación de la refutación:
    - Octubre 2022: delta de IDs sugería 1,057,109 trades/mes
    - Día real (15 oct 2022): 294 trades, 3 páginas
    - Sobrestimación: 115x

  El muestreo elimina este sesgo midiendo directamente trades de BTC/CLP.

Mecánica:
  - Para cada mes desde 2015-01 hasta el mes anterior al actual:
    * Tomamos el día 15 (mid-month, evita efectos de fin de mes / fin de
      año si los hubiera).
    * Hacemos un single curl al endpoint trades.json paginado para ese día.
    * Contamos trades.

  - El script NO usa Lambda; usa curls directos desde la máquina local.
    Esto evita costos de Lambda y mantiene la medición desacoplada de la
    infra del backfill.

  - Throttle de 3s entre días (no entre páginas dentro de un día, eso lo
    maneja el código de paginación interno).

Output: tabla mensual con (year, month, trades_dia_15, trades_mes_estimado,
        paginas_mes_estimadas, lambda_time_estimado_seg).

Tiempo total: ~136 meses * (1 dia * ~6 paginas * 3s + throttle 3s) = ~30 min
en el peor caso. En la práctica, días con pocos trades terminan en 1-2 páginas.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError


BASE_URL = "https://www.buda.com/api/v2/markets/btc-clp/trades.json"
THROTTLE_SECONDS_BETWEEN_PAGES = 3.0
THROTTLE_SECONDS_BETWEEN_DAYS = 3.0
PAGE_SIZE = 100
SECONDS_PER_PAGE_LAMBDA = 3.0
LAMBDA_TIMEOUT_SECONDS = 900
SAMPLE_DAY = 15  # día del mes a muestrear


def fetch_page(timestamp_ms: int) -> dict:
    """Una página de trades anteriores a timestamp_ms (cursor exclusivo)."""
    url = f"{BASE_URL}?timestamp={timestamp_ms}&limit=100"
    req = urllib.request.Request(url, headers={"User-Agent": "buda-sampler/1.0"})

    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def count_trades_in_day(year: int, month: int, day: int) -> tuple[int, int]:
    """
    Cuenta trades BTC/CLP en el día [day, day+1) de (year, month).
    Devuelve (trades_count, pages_used).
    """
    # Half-open: [start, end)
    start_ms = int(datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # Día siguiente. Manejo simple: usar Python datetime con timedelta.
    from datetime import timedelta
    end_dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc) + timedelta(days=1)
    end_ms = int(end_dt.timestamp() * 1000)

    count = 0
    pages = 0
    cursor = end_ms

    while True:
        try:
            payload = fetch_page(cursor)
        except (HTTPError, URLError) as e:
            print(f"    Error en cursor={cursor}: {e}. Retry en 10s...", file=sys.stderr)
            time.sleep(10)
            payload = fetch_page(cursor)

        wrapper = payload["trades"]
        entries = wrapper["entries"]
        last_ts = wrapper["last_timestamp"]
        pages += 1

        if not entries:
            break

        # Filtrar entries por start_ms (las entries vienen descendentes)
        crossed = False
        for entry in entries:
            ts = int(entry[0])
            if ts < start_ms:
                crossed = True
                break
            count += 1

        if crossed:
            break

        if last_ts is None:
            break

        new_cursor = int(last_ts)
        if new_cursor >= cursor:
            break  # defensa
        cursor = new_cursor

        time.sleep(THROTTLE_SECONDS_BETWEEN_PAGES)

    return count, pages


def iter_months(start_y, start_m, end_y, end_m):
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        yield y, m
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1


def main():
    now = datetime.now(timezone.utc)
    if now.month == 1:
        end_year, end_month = now.year - 1, 12
    else:
        end_year, end_month = now.year, now.month - 1

    months = list(iter_months(2015, 1, end_year, end_month))
    print(f"Sampling día {SAMPLE_DAY} de cada mes: {len(months)} meses", file=sys.stderr)
    print(f"Período: {months[0][0]}-{months[0][1]:02d} → {months[-1][0]}-{months[-1][1]:02d}", file=sys.stderr)
    print(file=sys.stderr)

    results = []
    for i, (year, month) in enumerate(months):
        # Fallback: si el mes tiene menos de 15 días (no debería pasar con día 15),
        # o si el día 15 cae en un periodo sin trades, usamos el día tal cual.
        # Para meses sin trades el resultado es 0.
        try:
            trades_in_day, pages_used = count_trades_in_day(year, month, SAMPLE_DAY)
        except Exception as e:
            print(f"  [{year}-{month:02d}] ERROR: {e}. Skipping.", file=sys.stderr)
            trades_in_day, pages_used = -1, 0

        # Estimación mensual: trades/día × días del mes
        from calendar import monthrange
        days_in_month = monthrange(year, month)[1]
        trades_month_est = trades_in_day * days_in_month if trades_in_day >= 0 else -1
        pages_month_est = (trades_month_est + PAGE_SIZE - 1) // PAGE_SIZE if trades_month_est > 0 else 0
        lambda_time_est = pages_month_est * SECONDS_PER_PAGE_LAMBDA
        risk = "⚠️ TIMEOUT" if lambda_time_est > LAMBDA_TIMEOUT_SECONDS else "✓"

        results.append({
            "year": year,
            "month": month,
            "trades_day_15": trades_in_day,
            "pages_day_15": pages_used,
            "trades_month_est": trades_month_est,
            "pages_month_est": pages_month_est,
            "lambda_time_est_s": lambda_time_est,
            "risk": risk,
        })

        # Progreso cada 12 meses
        if (i + 1) % 12 == 0:
            print(f"  ...sampled hasta {year}-{month:02d}", file=sys.stderr)

        time.sleep(THROTTLE_SECONDS_BETWEEN_DAYS)

    # Tabla
    print()
    print(f"{'Año-Mes':<10} {'Trades/día':>12} {'Pgs/día':>10} {'Trades/mes':>12} "
          f"{'Pgs/mes':>10} {'Lambda(s)':>12} {'Riesgo':>10}")
    print("-" * 86)
    for r in results:
        print(
            f"{r['year']}-{r['month']:02d}    "
            f"{r['trades_day_15']:>12,} "
            f"{r['pages_day_15']:>10} "
            f"{r['trades_month_est']:>12,} "
            f"{r['pages_month_est']:>10} "
            f"{r['lambda_time_est_s']:>12.0f} "
            f"{r['risk']:>10}"
        )

    # Resumen
    timeouts = [r for r in results if r['lambda_time_est_s'] > LAMBDA_TIMEOUT_SECONDS]
    near_timeouts = [r for r in results if 600 < r['lambda_time_est_s'] <= LAMBDA_TIMEOUT_SECONDS]

    print()
    print(f"Total trades estimado (suma de meses): {sum(r['trades_month_est'] for r in results if r['trades_month_est'] > 0):,}")
    print(f"Meses que EXCEDEN timeout (>900s): {len(timeouts)}")
    if timeouts:
        for r in timeouts:
            print(f"  - {r['year']}-{r['month']:02d}: {r['trades_month_est']:,} trades estimados, {r['lambda_time_est_s']:.0f}s")
    print(f"Meses cerca del límite (600-900s): {len(near_timeouts)}")
    if near_timeouts:
        for r in near_timeouts:
            print(f"  - {r['year']}-{r['month']:02d}: {r['trades_month_est']:,} trades estimados, {r['lambda_time_est_s']:.0f}s")

    # Guardar JSON
    with open("buda_monthly_volume_sampled.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResultados guardados en: buda_monthly_volume_sampled.json")


if __name__ == "__main__":
    main()