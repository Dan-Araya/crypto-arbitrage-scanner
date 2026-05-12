# ADR-009: Arquitectura de la transformación Bronze→Silver para FX (USD/CLP)

- **Status:** Accepted
- **Fecha:** 2026-05-11
- **Decisores:** Equipo del proyecto (autor único)
- **Issue/contexto:** Fase A.4 del pipeline — tercera y última transformación
  Bronze→Silver. Cierra la capa Silver. Hereda decisiones de ADR-007 y ADR-008
  donde aplican, y se aparta deliberadamente del patrón de particionado en
  un punto justificado por la naturaleza dimensional de la tabla FX.

---

## Contexto

La capa Bronze para FX estaba completa: un único archivo JSON
(`bronze/backtest/fx/usdclp_dolar_mindicador.json`, 109 KB) con la serie
USD/CLP del Banco Central de Chile publicada por MIndicador.cl. 2825
registros nativos cubriendo 2015-01-02 a 2026-05-06, con huecos
sistemáticos en fines de semana y feriados CL (MIndicador.cl solo publica
días hábiles bancarios).

Silver Buda (ADR-007) y Silver Binance (ADR-008) ya estaban deployados.
Ambos escriben a `silver/backtest/unified_candles/year=YYYY/month=MM/`
particionado, y consumen FX vía el módulo común `lambdas/common/fx.py`
(`build_fx_dict` + `lookup_fx_for_utc_ms`) leyendo Bronze directamente.

silver-fx no es estrictamente necesario para que silver-binance funcione
(este último ya consume FX desde Bronze). Existe por dos razones:

1. **Exponer FX como tabla queryable** en Athena. Sin un Silver FX, no
   hay manera limpia de hacer JOIN cross-tabla `unified_candles ⨝ fx` en
   queries de análisis sin re-procesar el JSON Bronze.
2. **Materializar la lógica de ffill** en el data lake. El forward-fill
   hoy vive solo en memoria de Lambda; persistirlo en Silver lo hace
   auditable, reproducible y disponible para consumers fuera del
   pipeline (notebooks, queries ad-hoc).

Las decisiones nuevas que documenta este ADR:

1. **Particionado:** ¿silver-fx replica `year=YYYY/month=MM/` como buda
   y binance, o se aparta del patrón?
2. **Schema:** ¿qué columnas, qué tipos, qué granularidad de flag de
   ffill?
3. **Semántica de la columna `date`:** ¿es fecha Santiago publicada por
   la fuente, fecha UTC, o algo derivado?

Las tres se cerraron antes de escribir código.

---

## Decisión 1: archivo único sin particionar

silver-fx escribe un Parquet único en
`silver/backtest/fx/usdclp.parquet`, **no** particionado por
year/month.

Justificación cuantitativa: la serie completa son 4143 filas (incluyendo
ffill). Particionar por year/month generaría ~108 archivos de ~40 filas
cada uno, lo cual es el antipatrón clásico de *small files problem* en
data lakes: cada archivo paga overhead de footer Parquet, metadata,
listado S3 y apertura por Athena. El costo de query sube sin beneficio
de pruning.

Justificación arquitectónica: el particionado por tiempo solo aporta
valor cuando los predicados de query tocan la columna de partición. En
este pipeline, el filtro temporal vive en `unified_candles`
(particionada), no en `fx`. El consumo natural de `fx` en Athena es
broadcast join: tabla de dimensión completa en memoria contra tabla de
hechos particionada. Tener un solo archivo facilita justamente eso.

Esta decisión se aparta deliberadamente de la uniformidad visual con
las otras Silver. La asimetría está justificada por el rol de la tabla
(dimension vs fact en términos de Kimball, *The Data Warehouse Toolkit*
3ra ed., 2013) y no por ahorro de complejidad.

**Sobre partition projection en Glue:** la inconsistencia entre tablas
particionadas (`unified_candles`) y no particionadas (`fx_usdclp`) no
afecta el catálogo porque partition projection se configura por tabla,
no por database. Ambas pueden coexistir en `arbitraje_btc` sin
conflicto.

---

## Decisión 2: schema mínimo de 3 columnas

| Campo        | Tipo            | Semántica                                          |
|--------------|-----------------|----------------------------------------------------|
| `date`       | `pa.date32()`   | Fecha publicada por MIndicador (calendario Santiago). |
| `usdclp`     | `pa.float64()`  | Valor publicado o ffilled, según `is_ffilled`.     |
| `is_ffilled` | `pa.bool_()`    | `true` si la fila proviene de forward-fill.        |

Schema explícito vía `pyarrow.schema`, sin inferencia desde pandas.
Coherente con silver-buda y silver-binance.

Decisiones de tipo:

- **`date` como `date32` nativo**, no string. 4 bytes por fila vs ~10
  para `YYYY-MM-DD`, operaciones aritméticas nativas en Athena.
- **`usdclp` como `float64`**, no `decimal(10,4)`. Bronze publica
  double; el ruido de punto flotante en 64 bits es del orden de 1e-13
  relativo, irrelevante para el análisis de arbitraje (que opera con
  spreads del orden de 0.1-1%). Si en el futuro se agregara PnL
  contable que requiera precisión exacta, se migraría con un cast
  determinístico.
- **`is_ffilled` como `bool`**, no `int` con distancia al último valor
  publicado. YAGNI: el flag cubre el 95% del análisis de calidad. La
  distancia se puede derivar en query con window function.

---

## Decisión 3: `date` representa fecha publicada por la fuente, sin conversión

La columna `date` contiene literalmente la fecha que MIndicador.cl
publica en su JSON (calendario Santiago, día hábil bancario CL). No se
convierte a UTC ni se ajusta de ninguna manera.

Esto es coherente con el principio "Silver no juzga" (ADR-007 §filosofía,
ADR-008 §invariantes): silver-fx describe `(fecha CL publicada, valor)`.
El consumidor del JOIN cross-tabla en Athena es responsable de convertir
sus timestamps UTC a fecha Santiago antes de hacer match contra esta
columna. La regla de conversión está implementada en
`common.fx.lookup_fx_for_utc_ms` y documentada en ADR-008 §JOIN
timezone.

---

## Decisión 4: reúso de `common.fx.build_fx_dict`

silver-fx reusa la función `build_fx_dict` ya validada en silver-binance,
sin modificarla. El handler de silver-fx es ~80 líneas: lee bronze,
identifica original_dates (parseando el JSON una vez para conocer qué
fechas son publicadas pre-ffill), llama `build_fx_dict` (que devuelve el
dict ffilled), construye los arrays de pyarrow, escribe Parquet.

El doble parseo del JSON (una vez en silver-fx para original_dates, otra
adentro de `build_fx_dict` para construir el dict) tiene costo
despreciable (~5ms sobre 109 KB) y mantiene `build_fx_dict` intacta y
reutilizable sin acoplamiento.

Esta Lambda se agrega a `LAMBDAS_NEEDING_COMMON` en `build_lambdas.sh`,
empaquetando `common/` dentro del zip (decisión α de ADR-008: no Lambda
Layer para módulos pequeños de uso interno).

---

## Configuración Terraform

| Parámetro    | Valor | Justificación                                    |
|--------------|-------|--------------------------------------------------|
| `memory_size`| 512   | 109 KB de input + 4143 filas de output. Holgado. |
| `timeout`    | 60    | Run real ~6s. Margen 10x sin enmascarar bugs.    |
| `layers`     | aws_sdk_pandas_layer | Reusa layer compartida con buda/binance (pyarrow). |
| Env vars     | `BUCKET_NAME` única | Paths son constantes del handler; particionarlos sería falsa flexibilidad. |

Diferencia explícita con silver-binance: silver-fx no usa 3008 MB ni 600s
porque su volumen no lo justifica. La asimetría en configuración refleja
asimetría real en el trabajo computacional, no inconsistencia.

---

## Consecuencias

**Positivas:**

- Capa Silver completa: 3 de 3 Lambdas operativas. Modelo medallion
  cerrado conceptualmente.
- FX queryable directamente en Athena como tabla externa, sin
  re-procesar Bronze.
- Implementación mínima (~80 líneas) reusando código ya validado.

**Negativas:**

- Inconsistencia visual al inspeccionar el bucket: dos tablas Silver
  particionadas (buda, binance) y una sin particionar (fx). Requiere
  documentación en `data_quality.md` para que onboardings futuros
  entiendan la asimetría.
- Si se migrara a Iceberg en el futuro, el path de fx habría que
  reorganizarlo bajo convención Iceberg. No bloqueante.

**Neutras:**

- Los consumers downstream (Athena queries, futuro Gold) deciden la
  política sobre filas ffilled (descartar, ponderar, ignorar). silver-fx
  no impone política, solo provee el flag.

---

## Resultados empíricos del deploy (2026-05-11)

Datos reales de la corrida de validación post-deploy:

| Métrica            | Valor                               |
|--------------------|-------------------------------------|
| Tiempo de ejecución| 6.04 s                              |
| Memoria asignada   | 512 MB                              |
| Filas totales      | 4,143                               |
| Filas ffilled      | 1,319 (31.84%)                      |
| Rango temporal     | 2015-01-02 → 2026-05-06             |
| Parquet escrito    | 42,459 bytes (~42 KB) con Snappy    |
| Cobertura          | 100% del rango sin huecos > 7 días  |

Validación del schema post-escritura (lectura local con pyarrow):