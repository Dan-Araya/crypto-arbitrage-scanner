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
ejemplo, noviembre 2017 tiene 43,200 records pero noviembre tiene 30 días
(43,200 = exactamente el total esperado, OK), mientras que diciembre 2017
tiene 44,515 vs 44,640 esperados (faltan ~125 minutos).

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
(que también requiere reconstrucción de velas vacías). El flag permite filtrar
en queries analíticas si es necesario.

---

## 5. Velas reconstruidas en Buda (placeholder)

> _Esta sección se completará cuando se implemente el handler de Buda._

Resumen anticipado: dado que Buda no expone un endpoint de klines y los trades
en BTC-CLP son esporádicos (mercado de baja liquidez), el pipeline reconstruye
velas de 1 minuto a partir de trades raw. Las velas resultantes incluirán un
flag `is_interpolated = true` para los minutos sin actividad.

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