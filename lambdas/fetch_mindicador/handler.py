import json
import boto3
import urllib.request
import urllib.error
import os
import time
from datetime import datetime, timezone

s3 = boto3.client("s3")

# Validado empíricamente (benchmark_mindicador.py):
#   - media 0.903s/request, desvío 0.182s
#   - 12 requests totales para backfill completo (2015-2026)
#   - Tiempo total proyectado con pausa: ~17s << timeout Lambda (900s)
MINDICADOR_BASE_URL = "https://mindicador.cl/api/dolar"
MINDICADOR_THROTTLE_SECONDS = 0.5  # Cortesía; el servidor no impone rate limit visible
MINDICADOR_TIMEOUT_SECONDS = 30.0

# serie[] viene en orden DESC (validado empíricamente: serie[0] = más reciente).
# Invertimos al persistir para que Bronze quede ASC, consistente con Binance y Buda.
EXPECTED_ORDER = "desc"

# Rango de backfill histórico. year_end es inclusivo.
# Buda arranca 2015-01; Binance 2017-08. Cubrimos desde el más antiguo.
YEAR_START_DEFAULT = 2015


def fetch_year(year: int) -> tuple[list, float]:
    """
    Descarga la serie anual de USD/CLP desde MIndicador.cl.
    Retorna (entries_asc, elapsed_seconds).
    entries_asc: lista de {fecha: str, valor: float} ordenada ASC por fecha.
    """
    url = f"{MINDICADOR_BASE_URL}/{year}"
    t0 = time.monotonic()

    try:
        req = urllib.request.urlopen(url, timeout=MINDICADOR_TIMEOUT_SECONDS)
        raw = req.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} al pedir año {year}: {e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Error de red al pedir año {year}: {e.reason}")

    elapsed = time.monotonic() - t0
    payload = json.loads(raw.decode("utf-8"))

    # Validación estructural mínima: el endpoint puede devolver {serie: []} en años
    # futuros o sin datos. Devolvemos lista vacía para que el caller decida.
    serie = payload.get("serie", [])
    if not isinstance(serie, list):
        raise RuntimeError(f"Año {year}: 'serie' no es lista. Payload: {str(payload)[:200]!r}")

    # Invertimos DESC → ASC para consistencia con el resto del data lake.
    entries_asc = list(reversed(serie))

    return entries_asc, elapsed


def validate_entries(entries: list, year: int) -> dict:
    """
    Valida completitud básica de la serie anual.
    Retorna stats de validación incluidos en metadata.
    No falla en días faltantes (fines de semana y festivos chilenos):
    el forward-fill se hace en Silver.
    """
    if not entries:
        return {
            "records_in_year": 0,
            "date_first": None,
            "date_last": None,
            "value_first": None,
            "value_last": None,
        }

    dates = [e["fecha"][:10] for e in entries]

    return {
        "records_in_year": len(entries),
        "date_first": dates[0],
        "date_last": dates[-1],
        "value_first": entries[0]["valor"],
        "value_last": entries[-1]["valor"],
    }


def main(event, context):
    # 1. Parámetros de entrada
    # year_start / year_end son opcionales: permiten re-runs parciales si se necesita.
    # En backfill normal, Step Function no los pasa y se usan los defaults.
    current_year = datetime.now(timezone.utc).year
    year_start = int(event.get("year_start", YEAR_START_DEFAULT))
    year_end = int(event.get("year_end", current_year))

    bucket = os.environ.get("BUCKET_NAME")
    if not bucket:
        raise RuntimeError("Variable de entorno BUCKET_NAME no definida.")

    print(f"Iniciando descarga USD/CLP: años {year_start}→{year_end}")

    # 2. Descarga año por año
    all_entries = []  # Lista plana ASC: toda la historia concatenada
    per_year_stats = []
    total_elapsed = 0.0

    for year in range(year_start, year_end + 1):
        try:
            entries, elapsed = fetch_year(year)
        except RuntimeError as e:
            # Error duro en un año específico: logueamos y abortamos.
            # No enmascaramos con un continue: queremos que el error sea visible
            # en CloudWatch y que Step Functions lo registre como failure.
            print(f"ERROR año {year}: {e}")
            raise

        stats = validate_entries(entries, year)
        is_partial = year == current_year

        print(
            f"  {year}: {stats['records_in_year']} registros "
            f"({stats['date_first']} → {stats['date_last']}) "
            f"{'[PARCIAL]' if is_partial else ''} — {elapsed:.3f}s"
        )

        all_entries.extend(entries)
        per_year_stats.append({
            "year": year,
            "is_partial": is_partial,
            **stats,
            "elapsed_seconds": round(elapsed, 3),
        })
        total_elapsed += elapsed

        # Pausa de cortesía entre requests (no entre el último y el fin)
        if year < year_end:
            time.sleep(MINDICADOR_THROTTLE_SECONDS)

    # 3. Construcción del payload final
    # Metadata análoga a fetch-buda: source, ingestion_timestamp_utc, schema_version,
    # records_count, range_*, is_partial_current_year.
    # Añadimos per_year_stats para trazabilidad sin abrir el array de datos.
    ingestion_ts = datetime.now(timezone.utc).isoformat()
    total_years = year_end - year_start + 1
    current_year_stats = next(
        (s for s in per_year_stats if s["year"] == current_year), None
    )

    final_payload = {
        "metadata": {
            "source": "mindicador_cl",
            "indicator": "dolar",
            "ingestion_timestamp_utc": ingestion_ts,
            "schema_version": "1.0",
            "year_start": year_start,
            "year_end": year_end,
            "years_fetched": total_years,
            "records_count": len(all_entries),
            "is_partial_current_year": year_end == current_year,  # True solo si year_end es el año en curso
            "current_year_fetched_through": (
                current_year_stats["date_last"] if current_year_stats else None
            ),
            "serie_order_in_file": "asc",  # Invertido de DESC original de la API
            "total_elapsed_seconds": round(total_elapsed, 2),
            "per_year_stats": per_year_stats,
        },
        # Fiel a la fuente: solo fecha (YYYY-MM-DD) y valor (float64).
        # fecha: tomamos los primeros 10 chars para normalizar el ISO8601 de la API.
        "data": [
            {"fecha": e["fecha"][:10], "valor": e["valor"]}
            for e in all_entries
        ],
    }

    # 4. Persistencia — archivo único (dataset de referencia, no eventos particionables)
    key = "bronze/backtest/fx/usdclp_dolar_mindicador.json"

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(final_payload, separators=(",", ":")),
    )

    print(
        f"Persistido: s3://{bucket}/{key} | "
        f"{len(all_entries)} registros | "
        f"{total_elapsed:.1f}s total"
    )

    return {
        "status": "success",
        "path": key,
        "records": len(all_entries),
        "years_fetched": total_years,
        "total_elapsed_seconds": round(total_elapsed, 2),
        "current_year_fetched_through": (
            current_year_stats["date_last"] if current_year_stats else None
        ),
    }
