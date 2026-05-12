"""
Benchmark de latencia para MIndicador.cl — endpoint anual USD/CLP.

Uso:
    python scripts/benchmark_mindicador.py

Valida además:
  - Orden de response.serie (¿DESC o ASC por fecha?)
  - Estructura de cada elemento
  - Días publicados vs esperados (días hábiles aprox.)
"""

import time
import urllib.request
import urllib.error
import json
import statistics
from datetime import datetime

BASE_URL = "https://mindicador.cl/api/dolar"
TEST_YEAR = 2023  # año completo para benchmark representativo
TOTAL_YEARS = list(range(2015, 2027))  # 12 requests en backfill real


def fetch_year(year: int) -> tuple[dict, float]:
    """Descarga un año y retorna (data, elapsed_seconds)."""
    url = f"{BASE_URL}/{year}"
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read()
    elapsed = time.perf_counter() - t0
    data = json.loads(raw)
    return data, elapsed


def inspect_response(data: dict, year: int) -> None:
    """Imprime info de validación estructural."""
    serie = data.get("serie", [])
    print(f"\n  registros       : {len(serie)}")

    if not serie:
        print("  WARN: serie vacía")
        return

    fechas = [e["fecha"][:10] for e in serie]
    primer_valor = serie[0]["valor"]
    ultimo_valor  = serie[-1]["valor"]

    print(f"  serie[0].fecha  : {fechas[0]}  (valor: {primer_valor})")
    print(f"  serie[-1].fecha : {fechas[-1]}  (valor: {ultimo_valor})")

    # ¿orden DESC o ASC?
    if fechas[0] > fechas[-1]:
        print("  orden           : DESC (más reciente primero) ← asumir serie[0] = último")
    elif fechas[0] < fechas[-1]:
        print("  orden           : ASC  (más antiguo primero)  ← asumir serie[-1] = último")
    else:
        print("  orden           : indeterminado (un solo registro)")

    # Claves presentes
    claves = list(serie[0].keys())
    print(f"  claves por entry: {claves}")

    # Tipo del valor
    print(f"  type(valor)     : {type(primer_valor).__name__}")


def main() -> None:
    print("=" * 60)
    print("BENCHMARK MIndicador.cl — /api/dolar/{{YYYY}}")
    print("=" * 60)

    # 1. Inspección estructural + latencia año de prueba
    print(f"\n[1] Inspección estructural — año {TEST_YEAR}")
    try:
        data, elapsed = fetch_year(TEST_YEAR)
        print(f"  latencia        : {elapsed:.3f}s")
        inspect_response(data, TEST_YEAR)
    except urllib.error.HTTPError as e:
        print(f"  ERROR HTTP {e.code}: {e.reason}")
        return
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    # 2. Benchmark: 3 requests al mismo año para medir varianza
    print(f"\n[2] Latencia (3 requests año {TEST_YEAR}, 1s pausa entre c/u)")
    latencias = []
    for i in range(3):
        _, t = fetch_year(TEST_YEAR)
        latencias.append(t)
        print(f"  req {i+1}: {t:.3f}s")
        if i < 2:
            time.sleep(1.0)

    media   = statistics.mean(latencias)
    mediana = statistics.median(latencias)
    desvio  = statistics.stdev(latencias) if len(latencias) > 1 else 0.0

    print(f"\n  media    : {media:.3f}s")
    print(f"  mediana  : {mediana:.3f}s")
    print(f"  desvío   : {desvio:.3f}s")

    # 3. Proyección para 12 años (sin pausa entre requests)
    print(f"\n[3] Proyección backfill completo ({len(TOTAL_YEARS)} años)")
    total_sin_pausa = media * len(TOTAL_YEARS)
    total_con_pausa = (media + 0.5) * len(TOTAL_YEARS)  # 0.5s de cortesía entre requests
    print(f"  sin pausa entre requests : ~{total_sin_pausa:.1f}s")
    print(f"  con 0.5s pausa cortesía  : ~{total_con_pausa:.1f}s")
    print(f"  Lambda timeout default   : 15min (900s) — {'OK' if total_con_pausa < 60 else 'revisar'}")

    # 4. Año actual — ¿responde bien un año incompleto?
    current_year = datetime.utcnow().year
    print(f"\n[4] Año en curso ({current_year}) — validar que el endpoint responde")
    try:
        data_cy, elapsed_cy = fetch_year(current_year)
        serie_cy = data_cy.get("serie", [])
        print(f"  latencia   : {elapsed_cy:.3f}s")
        print(f"  registros  : {len(serie_cy)} (año parcial, esperado < 366)")
        if serie_cy:
            fechas_cy = sorted(e["fecha"][:10] for e in serie_cy)
            print(f"  rango      : {fechas_cy[0]} → {fechas_cy[-1]}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print("\n" + "=" * 60)
    print("Benchmark completo. Pega los resultados para decidir.")
    print("=" * 60)


if __name__ == "__main__":
    main()
