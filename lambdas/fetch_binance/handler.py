import json
import boto3
import urllib3
import os
import time
import random
from datetime import datetime, timezone

# Configuración de pool de conexiones
http = urllib3.PoolManager(retries=False)
s3 = boto3.client("s3")

def main(event, context):
    try:
        test = http.request('GET', 'https://api.ipify.org?format=json', timeout=5.0)
        print(f"IP de salida Lambda: {test.data.decode()}")
    except Exception as e:
        print(f"Sin salida a Internet en absoluto: {e}")

    try:
        test2 = http.request('GET', 'https://data-api.binance.vision/api/v3/ping', timeout=5.0)
        print(f"Binance vision ping status: {test2.status}")
    except Exception as e:
        print(f"Binance bloqueado: {e}")

    # 1. Parámetros de entrada
    symbol = event.get("symbol", "BTCUSDT")
    interval = event.get("interval", "1m")
    start_ms = int(event["start_ms"])
    end_ms = int(event["end_ms"])
    
    bucket = os.environ.get("BUCKET_NAME")
    endpoints = ["https://data-api.binance.vision"]
    current_endpoint_idx = 0
    
    all_klines = []
    current_start = start_ms
    used_weight = 0  # Inicialización para evitar UnboundLocalError
    
    print(f"Iniciando descarga: {symbol} ({interval}) desde {start_ms} hasta {end_ms}")

    while current_start < end_ms:
        base_url = endpoints[current_endpoint_idx]
        url = f"{base_url}/api/v3/klines"
        
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000
        }
        
        try:
            query_str = "&".join([f"{k}={v}" for k, v in params.items()])
            response = http.request('GET', f"{url}?{query_str}", timeout=15.0)
            
            # Monitoreo de weight
            used_weight = int(response.headers.get('x-mbx-used-weight-1m', 0))
            
            if response.status == 429:
                wait_time = int(response.headers.get('Retry-After', 60))
                print(f"HTTP 429: Rate limit. Esperando {wait_time}s")
                time.sleep(wait_time)
                continue

            if response.status >= 500:
                print(f"HTTP {response.status} en {base_url}. Cambiando endpoint...")
                current_endpoint_idx = (current_endpoint_idx + 1) % len(endpoints)
                time.sleep(2)
                continue

            # Corrección de decode para evitar errores de parseo
            data = json.loads(response.data.decode('utf-8'))
            
            if not data:
                break
                
            all_klines.extend(data)
            
            # Paginación: close_time + 1
            last_close_time = data[-1][6]
            current_start = last_close_time + 1
            
            # Control de flujo basado en weight
            if used_weight > 1000:
                time.sleep(1)
            else:
                time.sleep(0.1) 

        except Exception as e:
            print(f"Falla de conexión en {base_url}: {str(e)}")
            current_endpoint_idx = (current_endpoint_idx + 1) % len(endpoints)
            time.sleep(2)

    # 2. Persistencia con Metadata
    final_payload = {
        "metadata": {
            "source": "binance",
            "ingestion_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "schema_version": "1.0",
            "symbol": symbol,
            "interval": interval,
            "records_count": len(all_klines)
        },
        "data": all_klines
    }

    # Idempotencia: el key incluye start_ms e interval. 
    # Un reintento sobreescribe el mismo archivo sin duplicar datos.
    dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    file_name = f"{symbol}_{interval}_{start_ms}.json"
    key = f"bronze/backtest/binance/year={dt.year}/month={str(dt.month).zfill(2)}/{file_name}"
    
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(final_payload)
    )
    
    return {
        "status": "success",
        "path": key,
        "records": len(all_klines),
        "last_weight": used_weight
    }