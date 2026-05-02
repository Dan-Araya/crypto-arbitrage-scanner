import json
import boto3
import urllib3
import os
import time
from datetime import datetime, timezone

# Pool de conexiones (sin retries automáticos: queremos control fino del backoff)
http = urllib3.PoolManager(retries=False)
s3 = boto3.client("s3")

# Constantes de la API de Buda (validadas empíricamente, no según doc)
BUDA_BASE_URL = "https://www.buda.com/api/v2"
BUDA_PAGE_SIZE = 100  # Hard cap server-side; pedir más es ignorado
BUDA_THROTTLE_SECONDS = 3.0  # Margen sobre los ~20 req/min nominales
BUDA_TIMEOUT_SECONDS = 15.0


def main(event, context):
    # Sanity check de conectividad (mismo patrón que fetch-binance, útil para CloudWatch)
    try:
        ip_check = http.request("GET", "https://api.ipify.org?format=json", timeout=5.0)
        print(f"IP de salida Lambda: {ip_check.data.decode()}")
    except Exception as e:
        print(f"Sin salida a Internet: {e}")

    # 1. Parámetros de entrada
    # Contrato simétrico con fetch-binance: {symbol, start_ms, end_ms}
    # symbol acepta el formato de Buda directamente: "btc-clp", "eth-clp", etc.
    symbol = event.get("symbol", "btc-clp").lower()
    start_ms = int(event["start_ms"])
    end_ms = int(event["end_ms"])

    bucket = os.environ.get("BUCKET_NAME")
    url = f"{BUDA_BASE_URL}/markets/{symbol}/trades.json"

    # 2. Paginación inversa por cursor
    # Naturaleza de Buda: el endpoint devuelve trades con ts < cursor (exclusivo).
    # Iteramos hacia atrás desde end_ms, hasta cruzar start_ms.
    # Rango tratado como half-open: [start_ms, end_ms)
    all_trades = []
    cursor = end_ms  # Seed: primer request pide trades con ts < end_ms
    pages_fetched = 0
    total_throttle_waits = 0

    print(f"Iniciando descarga: {symbol} en [{start_ms}, {end_ms})")

    while True:
        params = f"?timestamp={cursor}&limit={BUDA_PAGE_SIZE}"

        try:
            response = http.request("GET", f"{url}{params}", timeout=BUDA_TIMEOUT_SECONDS)
        except urllib3.exceptions.HTTPError as e:
            # Errores de red puros: reintentamos una vez con backoff agresivo
            print(f"Error de red en cursor={cursor}: {e}. Reintentando en 10s.")
            time.sleep(10)
            continue

        # Manejo de status codes
        if response.status == 429:
            # Cloudflare nos pidió bajar la frecuencia. Backoff explícito.
            wait = int(response.headers.get("Retry-After", 30))
            print(f"HTTP 429 en cursor={cursor}. Esperando {wait}s.")
            time.sleep(wait)
            total_throttle_waits += 1
            continue

        if response.status >= 500:
            # 5xx en Buda es transitorio; reintento con backoff fijo
            print(f"HTTP {response.status} en cursor={cursor}. Reintentando en 5s.")
            time.sleep(5)
            continue

        if response.status != 200:
            # Cualquier otro código (4xx no-429): error duro, abortamos sin enmascarar
            raise RuntimeError(
                f"HTTP {response.status} inesperado en cursor={cursor}. Body: {response.data[:500]!r}"
            )

        # Parseo (errores de JSON deben aflorar, no enmascararse)
        payload = json.loads(response.data.decode("utf-8"))
        wrapper = payload["trades"]
        entries = wrapper["entries"]
        last_ts = wrapper["last_timestamp"]

        pages_fetched += 1

        # Condición de fin: stream agotado (validado empíricamente con cursor pre-mercado)
        if not entries:
            print(f"Stream agotado en cursor={cursor} tras {pages_fetched} páginas.")
            break

        # Filtrado por límite inferior del rango.
        # Las entries vienen en orden DESCENDENTE por timestamp.
        # Aceptamos sólo las que cumplen start_ms <= ts (recuerda: ts < end_ms ya está
        # garantizado por la semántica exclusiva del cursor).
        accepted_in_page = 0
        crossed_lower_bound = False
        for entry in entries:
            ts_ms = int(entry[0])
            if ts_ms < start_ms:
                crossed_lower_bound = True
                break  # Como vienen descendentes, todo lo que sigue también está fuera
            all_trades.append(entry)
            accepted_in_page += 1

        if crossed_lower_bound:
            print(
                f"Cruzamos start_ms={start_ms} en página {pages_fetched}. "
                f"Aceptados {accepted_in_page} de {len(entries)} en esta página."
            )
            break

        # Avance del cursor: usamos last_timestamp tal cual (semántica exclusiva → no duplica).
        # Si last_ts es None, la API señaló fin de historia (defensa adicional).
        if last_ts is None:
            print(f"last_timestamp=null en cursor={cursor}. Fin de historia.")
            break

        new_cursor = int(last_ts)
        if new_cursor >= cursor:
            # Defensa contra loop infinito (no debería ocurrir, pero protegemos contra
            # cambios de comportamiento de la API)
            raise RuntimeError(
                f"Cursor no avanzó: anterior={cursor}, nuevo={new_cursor}. Abortando."
            )
        cursor = new_cursor

        # Throttling respetuoso del rate limit nominal (~20 req/min)
        time.sleep(BUDA_THROTTLE_SECONDS)

    # 3. Reordenamiento a cronológico ascendente
    # Buda devuelve descendente; el resto del data lake (Binance) usa ascendente.
    # Normalizamos aquí en bronze para mantener consistencia entre fuentes.
    all_trades.reverse()

    # 4. Persistencia con metadata
    final_payload = {
        "metadata": {
            "source": "buda",
            "ingestion_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "schema_version": "1.0",
            "symbol": symbol,
            "range_start_ms": start_ms,
            "range_end_ms": end_ms,
            "range_semantics": "half_open_[start,end)",
            "records_count": len(all_trades),
            "pages_fetched": pages_fetched,
            "throttle_429_events": total_throttle_waits,
        },
        "data": all_trades,
    }

    # Idempotencia: key derivado de symbol + start_ms (mismo patrón que Binance)
    dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    file_name = f"{symbol}_trades_{start_ms}.json"
    key = (
        f"bronze/backtest/buda/"
        f"year={dt.year}/month={str(dt.month).zfill(2)}/{file_name}"
    )

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(final_payload, separators=(",", ":")),
    )

    return {
        "status": "success",
        "path": key,
        "records": len(all_trades),
        "pages": pages_fetched,
        "throttle_events": total_throttle_waits,
    }