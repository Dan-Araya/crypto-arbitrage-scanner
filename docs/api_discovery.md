# 1. Análisis de API: Binance.com
 
## 1.1 Análisis de Conectividad
 
- **Endpoint Primario:** https://data-api.binance.vision  
- **Endpoint Alternativo:** https://api.binance.com (geo-restringido)  
**Hallazgo Crítico — Bloqueo Geográfico:**  
Durante las pruebas de integración desde AWS `us-east-2` (Ohio, USA), se detectó que los endpoints estándar de Binance (`api.binance.com`, `api1-4.binance.com`) rechazan solicitudes
originadas desde IPs estadounidenses con código `HTTP 451 Unavailable For Legal Reasons`.

**Evidencia (CloudWatch Logs):**
- IP de salida Lambda: `3.144.243.253` (rango AWS us-east-2)
- Response status `api.binance.com/api/v3/ping`: `451`

El código 451 indica restricción por motivos legales, evidenciando la política de Binance de no operar dominios .com en jurisdicciones estadounidenses. 

**Resolución:**  
Sin embargo, Binance también provee `data-api.binance.vision` como endpoint dedicado exclusivamente a datos de mercado públicos. Este endpoint ofrece los mismos endpoints REST que el principal, sin restricciones geográficas, y es la ruta oficialmente recomendada por la documentación de Binance para consultas de market data.
 
**Endpoints disponibles en `data-api.binance.vision`:**  
`GET /api/v3/klines`, `/aggTrades`, `/avgPrice`, `/depth`, `/exchangeInfo`, `/ticker/bookTicker`, `/ticker/price`, `/ticker/24hr`, `/trades`, `/uiKlines`, `/ping`, `/time`.
 
- **Resultado de Prueba:** `HTTP/2 200 OK` (validado desde Lambda en `us-east-2`)
**Fuentes de verificación:**
- Documentación oficial (Market Data Only): https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only
- Documentación general de la API: https://developers.binance.com/docs/binance-spot-api-docs/rest-api
- Endpoints de market data: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
- Repositorio de datos públicos históricos: https://github.com/binance/binance-public-data
## 1.2 Límites de Tasa (Rate Limiting)
 
- **Sistema de Cuotas:** Basado en pesos (*weights*) vinculados a la IP de origen.  
- **Límite Global:** 1,200 puntos por minuto por IP.  
- **Peso del Endpoint (`/klines`):** 1 punto por solicitud.  
**Headers de Control:**
- `x-mbx-used-weight-1m`: Monitoreo del consumo acumulado en el minuto actual.  
**Estrategia de Error:**
- `HTTP 403`: Violación de WAF (Web Application Firewall). Evitar keywords SQL en parámetros.
- `HTTP 429`: Detención inmediata del pipeline (*Exponential Backoff*).  
- `HTTP 418`: IP baneada (consecuencia de ignorar el código 429).  
## 1.3 Análisis de Esquema (Data Contract)

El endpoint devuelve una **Lista de Listas (Array of Arrays)**, optimizada para reducir el payload.

| Índice | Campo                       | Tipo de Dato | Notas de Ingeniería                          |
|--------|-----------------------------|--------------|----------------------------------------------|
| 0      | Open Time                   | Long (ms)    | Unix Timestamp de apertura                   |
| 1      | Open Price                  | String       | Mapeo a float64 para el Data Lake            |
| 2      | High Price                  | String       | Precio máximo en el intervalo                |
| 3      | Low Price                   | String       | Precio mínimo en el intervalo                |
| 4      | Close Price                 | String       | Valor base para cálculo de spread            |
| 5      | Volume                      | String       | Volumen transaccionado (base asset, BTC)     |
| 6      | Close Time                  | Long (ms)    | Unix Timestamp de cierre                     |
| 7      | Quote Asset Volume          | String       | Volumen en el activo de cotización (USDT). No utilizado en el MVP, pero documentado por completitud |
| 8      | Trade Count                 | Integer      | Número de transacciones en la vela           |
| 9      | Taker Buy Base Asset Volume | String       | Volumen comprado por takers agresivos (BTC). Usado en Silver para `buy_volume_btc` del schema unificado — semánticamente equivalente a `sum(amount where direction='buy')` en Buda. |
| 10     | Taker Buy Quote Asset Volume| String       | Volumen comprado por takers en quote asset (USDT). No utilizado en el MVP. |
| 11     | Ignore                      | String       | Campo legado de Binance, valor siempre `"0"`. Se descarta en Silver. |

**Nota sobre el campo 9 (`Taker Buy Base Asset Volume`):** este campo permite
construir un schema Silver simétrico entre Binance y Buda. En Buda, la
clasificación buy/sell viene explícita en el campo `direction` de cada trade
(taker side). En Binance, viene pre-agregada por kline en este índice. Ambas
representan el mismo concepto operacional — volumen donde el taker fue el
agresor compra — y se mapean a la misma columna `buy_volume_btc` en
`silver/backtest/unified_candles/`. La columna complementaria
`sell_volume_btc` se deriva como `volume - taker_buy_base` (Binance) o
`sum(amount where direction='sell')` (Buda). 
## 1.4 Consideraciones de Diseño y Tipado
 
**Precisión y Performance:**  
Aunque la API entrega valores en `String`, se ha optado por transformar y persistir los datos en `float64` (Double) dentro del Data Lake (Parquet).
 
**Justificación:**  
Para el análisis de arbitraje (spreads de 0.5%–2.0%), el error de redondeo de los floats (~10⁻¹²) es insignificante. El uso de `float64` permite aprovechar las optimizaciones de almacenamiento columnar de AWS Athena y la vectorización en procesos de backtesting, evitando la sobrecarga de serialización que implicaría el uso de `Decimal`.
 
> **Nota de evolución:** En un sistema de producción con ejecución real de órdenes, se migraría a `Decimal` para garantizar precisión absoluta en los cálculos financieros.
 
**Consistencia Temporal:**
- **Orden:** Los datos se entregan en orden cronológico ascendente.  
- **Timezone:** UTC estricto (basado en el header `Date` del servidor).  
## 1.5 Recurso Alternativo: Datos Históricos Bulk
 
Binance publica datasets históricos completos en `data.binance.vision` como archivos ZIP descargables directamente, organizados por símbolo, intervalo y mes:
 
```
https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2020-08.zip
```
 
Cada ZIP incluye un archivo `.CHECKSUM` para verificación de integridad (`sha256sum`).
 
**Decisión de diseño:**  
Para el MVP se utiliza la ingesta vía API REST (`/api/v3/klines`) porque demuestra competencias de paginación, rate limiting y manejo de errores. La descarga bulk, sin embargo, es una alternativa válida para escenarios donde el volumen justifique evitar la latencia de la API.
 
**Fuente:** https://github.com/binance/binance-public-data

# 2. Análisis de API: Buda.com
 
## 2.1 Análisis de Conectividad
 
- **Endpoint Base:** `https://www.buda.com/api/v2`
- **Endpoint de trades BTC/CLP:** `/markets/btc-clp/trades.json`
- **Protocolo:** HTTPS/REST (Público, sin autenticación para market data).
- **Resultado de Prueba:** `HTTP/2 200 OK`.
**Observación de Red:**
Servidor gestionado por Cloudflare con nodo en SCL (Santiago).
 
**Quirk del path:**
La API exige el sufijo `.json` en los endpoints de market data. Omitirlo retorna `HTTP 404 Not Found`. Confirmado empíricamente:
 
```
$ curl -sI "https://www.buda.com/api/v2/markets/btc-clp/trades?limit=1"
HTTP/2 404
$ curl -sI "https://www.buda.com/api/v2/markets/btc-clp/trades.json?limit=1"
HTTP/2 200
```
 
## 2.2 Política de Caché y Rate Limiting
 
**Headers de respuesta observados (mayo 2026):**
 
```
cache-control: max-age=0, private, must-revalidate
cf-cache-status: DYNAMIC
```
 
**Hallazgo:** Contrario a la documentación pública informal que mencionaba un TTL de 2 segundos, la política actual de Cloudflare en Buda es **no cachear las respuestas de trades.json**. Cada request impacta el origen.
 
**Rate Limiting:** Se observaron cero eventos de HTTP 429 en 246 ejecuciones de Lambda contra el endpoint, cubriendo throttle de 3.0s, 2.0s y 1.0s (esto es, 0.33 RPS hasta 1.0 RPS) desde IPs de AWS us-east-2. Total ~30k requests sostenidos. El límite real desde Lambda tolera al menos 1 RPS (60 req/min) sin throttling reactivo. Desde IP doméstica se llegó a cubrir throttle de 0.25 segundos sin observar eventos de 429.

**Headers de control de cuota:** Buda no expone headers tipo `x-ratelimit-remaining` o equivalentes. La única señal de saturación es la respuesta `HTTP 429 Too Many Requests` con un header `Retry-After` indicando segundos a esperar.
 
**Comportamiento de errores observados:**
- `HTTP 200`: respuesta exitosa.
- `HTTP 404`: path mal formado (típicamente falta `.json`).
- `HTTP 429`: rate limit excedido (no observado en tests con throttling de 3s).
- `HTTP 5xx`: errores transitorios del backend (no observados en tests).
## 2.3 Análisis de Esquema (Data Contract)
 
Buda envuelve los trades en un objeto `trades` con metadatos de paginación. La estructura completa:
 
```json
{
  "trades": {
    "market_id": "BTC-CLP",
    "timestamp": "<cursor enviado en el request, o null si no se envió>",
    "last_timestamp": "<ts del trade más antiguo del batch, o null si vacío>",
    "entries": [
      ["<ts_ms>", "<amount>", "<price>", "<direction>", <trade_id>],
      ...
    ]
  }
}
```
 
**Mapeo posicional de cada `entry`:**
 
| Índice | Campo      | Tipo en JSON   | Notas                                        |
|--------|------------|----------------|----------------------------------------------|
| 0      | Timestamp  | String (ms)    | Unix epoch en milisegundos, como string.     |
| 1      | Amount     | String         | Volumen en BTC, como string decimal.         |
| 2      | Price      | String         | Precio en CLP, como string decimal.          |
| 3      | Direction  | String         | `"buy"` o `"sell"` (taker side).             |
| 4      | Trade ID   | Number nativo  | Entero JSON, NO string.                      |
 
**Inconsistencia de tipos en JSON:**
Los primeros 4 campos vienen como string (incluyendo numéricos), pero `trade_id` viene como número JSON nativo. Esta asimetría requiere atención al deserializar.
 
**Observación sobre Trade IDs:**
Los IDs son enteros estrictamente crecientes pero **NO scopeados al market BTC/CLP**: comparten contador con otros markets de Buda (ETH/CLP, BCH/CLP, etc). Esto se infiere del siguiente hallazgo empírico:
 
> En el rango BTC/CLP del 15 sept 2017, los trade IDs van de 119,898 a 121,062 (delta=1,164), pero la API retorna sólo 643 trades para ese día. La diferencia (521 IDs ausentes) corresponde a trades de otros markets que no devuelve este endpoint.
 
**Implicación:** los trade IDs sirven para deduplicación y verificación de unicidad **dentro del market BTC/CLP**, pero el delta de IDs entre dos puntos NO es una medida confiable del volumen de BTC/CLP en ese intervalo.
 
## 2.4 Lógica de Paginación
 
A diferencia de Binance, Buda no permite consultas por rango temporal. Utiliza un cursor exclusivo basado en timestamp.
 
**Parámetros del endpoint:**
- `timestamp=<ms>`: cursor exclusivo. Devuelve trades con `ts < timestamp`.
- `limit=<n>`: tamaño de página solicitado. **Cap server-side a 100**, observado experimentalmente:
```
$ curl ".../trades.json?limit=500" | jq '.trades.entries | length'
100
```
 
**Variación observada:** En algunos casos la API retorna 101 entries en vez de 100 (off-by-one del lado de Buda). El consumidor debe iterar todas las entries devueltas, sin asumir un tamaño exacto.
 
**Orden de entrega:**
Los trades vienen en **orden cronológico DESCENDENTE** (más reciente primero). Esto contrasta con Binance, que entrega ascendente.
 
**Semántica del cursor (validada empíricamente):**
 
| Tipo de cursor             | Comportamiento                                                |
|----------------------------|---------------------------------------------------------------|
| Sin cursor (sin parámetro) | Devuelve los trades más recientes del market.                 |
| `timestamp=X`              | Devuelve trades con `ts < X` (exclusivo del valor X).         |
| `timestamp=<muy antiguo>`  | Devuelve `entries: []` y `last_timestamp: null`.              |
 
**Validación de exclusividad:**
Si `last_timestamp` del batch actual es `T`, el siguiente request con `timestamp=T` devuelve trades estrictamente anteriores a `T`. El trade con `ts=T` queda en el batch actual y no se duplica en el siguiente. Confirmado por el test de unicidad: descargando un día completo, los trade IDs resultan únicos sin necesidad de deduplicación post-hoc.
 
**Condición de fin de stream:**
Buda señala el agotamiento del histórico devolviendo:
 
```json
{
  "trades": {
    "market_id": "BTC-CLP",
    "timestamp": "<el cursor enviado>",
    "last_timestamp": null,
    "entries": []
  }
}
```
 
## 2.5 Síntesis de Velas (OHLCV)
 
**Hallazgo:** Buda no expone un endpoint de velas. La documentación oficial menciona `/candles` pero retorna `404` para todos los markets probados.
 
**Implicación:** Cualquier representación OHLCV debe construirse a partir de los trades raw. Esta agregación es responsabilidad del consumidor, no de la API.
 
## 2.6 Volumen Histórico de Trades
 
*(Sección pendiente: a completar con resultados de `measure_buda_monthly_volume.py`.)*
 
## 2.7 Características Distintivas vs. Binance
 
Resumen comparativo de las diferencias clave entre las APIs de las dos fuentes que requieren manejo distinto en el pipeline:
 
| Característica            | Binance (`/api/v3/klines`)         | Buda (`/markets/btc-clp/trades.json`) |
|---------------------------|------------------------------------|---------------------------------------|
| Tipo de dato              | Velas OHLCV pre-agregadas          | Trades raw                            |
| Query model               | Por rango temporal `[start, end]`  | Por cursor exclusivo `ts < cursor`    |
| Orden de entrega          | Cronológico ascendente             | Cronológico descendente               |
| Tamaño de página máximo   | 1000                               | 100 (con variación a 101)             |
| Rate limit                | 1200 weight/min (header tracked)   | ~≥1 RPS sostenido desde Lambda (hasta 4 RPS en red doméstica)    |
| Señalización de cuota     | `x-mbx-used-weight-1m`             | Sólo `HTTP 429` reactivo              |
| Cap geográfico            | `HTTP 451` desde IPs US            | Sin restricción geográfica observada  |
| Endpoint de velas         | Existe                             | Documentado pero no funcional         |
| Histórico bulk alternativo| `data.binance.vision` (ZIPs)       | No disponible                         |
| Scope de identificadores  | `kline_open_time` por símbolo      | `trade_id` global (todos los markets) |


# API Discovery: MIndicador.cl — Dólar Observado (USD/CLP)
 
## Conectividad
 
- **Endpoint anual:** `https://mindicador.cl/api/dolar/{YYYY}`
- **Endpoint por fecha:** `https://mindicador.cl/api/dolar/{dd-mm-yyyy}`
- **Endpoint raíz:** `https://mindicador.cl/api/dolar` — **inestable, no usar**.
  Se detectaron errores `500 Internal Server Error` y `Socket hang up`.
El endpoint anual devuelve todos los valores publicados para el año solicitado,
incluyendo años en curso (responde con los datos disponibles hasta la fecha de consulta).
 
## Esquema de Respuesta
 
```json
{
  "codigo": "dolar",
  "nombre": "Dólar Observado",
  "unidad_medida": "Pesos",
  "serie": [
    {"fecha": "2023-12-29T00:00:00.000Z", "valor": 884.59},
    {"fecha": "2023-01-03T00:00:00.000Z", "valor": 855.86}
  ]
}
```
 
- `serie` viene en orden **DESC** (más reciente primero). Validado empíricamente
  el 2026-05-05 para múltiples años.
- `serie[0]` es siempre el registro más reciente del año consultado.
- `fecha` es ISO 8601 con hora `T00:00:00.000Z`; los primeros 10 caracteres
  (`YYYY-MM-DD`) son suficientes para uso como fecha de referencia.
- `valor` es `float` nativo en JSON.
## Cobertura y Discontinuidades
 
- El valor publicado es el **Dólar Observado** del Banco Central de Chile.
- Solo se publican días hábiles bancarios (lunes a viernes, excluyendo festivos chilenos).
- Sábados, domingos y festivos no tienen entrada en `serie` — no son nulls, simplemente
  no existen. El consumidor debe manejar estos gaps explícitamente.
- El valor del día suele publicarse cerca de las **09:00 AM hora de Santiago (CLT/CLST)**.
  Antes de esa hora, `serie[0]` puede corresponder al día hábil anterior.
## Política de Caché
 
- **TTL reportado:** 3600 segundos (1 hora), observado en headers de respuesta.
- El valor del Dólar Observado es estático durante el día hábil una vez publicado.
## Latencia Observada
 
Medición desde red residencial (Santiago, Chile), 2026-05-05:
 
| Métrica | Valor  |
|---------|--------|
| Mínimo  | 0.716s |
| Máximo  | 1.158s |
| Media   | 0.903s |
| Desvío  | 0.182s |
