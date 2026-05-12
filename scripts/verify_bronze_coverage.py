#!/usr/bin/env python3
"""
verify_bronze_coverage.py

Validación end-to-end de la cobertura temporal de la capa Bronze para las
tres fuentes del pipeline de arbitraje BTC:

  - Binance  : bronze/backtest/binance/  (klines BTCUSDT 1m)
  - Buda     : bronze/backtest/buda/     (trades raw BTC-CLP)
  - MIndicador: bronze/backtest/fx/      (USD/CLP diario)

Para Binance y Buda (múltiples archivos particionados) responde:
  1. ¿Cubrimos todos los días del período sin huecos?
  2. ¿Algún día está cubierto por más de un archivo?

Para MIndicador (archivo único de referencia) verifica:
  1. ¿El archivo existe y tiene estructura válida?
  2. ¿Cuál es el rango de fechas y records_count?
  3. ¿is_partial_current_year está correctamente seteado?

Al final reporta la alineación temporal entre fuentes: el rango común
donde las tres fuentes tienen datos simultáneamente. Ese rango es el
válido para construir Gold (joins temporales).

Uso:
  python3 scripts/verify_bronze_coverage.py
  python3 scripts/verify_bronze_coverage.py --bucket otro-bucket

Salida: imprime resumen a stdout. Exit code 0 si todo OK, 1 si hay
gaps, solapes o archivos inválidos en cualquier fuente.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional


DEFAULT_BUCKET = "btc-arbitrage-data-lake-001"

SOURCES = {
    "binance": "bronze/backtest/binance/",
    "buda":    "bronze/backtest/buda/",
    "fx":      "bronze/backtest/fx/",
}

# MIndicador no usa range_start/end_ms — tiene su propio esquema de metadata
FX_KEY = "bronze/backtest/fx/usdclp_dolar_mindicador.json"


# ---------------------------------------------------------------------------
# Utilidades S3 (misma implementación que verify_buda_coverage.py)
# ---------------------------------------------------------------------------

def list_keys(bucket: str, prefix: str) -> list[str]:
    """Lista todas las keys bajo el prefijo dado vía AWS CLI."""
    result = subprocess.run(
        ["aws", "s3", "ls", f"s3://{bucket}/{prefix}", "--recursive"],
        capture_output=True, text=True, check=True,
    )
    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    return [line.split()[-1] for line in lines]


def fetch_json(bucket: str, key: str) -> Optional[dict]:
    """Descarga un objeto S3 y lo parsea como JSON. None si falla."""
    result = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def date_to_ms(date_str: str) -> int:
    """Convierte 'YYYY-MM-DD' a epoch ms UTC."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Verificación de fuentes con múltiples archivos (Binance, Buda)
# ---------------------------------------------------------------------------

def verify_partitioned_source(bucket: str, prefix: str, name: str) -> dict:
    """
    Verifica gaps y solapes en una fuente particionada (Binance o Buda).

    Retorna un dict con:
      ok        : bool
      files     : int
      gaps      : list
      overlaps  : list
      start_ms  : int | None  (inicio real de la cobertura)
      end_ms    : int | None  (fin real de la cobertura)
      days      : float
    """
    print(f"\n{'='*60}")
    print(f"FUENTE: {name.upper()}")
    print(f"Prefijo: s3://{bucket}/{prefix}")
    print(f"{'='*60}")

    keys = list_keys(bucket, prefix)
    print(f"Archivos encontrados: {len(keys)}")

    if not keys:
        print("ERROR: no hay archivos para verificar.")
        return {"ok": False, "files": 0, "gaps": [], "overlaps": [],
                "start_ms": None, "end_ms": None, "days": 0}

    # Extraer ranges — dos esquemas posibles:
    #
    # Buda: metadata contiene range_start_ms y range_end_ms explícitos.
    #
    # Binance: metadata no incluye range_*. El rango se deriva de los datos:
    #   start_ms = data[0][0]   (open_time del primer kline)
    #   end_ms   = data[-1][6]  (close_time del último kline) + 1 para half-open
    #   Archivos vacíos (pre-listing): se cuentan como "empty", no como error.
    ranges: list[tuple[int, int, str]] = []
    failed: list[str] = []
    empty: list[str] = []  # archivos válidos pero sin datos (pre-listing, etc.)

    for i, key in enumerate(keys):
        if i % 25 == 0:
            print(f"  leyendo metadata {i}/{len(keys)}...", end="\r", flush=True)
        payload = fetch_json(bucket, key)
        if payload is None:
            failed.append(key)
            continue
        meta = payload.get("metadata", {})
        data = payload.get("data", [])

        if "range_start_ms" in meta and "range_end_ms" in meta:
            # Esquema Buda: range explícito en metadata
            ranges.append((meta["range_start_ms"], meta["range_end_ms"], key))

        elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
            # Esquema Binance: array de arrays — derivar de data[0][0] y data[-1][6]
            start_ms = int(data[0][0])
            end_ms   = int(data[-1][6]) + 1  # close_time inclusivo → +1 para half-open
            ranges.append((start_ms, end_ms, key))

        elif meta.get("records_count", -1) == 0 or data == []:
            # Archivo vacío válido (pre-listing): no tiene rango real, no es error
            empty.append(key)

        else:
            failed.append(key)

    print(f"  metadata extraída: {len(ranges)}/{len(keys)}".ljust(60))
    if empty:
        print(f"  archivos vacíos (pre-listing): {len(empty)} — skipeados para gaps/solapes")

    if failed:
        print(f"\n  WARNING: {len(failed)} archivos sin metadata válida:")
        for k in failed[:5]:
            print(f"    - {k}")
        if len(failed) > 5:
            print(f"    ... y {len(failed) - 5} más")

    if not ranges:
        print("ERROR: ningún archivo tenía metadata extraíble.")
        return {"ok": False, "files": len(keys), "gaps": [], "overlaps": [],
                "start_ms": None, "end_ms": None, "days": 0}

    ranges.sort()

    # Detectar solapes y gaps
    overlaps = []
    gaps = []

    for i in range(len(ranges) - 1):
        s1, e1, k1 = ranges[i]
        s2, e2, k2 = ranges[i + 1]
        if s2 < e1:
            overlaps.append((k1, k2, e1 - s2))
        elif s2 > e1:
            gaps.append((ms_to_dt(e1), ms_to_dt(s2), s2 - e1))

    # Reporte
    if overlaps:
        print(f"\n  ⚠️  {len(overlaps)} solape(s) detectado(s):")
        for k1, k2, ms in overlaps[:5]:
            print(f"    {k1}")
            print(f"      <-> {k2}: solape de {ms} ms")
    else:
        print("\n  ✓ Cero solapes")

    if gaps:
        print(f"  ⚠️  {len(gaps)} gap(s) detectado(s):")
        for dt_e1, dt_s2, ms in gaps[:10]:
            print(f"    gap: {dt_e1.date()} → {dt_s2.date()} "
                  f"({ms / 1000 / 86400:.1f} días)")
    else:
        print("  ✓ Cero gaps temporales")

    total_ms = sum(e - s for s, e, _ in ranges)
    days = total_ms / 1000 / 86400
    start_ms = ranges[0][0]
    end_ms = ranges[-1][1]

    print(f"\n  Cobertura: {ms_to_dt(start_ms).date()} → {ms_to_dt(end_ms).date()}")
    print(f"  Días cubiertos: {days:.1f}")
    print(f"  Archivos válidos: {len(ranges)}")

    ok = not (overlaps or gaps or failed)
    return {
        "ok": ok,
        "files": len(keys),
        "gaps": gaps,
        "overlaps": overlaps,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "days": days,
    }


# ---------------------------------------------------------------------------
# Verificación de MIndicador (archivo único)
# ---------------------------------------------------------------------------

def verify_fx_source(bucket: str) -> dict:
    """
    Verifica el archivo único de USD/CLP.

    Retorna un dict con:
      ok             : bool
      records        : int
      date_first     : str | None   (YYYY-MM-DD, ASC → primera fecha)
      date_last      : str | None   (YYYY-MM-DD, última fecha disponible)
      is_partial     : bool
      fetched_through: str | None
      start_ms       : int | None   (para alineación con otras fuentes)
      end_ms         : int | None
    """
    print(f"\n{'='*60}")
    print(f"FUENTE: FX (MIndicador.cl — USD/CLP)")
    print(f"Key: s3://{bucket}/{FX_KEY}")
    print(f"{'='*60}")

    payload = fetch_json(bucket, FX_KEY)
    if payload is None:
        print("  ERROR: archivo no encontrado o JSON inválido.")
        return {"ok": False, "records": 0, "date_first": None, "date_last": None,
                "is_partial": None, "fetched_through": None,
                "start_ms": None, "end_ms": None}

    meta = payload.get("metadata", {})
    data = payload.get("data", [])

    records = meta.get("records_count", len(data))
    is_partial = meta.get("is_partial_current_year")
    fetched_through = meta.get("current_year_fetched_through")
    year_start = meta.get("year_start")
    year_end = meta.get("year_end")
    years_fetched = meta.get("years_fetched")
    ingestion_ts = meta.get("ingestion_timestamp_utc", "N/A")

    # Fechas reales desde los datos (fuente de verdad, no metadata)
    date_first = data[0]["fecha"] if data else None
    date_last = data[-1]["fecha"] if data else None

    # Validar is_partial_current_year dinámico
    current_year = datetime.now(timezone.utc).year
    expected_partial = (year_end == current_year)
    partial_ok = (is_partial == expected_partial)

    print(f"  ✓ Archivo encontrado")
    print(f"  Registros           : {records}")
    print(f"  Rango               : {date_first} → {date_last}")
    print(f"  Años descargados    : {years_fetched} ({year_start}–{year_end})")
    print(f"  Ingestado           : {ingestion_ts}")
    print(f"  is_partial_current_year: {is_partial} "
          f"({'✓ correcto' if partial_ok else '⚠️  debería ser ' + str(expected_partial)})")
    if is_partial:
        print(f"  fetched_through     : {fetched_through}")

    ok = bool(data) and partial_ok

    # Convertir fechas a ms para alineación con otras fuentes
    start_ms = date_to_ms(date_first) if date_first else None
    end_ms = date_to_ms(date_last) if date_last else None

    return {
        "ok": ok,
        "records": records,
        "date_first": date_first,
        "date_last": date_last,
        "is_partial": is_partial,
        "fetched_through": fetched_through,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


# ---------------------------------------------------------------------------
# Alineación temporal entre fuentes
# ---------------------------------------------------------------------------

def report_alignment(results: dict) -> None:
    """
    Dado el resultado de las tres fuentes, calcula y reporta el rango común
    donde las tres tienen datos simultáneamente.
    """
    print(f"\n{'='*60}")
    print("ALINEACIÓN TEMPORAL ENTRE FUENTES")
    print(f"{'='*60}")

    starts = {}
    ends = {}
    for name, r in results.items():
        if r.get("start_ms") and r.get("end_ms"):
            starts[name] = r["start_ms"]
            ends[name] = r["end_ms"]

    if len(starts) < 3:
        print("  ⚠️  No se puede calcular alineación: falta cobertura en alguna fuente.")
        return

    # Rango común = max de starts, min de ends
    common_start_ms = max(starts.values())
    common_end_ms = min(ends.values())
    common_start_source = max(starts, key=starts.get)
    common_end_source = min(ends, key=ends.get)

    print(f"\n  Inicio por fuente:")
    for name, ms in sorted(starts.items(), key=lambda x: x[1]):
        print(f"    {name:<12}: {ms_to_dt(ms).date()}")

    print(f"\n  Fin por fuente:")
    for name, ms in sorted(ends.items(), key=lambda x: x[1]):
        print(f"    {name:<12}: {ms_to_dt(ms).date()}")

    if common_start_ms >= common_end_ms:
        print("\n  ⚠️  Sin rango común entre las tres fuentes.")
        return

    common_days = (common_end_ms - common_start_ms) / 1000 / 86400
    print(f"\n  ✓ Rango válido para Gold (las tres fuentes con datos):")
    print(f"    Desde : {ms_to_dt(common_start_ms).date()}"
          f"  ← limitado por {common_start_source}")
    print(f"    Hasta : {ms_to_dt(common_end_ms).date()}"
          f"  ← limitado por {common_end_source}")
    print(f"    Días  : {common_days:.0f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verifica cobertura temporal de las tres fuentes Bronze."
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET,
                        help=f"Bucket S3 (default: {DEFAULT_BUCKET})")
    parser.add_argument("--source", choices=["binance", "buda", "fx", "all"],
                        default="all",
                        help="Fuente a verificar (default: all)")
    args = parser.parse_args()

    results = {}
    exit_code = 0

    if args.source in ("binance", "all"):
        results["binance"] = verify_partitioned_source(
            args.bucket, SOURCES["binance"], "binance"
        )

    if args.source in ("buda", "all"):
        results["buda"] = verify_partitioned_source(
            args.bucket, SOURCES["buda"], "buda"
        )

    if args.source in ("fx", "all"):
        results["fx"] = verify_fx_source(args.bucket)

    if args.source == "all":
        report_alignment(results)

    # Resumen final
    print(f"\n{'='*60}")
    print("RESUMEN")
    print(f"{'='*60}")
    for name, r in results.items():
        status = "✓ OK" if r.get("ok") else "✗ FAIL"
        print(f"  {name:<12}: {status}")

    if not all(r.get("ok") for r in results.values()):
        exit_code = 1

    print()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())