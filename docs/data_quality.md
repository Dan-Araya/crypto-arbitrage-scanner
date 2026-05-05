# Data Quality Considerations

A continuación se documentan observaciones, decisiones y reglas de calidad de datos del pipeline. Mientras `api_discovery.md` describe contratos de APIs externas, aquí se documentan los comportamientos del pipeline propio: duplicaciones esperadas, estrategias de deduplicación, manejo de discontinuidades
temporales, y reglas de validación entre capas.

---

## 1. Capa Bronze: principios

La capa bronze es **fiel a la fuente**. Almacena la respuesta cruda de la API de Binance en JSON, con un envoltorio de metadata mínimo (`source`, `ingestion_timestamp_utc`,
`schema_version`, etc.).

**Reglas:**

- Bronze nunca modifica, deduplica ni filtra datos de la fuente.
- Cualquier "ruido" estructural (duplicados, valores faltantes, solapamientos de paginación) se preserva intacto.
- La limpieza es responsabilidad exclusiva de la capa silver.

**Justificación:** En caso de bugs en la lógica de limpieza, o si en el futuro se necesita una transformación distinta, bronze con datos crudos permite reprocesar sin volver a llamar a las APIs.

---

## 2. Solapamiento de un minuto entre archivos consecutivos (Binance)

### Comportamiento observado

El parámetro `endTime` de la API de Binance (`/api/v3/klines`) es **inclusivo**.
Cuando se hacen dos invocaciones consecutivas con rangos `[T0, T1]` y `[T1, T2]`,
la kline con `open_time = T1` aparecerá en ambos archivos.

### Validación empírica

Una invocación con `start_ms = 1704067200000` y `end_ms = 1704153600000`
(exactamente 1440 minutos = 1 día) devolvió **1441 records**, no 1440. La kline extra corresponde a `open_time = 1704153600000` (= `end_ms`), confirmando el comportamiento inclusivo.

### Implicación en el backfill mensual

El generador de períodos (`generate_backfill_periods.py`) divide el histórico en meses consecutivos donde `start_ms` del mes N+1 es muy cercano al `end_ms` del mes N. Aunque el script usa el último milisegundo del mes (`23:59:59.999`) para reducir el solape, la kline del primer minuto del mes siguiente puede
aparecer también en el archivo del mes anterior, dependiendo de cómo Binance mapee los timestamps a klines.

**Resultado:** se espera al menos 1 record duplicado por frontera de mes en el
peor caso (≤ 111 duplicados sobre ~4.5M de records totales, equivalente a
< 0.003% del dataset).

### Resolución

Deduplicación en la capa silver usando `open_time` como clave natural:

```sql
SELECT DISTINCT ON (open_time) *
FROM bronze_klines
ORDER BY open_time, ingestion_timestamp_utc DESC;
```

O en PySpark:

```python
df.dropDuplicates(["open_time"])
```

### Alternativa descartada

Restar 1 ms al `end_ms` antes de enviar el request (`endTime = end_ms - 1`).

**Razones del descarte:**
1. Depende de un comportamiento no documentado oficialmente. Si Binance cambiara
   la semántica del parámetro en el futuro, generaría gaps de datos en lugar de
   solapamientos (un fallo silencioso peor que el actual).
2. Viola el principio de "bronze fiel a la fuente" — sería el handler ajustando
   datos para conveniencia de capas posteriores.
3. La deduplicación en silver es un patrón estándar en arquitecturas medallion,
   bien soportado por Spark, Athena y dbt. No agrega complejidad significativa.

---

## 3. Meses sin actividad de mercado (BTCUSDT pre-listing)

### Comportamiento observado

BTCUSDT comenzó a operar en Binance alrededor del **17 de agosto de 2017**.
Para meses anteriores a esa fecha, la API devuelve un array vacío (`[]`).

### Validación empírica

Conteo de registros descargados en meses tempranos:

| Mes | Records | Estado |
|-----|---------|--------|
| 2017-06 | 0 | Pre-listing |
| 2017-07 | 0 | Pre-listing |
| 2017-08 | 21,360 | Mes parcial (desde ~17 ago) |
| 2017-09 | 42,781 | Activo |
| 2017-10 | 44,640 | Mes completo |
| 2017-12 | 44,515 | Activo |
| 2024-01 | 44,640 | Referencia mes completo |

### Resolución

El handler maneja el caso vacío con `if not data: break`, preservando un
archivo bronze con `metadata.records_count = 0` y `data: []`. Esto es
intencional: distinguir "no había datos en la fuente" de "no se descargó el
mes" requiere persistir el archivo aunque esté vacío.

### Implicación en silver

La capa silver debe filtrar archivos con `records_count = 0` antes de
agregar al dataset analítico, o tolerarlos y producir particiones vacías.
Recomendación: filtrar en silver para que los downstream consumers no tengan
que manejar particiones huérfanas.

---

## 4. Klines faltantes intra-mes

### Observación

Algunos meses presentan conteos ligeramente menores al teórico (44,640 para
meses de 31 días, 43,200 para 30 días, 40,320 para febrero no bisiesto). Por
ejemplo, diciembre 2017 tiene 44,515 records vs los 44,640 esperados para un
mes de 31 días: faltan 125 minutos.

### Hipótesis

Períodos de mantenimiento de Binance, especialmente frecuentes en los primeros
años de operación (2017-2018). Binance no inserta klines vacías por minuto
faltante; simplemente omite el intervalo.

### Resolución en silver

**Opción A — Forward fill:** rellenar minutos faltantes con el último close
conocido y `volume = 0`, marcando con flag `is_synthetic = true`.

**Opción B — Mantener gaps:** dejar la serie con discontinuidades. El análisis
posterior debe ser tolerante a timestamps no equiespaciados.

**Decisión actual:** Opción A para consistencia con el tratamiento de Buda
(donde la mayoría de minutos no tiene actividad y la reconstrucción OHLCV
exige tomar una postura sobre los huecos — ver §5). El flag permite filtrar
en queries analíticas si es necesario.

---

## 5. Capa Bronze para Buda: granularidad adaptativa, archivos atómicos y reconstrucción OHLCV

A diferencia de Binance, donde una invocación cubre un mes y produce un
archivo, en Buda la unidad de archivo bronze **no es fija**. Esta sección
documenta las consecuencias para el pipeline.

### 5.1 Granularidad por archivo

El generador `generate_buda_periods.py` decide el tamaño de cada archivo bronze
en función del volumen muestreado del día 15 de cada mes (proxy del volumen
mensual completo). Las posibles granularidades son:

| Granularidad | Archivos por mes | Cuándo se aplica                          |
|--------------|------------------|-------------------------------------------|
| Mensual      | 1                | Volumen bajo (`lambda_time_est ≤ 400s`)   |
| Quincenal    | 2                | Volumen medio (400 < est ≤ 1000s)         |
| Semanal      | 4-5              | Volumen alto (est > 1000s)                |
| Sub-semanal  | variable         | Override puntual (ver §5.4)               |

**Implicación para silver:** un proceso que itere sobre `bronze/backtest/buda/year=YYYY/month=MM/`
debe esperar 1 a 5 archivos por mes, sin asumir 1:1.

### 5.2 Atomicidad por archivo

El handler de Buda mantiene los trades en memoria durante toda la paginación
y sólo invoca `s3.put_object()` al final, justo antes de retornar. Esta
decisión deliberada produce dos garantías útiles:

1. **No hay archivos parciales corruptos.** Si la Lambda muere por timeout o
   por excepción, no se escribe nada en S3. El período se ejecuta atómicamente
   o no se ejecuta.
2. **Idempotencia simple.** Re-ejecutar el mismo período (mismo `start_ms`)
   sobreescribe el archivo anterior porque la S3 key se deriva determinísticamente
   de `symbol` y `start_ms`. Sin lógica especial de reconciliación.

El costo es que un período cercano al timeout de Lambda pierde todo su
trabajo si falla. Esto motivó la heurística de granularidad adaptativa
(cuanto mayor el volumen estimado, menor la unidad de pérdida en caso de
fallo).

### 5.3 Trades raw vs OHLCV reconstruido

Buda no expone un endpoint de klines (ver `api_discovery.md` §2.5). Bronze
almacena los trades raw — array de tuplas `[ts, amount, price, direction, trade_id]`
exactamente como llegan de la API, sin transformación. La construcción de
velas OHLCV es responsabilidad de silver, donde:

- **Bucket por minuto** usando `ts // 60_000` (truncamiento del timestamp en ms).
- **Open** = precio del primer trade del minuto (cronológico ascendente, que
  bronze ya garantiza por la conversión hecha en el handler).
- **Close** = precio del último trade del minuto.
- **High/Low** = max/min de los precios del minuto.
- **Volume** = suma de los `amount` (en BTC).
- **Trades** = conteo de entries en el minuto.

### 5.4 Minutos sin actividad: la realidad de un mercado de baja liquidez

BTC-CLP es un par de baja liquidez incluso en los años de mayor volumen.
Empíricamente, la mayoría de minutos no tienen ningún trade. Por ejemplo:

- Una semana de enero 2021 (período de actividad muy alta, durante el rally
  hacia $40k) tuvo 25.790 trades en 7 días = ~2.5 trades/minuto promedio,
  pero distribuidos no uniformemente: muchos minutos con 0 trades, algunos
  con bursts.
- Meses tempranos (2015) tienen literalmente decenas de trades en todo el
  mes, dejando >43.000 minutos vacíos sobre los ~44.640 posibles.

**Decisión:** silver almacena **una fila por minuto**, con flag
`is_interpolated: true` cuando no hubo trades. Los campos OHLC heredan el
último precio conocido (forward-fill); volume = 0; trades = 0.

**Justificación:** el caso de uso (detección de spreads de arbitraje
Binance-Buda) requiere un timestamp continuo para alinearse con los klines
de Binance, que sí tienen 1 fila por minuto. Almacenar sólo minutos con
actividad obligaría a cada query downstream a hacer un join asimétrico contra
una serie continua. Es más simple resolver esa asimetría una vez en silver.

**Consecuencia:** las particiones silver de Buda son intencionalmente
"ruidosas" — la mayoría de filas tienen `is_interpolated: true`. Las queries
analíticas que sólo quieran trades reales filtran por ese flag.

### 5.5 Períodos sin mercado (análogo a Binance pre-listing)

Buda fue lanzado como SurBTC en enero de 2015. Los primeros meses tienen
actividad mínima o nula:

| Mes | Records | Tamaño archivo | Estado                           |
|-----|---------|----------------|----------------------------------|
| 2015-01 | 0  | 305 B  | Pre-mercado real (archivo vacío) |
| 2015-02 | 0  | 305 B  | Pre-mercado real                 |
| 2015-03 | ~5 | 1.3 KiB | Mercado naciente                |
| 2015-04 | ~50 | 5.4 KiB | Mercado naciente                |

El handler descarga estos meses sin error: la API responde con
`entries: []` y `last_timestamp: null`, lo cual el handler interpreta
correctamente como "fin de stream" y persiste un archivo bronze con
`records_count: 0`. Mismo principio que en Binance pre-listing (§3): bronze
preserva la verdad histórica de la fuente.

Silver debe filtrar estas particiones vacías o tolerarlas como días con
todos los minutos `is_interpolated: true` (sin precio conocido previo, los
campos OHLC quedarían NULL).

### 5.6 Limitación conocida del sample del día 15

La heurística de granularidad usa el volumen del **día 15** de cada mes como
proxy. Funciona razonablemente cuando el volumen es relativamente uniforme
dentro del mes, pero **subestima sistemáticamente meses con eventos exógenos
concentrados temporalmente**. Casos observados durante el backfill:

- **Diciembre 2020**: el sample del 15 cayó en zona de relativa calma; el
  rally final del 24-31 de diciembre (BTC $19k → $29k) disparó un volumen
  ~3x el muestreado. Resultado: timeout en la quincena 16-31. Mitigación
  aplicada: override puntual a granularidad semanal sólo para esa quincena.
- **Enero 2021**: el sample subestimó el efecto sostenido del rally; algunas
  semanas terminaron muy cerca del timeout incluso con granularidad semanal.
  Mitigación aplicada: bajar el throttle del handler de 3.0s a 2.0s, y luego
  a 1.0s tras validar empíricamente que Cloudflare no aplica rate limiting
  reactivo a esa frecuencia desde IPs de Lambda.

**Patrón general:** la heurística de sampling es razonable pero falible para
meses con distribuciones de volumen muy heterogéneas. La estrategia de
mitigación combina (a) márgenes de seguridad conservadores en los thresholds
del generador (factor 2.25x sobre el timeout), (b) overrides puntuales
sub-semanales para casos extremos, y (c) calibración empírica del throttle
del handler.

### 5.7 Tabla de hallazgos para silver

Resumen de aspectos que silver debe manejar al consumir bronze/buda:

| Aspecto                                  | Tratamiento en silver                       |
|------------------------------------------|---------------------------------------------|
| Múltiples archivos por mes               | Iterar sin asumir 1:1; ordenar por start_ms |
| Trades raw (no OHLCV pre-agregado)       | Reconstruir velas de 1 min (§5.3)           |
| Mayoría de minutos sin actividad         | Forward-fill con flag `is_interpolated`     |
| Meses pre-mercado con records_count = 0  | Filtrar o tolerar como NULLs                |
| Trade IDs no scopeados al market         | No usar para conteos (ver `api_discovery.md` §2.3) |

---

## 6. Discontinuidad cambiaria (USD/CLP — MIndicador.cl)

> _Esta sección se completará cuando se implemente el handler de MIndicador._

Resumen anticipado: el "Dólar Observado" no se publica en sábados, domingos
ni festivos chilenos. La regla de forward-fill aplica el último valor conocido
(típicamente el del viernes anterior) hasta que se publica el siguiente.

---

## 7. Esquema de tipado a través de capas

| Campo | Bronze (JSON desde API) | Silver (Parquet) | Notas |
|-------|------------------------|------------------|-------|
| `open_time` | Long (ms) | Timestamp(ms, UTC) | Conversión a tipo nativo |
| `open`, `high`, `low`, `close` | String | float64 | Justificado en `api_discovery.md` §1.4 |
| `volume`, `quote_volume` | String | float64 | |
| `trades_count` | Integer | int32 | |
| `is_synthetic` | (no existe) | boolean | Generado en silver |

**Decisión sobre precision:** se usa `float64` (no `Decimal`) por las razones
documentadas en `api_discovery.md`. Esto es aceptable para el caso de uso
analítico (detección de spreads del orden de 0.5%-2%, donde el error de
redondeo de IEEE 754 a ~10⁻¹² es despreciable). En un sistema con ejecución
real de órdenes, esta decisión debería revisarse.

---

## 8. Validaciones automatizadas (futuro)

Pendiente de implementar:

- **Schema validation:** verificar que los archivos bronze sigan el contrato
  documentado en `api_discovery.md` (longitud del array de klines = 12 campos,
  tipos consistentes, etc.).
- **Range checks:** detectar precios o volúmenes fuera de rangos plausibles
  (negativos, valores extremos que sugieran corrupción).
- **Continuity checks:** medir gaps de tiempo entre klines consecutivas en
  silver y alertar si exceden umbrales esperados.
- **Cross-source consistency:** validar que los timestamps de Binance, Buda y
  MIndicador puedan unirse correctamente en gold (todos en UTC, sin zonas
  horarias mezcladas).

Herramienta candidata: [Great Expectations](https://greatexpectations.io/) o
[Soda Core](https://www.soda.io/). Pendiente de evaluación.

---

## 9. Verificación end-to-end de cobertura temporal (Buda bronze)

Al cierre del backfill se ejecutó una validación programática de cobertura
sobre todos los archivos de `bronze/backtest/buda/`. El propósito es responder
con datos a las dos preguntas que importan para integridad temporal:

1. ¿Cubrimos todos los días del período? (no hay gaps)
2. ¿Algún día está cubierto por más de un archivo? (no hay solapes)

### Procedimiento

Para cada archivo bronze se extrajo `metadata.range_start_ms` y
`metadata.range_end_ms`. La lista de pares `[start, end)` se ordenó por
`start_ms`. Se chequearon dos invariantes consecutivas:

- **Sin solapes:** `start[i+1] >= end[i]` para todo i.
- **Sin gaps:** `start[i+1] == end[i]` para todo i (estrictamente).

### Resultado (ejecución del 5 de mayo de 2026)

| Métrica                  | Valor                                  |
|--------------------------|----------------------------------------|
| Archivos analizados      | 246                                    |
| Solapes detectados       | 0                                      |
| Gaps detectados          | 0                                      |
| Cobertura inicial        | 2015-01-01T00:00:00+00:00              |
| Cobertura final          | 2026-05-01T00:00:00+00:00              |
| Días cubiertos           | 4138                                   |

Todos los archivos respetaron la semántica `[start_ms, end_ms)` half-open
acordada (ver `pipeline_design.md`), y el ensamblado de archivos por
`start_ms` consecutivo produjo una cobertura continua sin overlap.

### Implicación

Cualquier query downstream que itere todos los archivos de bronze/buda en
orden de `start_ms` puede asumir que está leyendo el histórico completo y
contiguo de BTC-CLP en Buda, sin necesidad de deduplicación por solape ni
de imputación por gap a nivel de archivos (la imputación por minuto sin
trades es una preocupación distinta — ver §5.4).

### Por qué importa

La granularidad adaptativa de bronze/buda (§5.1) y los overrides puntuales
(§5.6) producen una mezcla de archivos mensuales, quincenales, semanales y
sub-semanales. Sin esta verificación, sería razonable temer un solape o gap
entre, por ejemplo, una quincena del lote original y una semana del override
de diciembre 2020. La verificación confirma que la matemática de fechas en
el generador y los overrides se aplicó consistentemente.

El script de validación se preserva para ejecuciones periódicas y para
extensión a otras fuentes (Binance ya tiene un análogo natural — el
`open_time` actuá como clave continua).