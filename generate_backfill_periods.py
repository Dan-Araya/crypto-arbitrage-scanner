"""
Genera la lista de períodos mensuales para el backfill histórico de Binance
en formato compatible con Step Functions.

Uso:
    python generate_backfill_periods.py > backfill_input.json
"""

import json
from datetime import datetime, timezone
from calendar import monthrange


def generate_monthly_periods(
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
):
    """
    Genera períodos mensuales [start_ms, end_ms] para el backfill.

    Cada período cubre desde el primer milisegundo del mes hasta el último
    milisegundo del mes (inclusive). Binance interpreta endTime como inclusivo,
    por lo que el solapamiento de 1 minuto entre meses consecutivos está
    documentado en data_quality.md y se resuelve en la capa silver.
    """
    periods = []
    year, month = start_year, start_month

    while (year, month) <= (end_year, end_month):
        start_dt = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
        last_day = monthrange(year, month)[1]
        end_dt = datetime(
            year, month, last_day, 23, 59, 59, 999000, tzinfo=timezone.utc
        )

        periods.append(
            {
                "symbol": symbol,
                "interval": interval,
                "start_ms": int(start_dt.timestamp() * 1000),
                "end_ms": int(end_dt.timestamp() * 1000),
            }
        )

        # Avanzar al siguiente mes
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1

    return {"periodos": periods}


if __name__ == "__main__":
    # Rango completo: enero 2017 a abril 2026
    # BTCUSDT se listó en Binance en nov-2017, los meses anteriores devolverán
    # arrays vacíos (manejado por el handler con `if not data: break`).
    output = generate_monthly_periods(
        start_year=2017,
        start_month=1,
        end_year=2026,
        end_month=4,
    )
    print(json.dumps(output, indent=2))