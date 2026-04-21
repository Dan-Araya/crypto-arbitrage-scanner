# 1. Análisis de API: Binance.com

## 1.1 Análisis de Conectividad

- **Endpoint Primario:** https://api.binance.com  
- **Endpoint de Resiliencia (Fallback):** https://api3.binance.com  

**Justificación de Arquitectura:**  
Se establece el endpoint principal como ruta estándar por su estabilidad y documentación oficial. Se incluye `api3` en la lógica de failover del pipeline para asegurar la continuidad operativa ante congestión de red o latencias elevadas en el nodo principal, aprovechando que `api3` suele ofrecer una ruta de red más directa (bypass de ciertas capas de caché).

- **Resultado de Prueba:** `HTTP/2 200 OK`

## 1.2 Límites de Tasa (Rate Limiting)

- **Sistema de Cuotas:** Basado en pesos (*weights*) vinculados a la IP de origen.  
- **Límite Global:** 1,200 puntos por minuto por IP.  
- **Peso del Endpoint (`/klines`):** 1 punto por solicitud.  

**Headers de Control:**
- `x-mbx-used-weight-1m`: Monitoreo del consumo acumulado en el minuto actual.  

**Estrategia de Error:**
- `HTTP 429`: Detención inmediata del pipeline (*Exponential Backoff*).  
- `HTTP 418`: IP baneada (consecuencia de ignorar el código 429).  

## 1.3 Análisis de Esquema (Data Contract)

El endpoint devuelve una **Lista de Listas (Array of Arrays)**, optimizada para reducir el payload.

| Índice | Campo              | Tipo de Dato | Notas de Ingeniería                          |
|--------|--------------------|--------------|----------------------------------------------|
| 0      | Open Time          | Long (ms)    | Unix Timestamp de apertura                   |
| 1      | Open Price         | String       | Mapeo a float64 para el Data Lake            |
| 2      | High Price         | String       | Precio máximo en el intervalo                |
| 3      | Low Price          | String       | Precio mínimo en el intervalo                |
| 4      | Close Price        | String       | Valor base para cálculo de spread            |
| 5      | Volume             | String       | Volumen transaccionado (base asset)          |
| 6      | Close Time         | Long (ms)    | Unix Timestamp de cierre                     |
| 7      | Quote Asset Volume | String       | Volumen en el activo de cotización (USDT). No utilizado en el MVP, pero documentado por completitud |
| 8      | Trade Count        | Integer      | Número de transacciones en la vela           |

## 1.4 Consideraciones de Diseño y Tipado

**Precisión y Performance:**  
Aunque la API entrega valores en `String`, se ha optado por transformar y persistir los datos en `float64` (Double) dentro del Data Lake (Parquet).

**Justificación:**  
Para el análisis de arbitraje (spreads de 0.5%–2.0%), el error de redondeo de los floats (~10⁻¹²) es insignificante. El uso de `float64` permite aprovechar las optimizaciones de almacenamiento columnar de AWS Athena y la vectorización en procesos de backtesting, evitando la sobrecarga de serialización que implicaría el uso de `Decimal`.

> **Nota de evolución:** En un sistema de producción con ejecución real de órdenes, se migraría a `Decimal` para garantizar precisión absoluta en los cálculos financieros.

**Consistencia Temporal:**
- **Orden:** Los datos se entregan en orden cronológico ascendente.  
- **Timezone:** UTC estricto (basado en el header `Date` del servidor).  


# 2. Análisis de API: Buda.com

## 2.1 Análisis de Conectividad

- **Endpoint Base:** https://www.buda.com/api/v2  
- **Protocolo:** HTTPS/REST (Público).  
- **Resultado de Prueba:** `HTTP/2 200 OK` (validado mediante `trades.json`).  

**Observación de Red:**  
Servidor gestionado por Cloudflare con nodo en SCL (Santiago).

**Quirk detectado:**  
La API es estricta con el formato. Se requiere explícitamente el sufijo `.json` en los endpoints (ej. `/trades.json`) para evitar errores `404 Not Found`, corrigiendo así la omisión detectada en la documentación oficial.

## 2.2 Límites de Tasa y Control de Flujo

- **Límite Nominal:** ~20 solicitudes por minuto (dinámico).  
- **TTL de Caché:** `max-age=2` (2 segundos).  

**Estrategia de Polling:**  
Se establece una frecuencia de consulta ≥ 2 segundos para el pipeline en tiempo real. Consultas más frecuentes consumirían cuota de IP sin obtener datos nuevos debido a la política de almacenamiento en borde (*Edge*) de Cloudflare.

## 2.3 Análisis de Esquema (Data Contract)

Buda entrega las transacciones granulares en un formato de envoltorio (*wrapper*) con metadatos de paginación.

**Mapeo posicional de `entries`:**

| Índice | Campo       | Tipo de Dato | Transformación Final              |
|--------|------------|--------------|----------------------------------|
| 0      | Timestamp  | String (ms)  | Unix Epoch (milisegundos)        |
| 1      | Amount     | String       | float64 (volumen BTC)            |
| 2      | Price      | String       | float64 (precio CLP)             |
| 3      | Direction  | String       | Categorical (`buy` / `sell`)     |
| 4      | Trade ID   | Integer      | ID único para deduplicación      |

## 2.4 Lógica de Paginación e Ingesta Histórica

A diferencia de Binance, Buda no permite consultas por rangos de tiempo fijos, sino que utiliza un cursor basado en eventos.

- **Seed:** Se inicia con el llamado a los trades más recientes.  
- **Cursor:** Se extrae el campo `last_timestamp` de la raíz del JSON (representa el evento más antiguo del batch actual).  
- **Iteración:** El siguiente request se parametriza como: `.../trades.json?timestamp={last_timestamp}&limit=100`  
- **Resiliencia:** Debido al límite de ~20 req/min, el *backfill* histórico masivo requiere una implementación de *throttling* para evitar bloqueos de IP.  

## 2.5 Síntesis de Velas (OHLCV)

**Hallazgo:** No existe un endpoint funcional de velas (`/candles`) en la API de Buda.

**Estrategia de Datos:**  
Para el backtest y el análisis comparativo con Binance, el pipeline asumirá la responsabilidad de reconstruir velas de 1 minuto a partir de los trades raw. La agregación se realizará en la capa de transformación (Lambda con pyarrow/pandas) antes de la persistencia en el Data Lake.

**Ventaja:** Esto permite un análisis de high-tick más preciso que el de Binance, capturando el slippage real del mercado local.

## 2.6 Estimación de Volumen para Backfill

**Problema:** El dimensionamiento del backfill depende directamente del volumen histórico de trades en BTC/CLP. Este es un mercado de baja liquidez comparado con Binance, lo que impacta tanto los tiempos de descarga como la calidad de las velas reconstruidas.

**Estimación (pendiente de validación empírica):**

| Escenario | Trades/día | Páginas/día (100 trades/pág) | Tiempo/día (20 req/min) |
|-----------|------------|------------------------------|-------------------------|
| Baja liquidez  | ~100   | 1     | < 1 min   |
| Media          | ~1,000 | 10    | ~30 seg   |
| Alta (peaks)   | ~5,000 | 50    | ~2.5 min  |

> **Acción requerida:** Antes de iniciar el backfill masivo, ejecutar una descarga de prueba de 1 semana para medir el volumen real y calibrar los tiempos de la Step Function.

**Implicación en calidad:** En períodos de baja liquidez (< 50 trades/día), muchas velas de 1 minuto estarán vacías. El pipeline aplicará forward-fill del último close conocido para mantener la continuidad de la serie temporal, y marcará estas velas sintéticas con un flag `is_interpolated = true` para que el análisis posterior pueda filtrarlas si es necesario.


# 3. Análisis de API: MIndicador.cl (Conversión USD/CLP)

## 3.1 Análisis de Conectividad

- **Endpoint:** `https://mindicador.cl/api/dolar/{dd-mm-yyyy}`  
- **Resultado de Prueba:** `HTTP/1.1 200 OK` (validado con fecha específica).  

**Observación de Estabilidad:**  
Se detectaron errores `500 Internal Server Error` y `Socket hang up` en el endpoint raíz. Se establece como regla de diseño consultar siempre por fecha específica o el endpoint anual para minimizar la carga sobre el servidor y asegurar la respuesta.

## 3.2 Política de Caché y Consumo

- **TTL (Time To Live):** 3600 segundos (1 hora).  

**Estrategia de Optimización:**  
Dado que el valor del "Dólar Observado" es estático durante la mayor parte del día, el pipeline implementará una caché de nivel 1 (variable global en Lambda) para evitar llamadas redundantes en cada ciclo de arbitraje. Esto reduce la latencia del cálculo y evita sobrecargar la API externa.

## 3.3 Análisis de Esquema y Tipado

- **Formato:** JSON anidado con metadatos y una lista `serie`.  
- **Tipo de Dato:** Aunque la API entrega un `float` nativo en el JSON, el pipeline lo tratará como `float64` para mantener la consistencia con el esquema de Parquet definido para los otros activos.  
- **Acceso:** `response.serie[0].valor`  

## 3.4 Manejo de Discontinuidad Temporal (Fines de semana y Festivos)

A diferencia de los mercados de criptomonedas (24/7), el mercado cambiario formal opera bajo el calendario bancario chileno.

**Fenómeno:**  
Ausencia de datos para sábados, domingos y festivos nacionales.

**Regla de Ingeniería (Forward Fill):**  
El sistema implementará la lógica de "Último Valor Conocido". Ante la ausencia de un dato para la fecha `T`, se aplicará el valor de la fecha `T-n` más cercana disponible (típicamente el valor del viernes anterior).

**Excepción de Lunes (aplica solo a Fase B — pipeline en vivo):**  
El nuevo valor observado suele publicarse cerca de las 09:00 AM SCL; antes de ese horario, el sistema mantendrá el valor del cierre de la semana anterior. En la Fase A (backfill histórico), este caso no aplica porque los valores ya están publicados al momento de la descarga.


# 4. Estrategia de Ingesta Histórica (Backfilling)

Para el backtest se requiere una sincronización de tres fuentes con naturalezas distintas:

| Fuente      | Método de Extracción            | Granularidad | Notas de Implementación |
|-------------|--------------------------------|--------------|--------------------------|
| Binance     | Iteración vía `startTime`       | 1 min (klines) | Alta fidelidad. Persistencia directa a Parquet |
| Buda        | Iteración vía `last_timestamp`  | Trades raw   | Se descargan trades y se reconstruyen velas OHLCV de 1 min en la capa de transformación (Lambda). Incluye flag `is_interpolated` para velas sin actividad |
| MIndicador  | Endpoint anual (`/api/dolar/YYYY`) | Diario   | Descarga masiva. Menos de 10 peticiones para cubrir todo el histórico disponible |

## Regla de Sincronización Universal

Todas las fuentes se normalizarán a UTC y se almacenarán en el Data Lake utilizando una estructura de particionado Hive: `year=YYYY/month=MM/day=DD`.

Esto permitirá que AWS Athena realice joins temporales eficientes para calcular el spread histórico.