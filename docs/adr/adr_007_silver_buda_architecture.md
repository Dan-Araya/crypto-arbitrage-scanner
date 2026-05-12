# ADR-007: Arquitectura de la transformación Bronze→Silver para Buda

- **Status:** Accepted
- **Fecha:** 2026-05-07
- **Decisores:** Equipo del proyecto (autor único)
- **Issue/contexto:** Fase A.4 del pipeline — primera transformación
  Bronze→Silver del proyecto, sienta precedentes para las dos siguientes
  (Binance, FX).

---

## Contexto

La capa Bronze para Buda BTC-CLP estaba completa: 246 archivos JSON con
trades raw, ~2.67M trades cubriendo 2015-01-01 a 2026-05-01, validados
sin gaps ni solapes (`verify_bronze_coverage.py`).

El próximo paso era construir Silver: velas OHLCV de 1 minuto en formato
Parquet, particionadas por año/mes, listas para queries Athena. Esto requería
varias decisiones arquitectónicas no triviales:

1. **Cómo se ejecuta el cómputo:** ¿una Lambda procesa todo? ¿una por mes?
   ¿Glue/Spark?
2. **Cómo se organiza el storage:** ¿una tabla por exchange o tabla unificada?
3. **Qué semántica tienen los minutos sin trades:** ¿drop, NULL, o
   forward-fill?
4. **Qué columnas incluir en el schema:** la checklist original tenía 8;
   Bronze permitía derivar más a costo cero.
5. **Qué validaciones hacer:** ¿outliers? ¿filtros de calidad?
6. **Convenciones de naming:** consistencia con Bronze, layout en S3.

Este ADR documenta las decisiones tomadas en bloque porque están
lógicamente acopladas — no son independientes entre sí.

---

## Decisión 1: Single-Lambda procesa todo Buda en una invocación

**Decisión:** un único Lambda (`silver-buda`, 3008 MB / 5 min timeout) lee
los 246 archivos Bronze, concatena todos los trades en memoria, reconstruye
el OHLCV completo, y escribe los 134 archivos Parquet a S3 en una sola
ejecución.

### Alternativas consideradas

**A1 — Lambda por archivo Bronze.** Cada archivo dispara una invocación
independiente. Patrón canónico de pipelines AWS, pero el forward-fill que
cruza fronteras de archivo (último close de archivo N → primer minuto de
archivo N+1 sin trades) requiere un segundo paso de "stitching" o de
"forward-fill global". Dos pasadas, mayor complejidad operacional.

**A2 — Lambda por archivo + stitching de fronteras.** Variante de A1 que
intenta resolver el problema de fronteras leyendo los bordes de archivos
adyacentes. Frágil: el forward-fill semánticamente correcto requiere ver
toda la serie hasta el último trade real, no solo los bordes.

**A3 — Lambda por mes (1 invocación = 1 partición Silver).** Resuelve la
correspondencia 1:1 entre invocación y partición de salida. El forward-fill
sigue requiriendo lookback al mes anterior, y la coordinación cruzada
(Step Functions iterando meses) agrega infraestructura sin beneficio
proporcional.

**A4 — Glue/PySpark.** Adecuado para volúmenes que no caben en memoria.
Sobre-ingeniería para 1.6M trades documentados en Bronze.

### Justificación

Validación empírica del volumen: el archivo Bronze más grande es 1 MB; el
total estimado fue 1.6M trades (resultado real: 2.67M). En memoria con
pandas, son ~500 MB con margen de 6× sobre la asignación de Lambda. El
resampling y el reindex a grilla continua son operaciones lineales en
tiempo y memoria.

Procesar todo junto **trivializa** el problema de forward-fill cruzando
fronteras, que era la complejidad estructural que motivaba A1/A2/A3. Es
un caso donde la solución simple es estrictamente superior a la canónica
porque el volumen lo permite.

### Resultado empírico

47.7 segundos end-to-end, ~16% del timeout, ~$0.0015 USD por invocación.
Ver `data_quality.md` §10.

### Consecuencias y trade-offs

**Aceptados:**
- No paraleliza. Si Buda crece 100× en volumen, esta decisión se debe
  revisar.
- Pierde toda la corrida si falla a mitad. Aceptable para un backfill
  histórico de una sola corrida; los datos están congelados.
- No idempotente a nivel de archivo individual (no se puede reprocesar un
  mes específico sin reprocesar todo). Sí idempotente a nivel global: cada
  corrida produce exactamente el mismo conjunto de Parquet.

**Beneficios:**
- Una sola unidad operacional. Un Lambda, una corrida, un log.
- Forward-fill semánticamente correcto sin coordinación entre invocaciones.
- Costo y latencia bajos.

### Cuándo revisar

Si en Fase B se introduce ingesta incremental (live), single-Lambda deja de
servir: cada hora no se quiere reprocesar 11 años. La transformación
incremental requeriría arquitectura distinta (lectura del último estado
Silver + procesamiento del nuevo Bronze + append). Eso amerita un ADR
nuevo que supersede o complemente este.

---

## Decisión 2: Schema Silver unificado entre exchanges, no separado

**Decisión:** una sola tabla `unified_candles` en Glue Catalog, con columna
`exchange` discriminando filas. Layout en S3:

```
silver/backtest/unified_candles/
├── year=2017/month=08/
│   ├── binance.parquet
│   └── buda.parquet
├── year=2017/month=09/
│   ├── binance.parquet
│   └── buda.parquet
└── ...
```

Cada Lambda de transformación escribe su archivo en la misma partición sin
colisión.

### Alternativas consideradas

**B1 — Tablas separadas:** `silver/buda/...` y `silver/binance/...`, dos
tablas distintas en Glue, JOIN cross-table en Gold.

**B2 — Tabla única con prefix por exchange:** `silver/buda/`, `silver/binance/`,
pero declarar una tabla virtual con UNION en Athena. Híbrido ineficiente.

### Justificación

El query fundamental en Gold es un JOIN temporal entre exchanges:

```sql
SELECT b.timestamp, b.close_clp, n.close_clp,
       (b.close_clp - n.close_clp) / n.close_clp * 100 AS spread_pct
FROM unified_candles b JOIN unified_candles n ON b.timestamp = n.timestamp
WHERE b.exchange = 'buda' AND n.exchange = 'binance'
```

En tabla única, esto es un self-join sobre Parquet particionado: Athena
lo optimiza con partition pruning automático. En tablas separadas (B1) es
un cross-table JOIN más caro, y la lógica de partitioning consistente
entre dos tablas debe garantizarse manualmente.

El layout con dos archivos por partición (`buda.parquet`, `binance.parquet`)
permite que ambas Lambdas escriban a la misma ubicación sin coordinación —
los nombres no chocan, S3 maneja la concurrencia, y Athena lee ambos
archivos como filas de una sola tabla.

### Consecuencias

**Contrato de schema obligatorio:** ambos Lambdas (`silver-buda` ya
deployado, `silver-binance` pendiente) deben escribir Parquet con
**exactamente** el mismo schema (columnas, tipos, orden). Una desviación
de tipo (e.g., `float32` vs `float64`) rompe el read en Athena. El schema
canónico está documentado en `data_quality.md` §7 y replicado como contrato
explícito en cada handler:

```python
schema = pa.schema([
    ("timestamp", pa.timestamp("ns", tz="UTC")),
    ("exchange", pa.string()),
    ("open_clp", pa.float64()),
    # ... 11 columnas total
])
```

---

## Decisión 3: Una fila por minuto en grilla continua (forward-fill, no drop)

**Decisión:** Silver almacena una fila por minuto desde el primer trade
real hasta el último trade observado, sin gaps. Minutos sin trades reales
se rellenan con `is_interpolated=true`, OHLC iguales al último close
conocido, volúmenes y trade_count en cero.

### Alternativas consideradas

**C1 — Drop minutos vacíos.** Solo se almacenan minutos con actividad real.
Más compacto pero rompe el JOIN temporal con Binance (que sí tiene 1 fila
por minuto), produciendo NULLs asimétricos en Gold.

**C2 — Forward-fill solo si gap < N minutos.** Compromiso. Decidir un
umbral N introduce un parámetro arbitrario sin justificación analítica.

### Justificación

El caso de uso (detección de spreads de arbitraje) requiere que cada
timestamp de Binance tenga un timestamp comparable de Buda. Si Buda no
tiene fila a las 14:32 porque no hubo trades, el JOIN no produce esa
comparación — y precisamente los minutos de baja actividad son donde las
divergencias de precio son más probables (mercado ilíquido, spreads
amplios). Drop produce ceguera estadística sistemática justo donde más
información hay para extraer.

El forward-fill no inventa actividad: dice "el último precio conocido es
el mejor estimador del precio actual en ausencia de nueva información",
que es económicamente correcto. La columna `is_interpolated` permite al
consumidor en Gold filtrar a su criterio (analizar solo minutos con trades
reales, o usar la grilla completa).

### Consecuencias

- Silver es ~5× más grande que la alternativa "drop" (5.83M filas vs ~1M).
- El 80.9% de las filas son interpoladas. Es una característica del par
  BTC-CLP, no un defecto del pipeline. La distribución por año
  (`data_quality.md` §10) es información analítica valiosa per se.
- Parquet+Snappy comprime brutalmente bien las run-length de
  forward-fill: 5.83M filas en 108 MiB total.

### Decisión asociada: nombre del flag

Se eligió `is_interpolated` sobre las alternativas evaluadas:
- `is_synthetic` (mencionado en versiones tempranas de `data_quality.md`):
  demasiado general, sugiere que la fila no existió.
- `is_imputed`: terminología estándar de estadística, técnicamente correcta.
- `is_interpolated`: descriptiva del método (forward-fill ≈ interpolación
  constante hacia adelante, LOCF).

Se prefirió `is_interpolated` por ser inmediatamente legible para un
ingeniero de datos sin background en estadística, y porque el proyecto no
usa otras formas de interpolación que generarían ambigüedad.

---

## Decisión 4: Schema preservado a costo marginal cero (11 columnas, no 8)

**Decisión:** Silver incluye `buy_volume_btc`, `sell_volume_btc` y
`trade_count` además del OHLCV básico, llevando el schema a 11 columnas.

### Alternativa considerada

Schema mínimo de 8 columnas (timestamp, exchange, OHLC, volume,
is_interpolated), como estaba en la checklist original.

### Justificación

Los tres campos extra son derivables trivialmente de Bronze:

- **Buda**: el campo `direction` (índice 3) ya está en cada trade.
  `buy_volume_btc = sum(amount where direction='buy')` es una agregación
  más, pareja al volumen total.
- **Binance**: el campo `taker_buy_base` (índice 9) viene pre-agregado
  por kline. Es lectura directa.
- **trade_count**: lectura directa de Bronze en ambos casos.

El **principio operacional** que se establece con esta decisión:

> Silver preserva todo lo que Bronze tiene y que la normalización puede
> transformar con costo marginal cero. Lo que se omite es lo que requeriría
> reingesta o cálculos complejos que pertenecen a Gold.

El test de "costo marginal cero" tiene dos criterios: (a) el dato existe en
Bronze sin reingesta, y (b) la transformación es agregación o mapeo
posicional, no inferencia. Ambos se cumplen para los tres campos.

### Beneficio

Habilita análisis de microestructura en Gold sin reprocesar Silver:
- **Order flow imbalance** = `(buy_volume - sell_volume) / volume`,
  predictor clásico de movimientos de precio.
- **Densidad de actividad** = `trade_count` por minuto, proxy de
  intensidad de trading separado del volumen agregado.

Para un proyecto con énfasis en arbitraje, ambos son señales potencialmente
útiles. El costo de no incluirlos en Silver sería volver a procesar 2.67M
trades cuando se quieran usar.

### Consecuencias

- Schema más amplio que la checklist original. Documentado en
  `data_quality.md` §7 como contrato canónico.
- Simetría buy/sell entre exchanges: ambos campos `buy_volume_btc` tienen
  semántica equivalente (taker side = buy), facilitando comparaciones
  cross-exchange en Gold sin transformaciones adicionales.

---

## Decisión 5: Silver como agregación pura (sin detección de outliers)

**Decisión:** Silver no detecta, no marca y no descarta outliers. La
checklist original mencionaba "velas donde |close - close_anterior| > 10%
en 1 minuto"; esa detección se omite deliberadamente.

### Alternativas consideradas

**E1 — Descartar outliers en Silver.** Silver entrega "datos limpios".
**E2 — Flagear sin descartar (`is_outlier`).** Silver entrega "datos
anotados", consumidor decide.
**E3 — No tocar.** Silver es agregación, Gold juzga.

### Justificación

La definición de "outlier" depende del caso de uso. En BTC-CLP histórico
en años de baja liquidez, un movimiento de 10% en 1 minuto puede ser un
trade real (mercado ilíquido y volátil), no un error. Descartar (E1)
elimina información histórica genuina. Flagear (E2) requiere acordar el
umbral en Silver, que luego es difícil de cambiar sin reprocesar.

E3 es consistente con la línea editorial del medallion architecture
("Silver es agregación pura, Gold es lógica de negocio"). Al tratarse de
un proyecto con un consumidor único (Gold), la detección y el filtrado se
hacen donde se conoce el caso de uso específico, con el umbral apropiado
a la pregunta concreta.

### Consecuencias

- Gold debe implementar su propia lógica de calidad si la necesita.
- Silver es estable: cambios en la definición de outlier no requieren
  reprocesar Silver.
- El `data_quality.md` §10 deja en evidencia mediante `is_interpolated`
  cuáles minutos no son trades reales. Esa señal es suficiente para que
  Gold haga filtrado básico sin necesidad de outlier detection en Silver.

### Decisión relacionada: validaciones defensivas (warning, no descarte)

Se evaluó agregar validaciones tipo `assert price > 0, amount >= 0` en
Bronze→Silver. Pendiente de implementación. La decisión fue **logear como
warning sin descartar**, alineado con E3: si un campo es absurdo, queremos
saberlo, pero no decidimos en Silver qué hacer con él. La verificación de
Bronze (`verify_bronze_coverage.py`) cubre cobertura temporal, no validez
de campos individuales — esta validación complementa esa cobertura.

---

## Decisión 6: Capa medallion, no Step Functions para Silver

**Decisión:** el Lambda `silver-buda` se invoca manualmente (CLI/console)
para Fase A. No se crea Step Function que lo orquesta.

### Justificación

Las Step Functions del proyecto (`btc_orchestrator`, `buda_orchestrator`,
`mindicador_orchestrator`) existen para iterar sobre **listas de períodos**
en Bronze: el patrón Map → Lambda por período. Silver Buda no itera nada
externo: una sola invocación procesa el dataset completo. Una Step Function
con un solo Task degenerado no aporta — sí agrega un recurso, una IAM role
y una capa de indirección.

Por el contrario, `silver-binance` (pendiente) probablemente sí amerita
Step Function: la transformación involucra JOIN con FX que se resuelve
mejor por particiones temporales. Esa decisión se tomará en su propio ADR.

### Consecuencias

- Para Fase A, `silver-buda` se invoca con `aws lambda invoke`. Documentado
  en el README/runbook del repo.
- Si en Fase B se introduce ingesta incremental, esto se revisa: el patrón
  natural sería EventBridge → Lambda, sin Step Function intermedia.

---

## Convenciones de naming establecidas en este ADR

Se establecen convenciones que aplican a todos los Lambdas Silver/Gold
futuros:

- **Directorio del Lambda:** `lambdas/<layer>_<source>/` —
  `lambdas/silver_buda/`, `lambdas/silver_binance/`, etc. La capa como
  prefijo agrupa por nivel de procesamiento al listar el repo,
  facilitando navegación.
- **Nombre del recurso AWS:** `<layer>-<source>` con guion (Lambda no
  acepta underscore en nombres de función). `silver-buda`,
  `silver-binance`, etc.
- **Path en S3:** `<layer>/backtest/<table_name>/year=YYYY/month=MM/<discriminator>.parquet`.
  `unified_candles` es el `table_name` para Silver. `discriminator` es
  el exchange (cuando aplica) o `data` (cuando hay un solo archivo
  por partición).
- **Layer público de pandas:** `AWSSDKPandas-Python311:31` declarado
  como variable Terraform pineada. Cambios de versión requieren actualización
  explícita de `variables.tf` (no auto-tracking).

---

## Referencias

- `data_quality.md` §5, §7, §10 — comportamiento implementado y
  resultados empíricos.
- `api_discovery.md` §1.3, §2.3 — contratos de Binance y Buda.
- `pipeline_design.md` — arquitectura general del pipeline.
- `lambdas/silver_buda/handler.py` — implementación.
- `infra/main.tf` — definición de la Lambda y recursos asociados.

---

## Trabajo derivado

Este ADR sienta el patrón para los dos Lambdas Silver pendientes:

1. **`silver-binance`** — debe escribir al mismo path
   `silver/backtest/unified_candles/` con archivo `binance.parquet` por
   partición, schema idéntico al de este ADR. El JOIN con FX (USDT→CLP)
   es la complejidad propia de Binance Silver y amerita su propio ADR.
2. **`silver-fx`** — la salida no es velas, sino una serie diaria normalizada
   con forward-fill de fines de semana y festivos. No se integra al schema
   `unified_candles`; va a su propio path. Amerita su propio ADR para
   discutir el path y la semántica del forward-fill.dan@customer:~/Escritorio/side-proyects/crypto-arbitrage-scanner$ 
