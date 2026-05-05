#!/usr/bin/env python3
"""
generate_buda_periods.py

Genera el input para la Step Function btc-arbitrage-buda-ingestion con
granularidad ADAPTATIVA basada en el muestreo empírico de volumen
(buda_monthly_volume_sampled.json).

Reglas de granularidad:
  - lambda_time_est ≤ 400s  → Mensual (margen 2.25x sobre timeout de 900s).
  - 400 < lambda_time_est ≤ 1000s → Quincenal (1-15, 16-fin).
  - lambda_time_est > 1000s → Semanal (4-5 semanas por mes).

Justificación de los thresholds (revisión post-lote-3, mayo 2026):
  Versión inicial usaba 600/1500 (margen implícito 1.5x). El lote 3 falló
  por timeout en noviembre 2020: el sample del día 15 estimó ~432s, la
  realidad excedió 900s. Diferencia >2x entre el día muestreado y el peor
  día del mes.

  Thresholds nuevos (400/1000) implican margen 2.25x sobre 900s. Cubre
  varianza intra-mensual >2x observada empíricamente, con cabecera para
  meses no vistos aún (lotes 4 y 5).

  Trade-off: más períodos totales → backfill más largo, pero unidad de
  pérdida menor en caso de fallo (un timeout pierde una quincena, no un
  mes completo).

Input:  buda_monthly_volume_sampled.json (output de sample_buda_monthly_volume.py)
Output: backfill_buda_input.json (formato esperado por la Step Function)
"""

import json
import sys
from calendar import monthrange
from datetime import datetime, timezone


SYMBOL = "btc-clp"
MONTHLY_THRESHOLD_SEC = 400
QUINCE_THRESHOLD_SEC = 1000


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def month_start_dt(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=timezone.utc)


def next_month_start_dt(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(year, month + 1, 1, tzinfo=timezone.utc)


def split_monthly(year: int, month: int) -> list[tuple[int, int]]:
    """Devuelve [(start_ms, end_ms)] de un solo período mensual."""
    start = month_start_dt(year, month)
    end = next_month_start_dt(year, month)
    return [(to_ms(start), to_ms(end))]


def split_biweekly(year: int, month: int) -> list[tuple[int, int]]:
    """
    Devuelve dos períodos: días 1-15 y 16-fin de mes.
    Split en el inicio del día 16, half-open.
    """
    start = month_start_dt(year, month)
    mid = datetime(year, month, 16, tzinfo=timezone.utc)
    end = next_month_start_dt(year, month)
    return [
        (to_ms(start), to_ms(mid)),
        (to_ms(mid), to_ms(end)),
    ]


def split_weekly(year: int, month: int) -> list[tuple[int, int]]:
    """
    Devuelve 4-5 períodos semanales.
    Bordes: días 1-8, 8-15, 15-22, 22-29, 29-fin.
    """
    days_in_month = monthrange(year, month)[1]
    week_start_days = [1, 8, 15, 22, 29]
    week_starts = [d for d in week_start_days if d <= days_in_month]

    periods = []
    for i, day in enumerate(week_starts):
        start = datetime(year, month, day, tzinfo=timezone.utc)
        if i + 1 < len(week_starts):
            next_day = week_starts[i + 1]
            end = datetime(year, month, next_day, tzinfo=timezone.utc)
        else:
            end = next_month_start_dt(year, month)
        periods.append((to_ms(start), to_ms(end)))
    return periods


def decide_granularity(lambda_time_est_s: float) -> str:
    if lambda_time_est_s <= MONTHLY_THRESHOLD_SEC:
        return "monthly"
    if lambda_time_est_s <= QUINCE_THRESHOLD_SEC:
        return "biweekly"
    return "weekly"


def main():
    with open("buda_monthly_volume_sampled.json") as f:
        sampling = json.load(f)

    periods = []
    breakdown = {"monthly": 0, "biweekly": 0, "weekly": 0}
    period_count_by_gran = {"monthly": 0, "biweekly": 0, "weekly": 0}
    estimated_total_lambda_seconds = 0.0

    for entry in sampling:
        year = entry["year"]
        month = entry["month"]
        lambda_time = entry["lambda_time_est_s"]

        granularity = decide_granularity(lambda_time)
        breakdown[granularity] += 1

        if granularity == "monthly":
            ranges = split_monthly(year, month)
        elif granularity == "biweekly":
            ranges = split_biweekly(year, month)
        else:
            ranges = split_weekly(year, month)

        period_count_by_gran[granularity] += len(ranges)
        estimated_total_lambda_seconds += lambda_time

        for start_ms, end_ms in ranges:
            periods.append({
                "symbol": SYMBOL,
                "start_ms": start_ms,
                "end_ms": end_ms,
            })

    # Resumen a stderr
    print(f"Granularidad aplicada:", file=sys.stderr)
    print(f"  Mensual:   {breakdown['monthly']:>3} meses → {period_count_by_gran['monthly']:>3} períodos", file=sys.stderr)
    print(f"  Quincenal: {breakdown['biweekly']:>3} meses → {period_count_by_gran['biweekly']:>3} períodos", file=sys.stderr)
    print(f"  Semanal:   {breakdown['weekly']:>3} meses → {period_count_by_gran['weekly']:>3} períodos", file=sys.stderr)
    print(file=sys.stderr)
    print(f"Total períodos: {len(periods)}", file=sys.stderr)
    print(f"Tiempo Lambda total estimado: {estimated_total_lambda_seconds/60:.1f} minutos "
          f"({estimated_total_lambda_seconds/3600:.1f}h)", file=sys.stderr)
    print(f"  (con MaxConcurrency=1; throttle entre invocaciones es despreciable)", file=sys.stderr)
    print(file=sys.stderr)
    print(f"Primer período:  {periods[0]}", file=sys.stderr)
    print(f"Último período:  {periods[-1]}", file=sys.stderr)

    # JSON al stdout
    payload = {"periodos": periods}
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()