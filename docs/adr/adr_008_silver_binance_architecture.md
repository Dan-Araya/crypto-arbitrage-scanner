# ADR-008: Arquitectura de la transformación Bronze→Silver para Binance

- **Status:** Accepted
- **Fecha:** 2026-05-10
- **Decisores:** Equipo del proyecto (autor único)
- **Issue/contexto:** Fase A.4 del pipeline — segunda transformación
  Bronze→Silver. Hereda el patrón establecido en ADR-007 (silver-buda) y
  agrega la complejidad propia de Binance: precios en USDT que requieren
  conversión a CLP usando el FX diario del Banco Central de Chile.

---

## Contexto

La capa Bronze para Binance estaba completa: 112 archivos JSON con klines
BTCUSDT 1m, ~4.5M registros cubriendo 2017-08-17 a 2026-04-30, validados
sin gaps ni solapes. Silver Buda ya estaba deployado (ADR-007), aportando
el schema canónico `unified_candles` que silver-binance debe respetar para
que ambos archivos coexistan en la misma partición.

Las decisiones del ADR-007 se heredan donde aplican (single-Lambda,
schema unificado, forward-fill, agregación pura sin outliers). Este ADR
documenta lo nuevo:

1. **Fuente del FX:** ¿silver-binance lee el FX desde Bronze directamente
   o depende de un Silver FX intermedio?
2. **Semántica del JOIN cross-timezone:** el FX viene fechado sin
   timezone explícita; ¿qué fecha aplicar a un kline indexado por UTC?
3. **Granularidad del cómputo:** ¿single-Lambda como buda, o el JOIN FX
   amerita Step Function?
4. **Reúso de código entre Silver Lambdas:** ¿cómo compartir la lógica
   de FX entre silver-binance y silver-fx?

Las tres primeras decisiones se cerraron antes de escribir código,
después de verificación empírica de la fuente FX. La cuarta surgió como
consecuencia natural al implementar.

---

## Decisión 1: silver-binance lee el FX directamente desde Bronze

**Decisión:** silver-binance carga el JSON FX desde
`bronze/backtest/fx/usdclp_dolar_mindicador.json` al inicio de su
ejecución, construye en memoria un diccionario `{date_iso: rate}` con
forward-fill aplicado, y usa ese diccionario para el JOIN con los klines.
No depende de la Lambda silver-fx (que escribirá su propio Silver más
adelante).

### Alternativas consideradas

**A1 — Lectura desde Silver FX intermedio.** silver-fx procesa primero
y deja el FX en `silver/backtest/fx/...` con el forward-fill ya
materializado. silver-binance lee ese Silver. Arquitectónicamente más
limpio: cada Lambda lee del nivel inmediatamente inferior.

**A2 — Step Function que orquesta FX → Binance secuencialmente.** silver-fx
y silver-binance se invocan en cadena por una state machine, garantizando
el orden. Resuelve la dependencia explícitamente sin necesidad de coordinar
manualmente.

**A3 — Bronze directo + módulo común (esta decisión).** silver-binance es
autocontenido; reusa la lógica de carga/ffill vía un módulo Python
compartido (`lambdas/common/fx.py`) que también consumirá silver-fx
cuando se implemente.

### Justificación

El JSON Bronze de FX es minúsculo: 107 KB, 2825 registros (~11 años de
serie diaria). Cargarlo y construir el dict con forward-fill cabe en
~50 ms y <1 MB de memoria. La lógica del forward-fill es trivial:

- 110 weekdays missing en 2275 hábiles en el rango 2017-2026 (4.8%,
  consistente con ~15-17 feriados oficiales por año).
- Distribución de gaps: 88% de 1-2 días (típico fin de semana o lunes
  feriado), 10% de 3 días, 13 ocurrencias de 4-5 días en 9 años
  (probablemente Semana Santa cuando jueves y viernes son ambos feriados).
- **Sin gaps mayores a 5 días.** Un bound de 7 días en el ffill cubre todo
  el rango histórico con margen.

A1 introduce una dependencia operacional sin beneficio funcional: el
forward-fill es la misma operación, hecha en la misma forma, sea que viva
en silver-fx o en silver-binance. A2 agrega orquestación (Step Function
+ IAM + estado) para resolver un problema que no existe — silver-fx se
puede ejecutar antes en cualquier momento sin coordinación.

A3 captura el beneficio de A1 (no duplicar código) sin su costo
arquitectónico (acoplamiento entre Lambdas): la lógica de carga y ffill
del FX vive en un módulo Python (`lambdas/common/fx.py`) que ambas
Lambdas importan. La separación es **a nivel de código fuente**, no a
nivel de pipeline.

### Consecuencias

- silver-binance es funcionalmente autónomo: si silver-fx no existe aún,
  silver-binance funciona igual.
- `lambdas/common/` es un nuevo directorio en el proyecto. No es una
  Lambda; es un package importado por las Lambdas Silver. Convención
  establecida: directorios bajo `lambdas/` que **no** tienen `handler.py`
  son módulos compartidos, no Lambdas.
- silver-fx (pendiente) escribirá un Silver propio, pero silver-binance
  **no leerá ese Silver**. Esto es deliberado: el Silver de FX existe
  para queries Athena cross-tabla en Gold, no como input de otra Lambda
  Silver. Es la primera vez que una capa Silver tiene **un consumidor
  Athena pero ningún consumidor Lambda**, y vale anotarlo.

---

## Decisión 2: el JOIN FX usa la fecha calendario Santiago, no UTC

**Decisión:** dado un kline Binance con timestamp UTC en milisegundos, el
FX aplicable se determina convirtiendo el timestamp a la timezone
`America/Santiago` y usando esa fecha calendario como clave de lookup en
el diccionario FX. Implementación canónica:

```python
ts_scl = df["timestamp"].dt.tz_convert("America/Santiago")
date_keys = ts_scl.dt.strftime("%Y-%m-%d")
fx_series = date_keys.map(fx_dict)
```

### Alternativas consideradas

**B1 — Hipótesis UTC.** Usar `df["timestamp"].dt.date` directamente. Es
el comportamiento "naive" si uno olvida que la fuente FX no está en UTC.

**B2 — Hipótesis Santiago (esta decisión).** Convertir UTC a Santiago
antes de extraer la fecha.

**B3 — Asumir que las dos coinciden.** Implícito en cualquier
implementación que no piense en timezones. Equivalente operativamente a B1
para este caso, pero peor estructuralmente porque oculta el problema.

### Verificación empírica

Antes de implementar se ejecutó `verify_fx_join_v2.py` contra datos
reales descargados de Bronze (FX completo + klines de diciembre 2023,
enero 2024, septiembre 2024). Tres pruebas:

**Prueba 1 — Cobertura FX (`§1`).** Confirmó que el ffill ≤7 días cubre
todo el rango operativo sin gaps anómalos. Cuantificó: 4143 días totales
cubiertos entre 2015-01-02 y 2026-05-06, incluidos los 1318 días
ffilleados (fines de semana + feriados).

**Prueba 2 — Caso control limpio (`§2`, caso 2).** Klines UTC del rango
`[2024-01-09T00:00:00Z, 2024-01-09T03:00:00Z)` (= `[2024-01-08T21:00,
2024-01-09T00:00) SCL`):

| Kline UTC | Kline SCL | `date_utc` | `date_scl` | FX hip. A (SCL) | FX hip. B (UTC) |
|---|---|---|---|---|---|
| 2024-01-09T00:00 | 2024-01-08T21:00 | 2024-01-09 | 2024-01-08 | 893.07 | 901.31 |
| 2024-01-09T02:30 | 2024-01-08T23:30 | 2024-01-09 | 2024-01-08 | 893.07 | 901.31 |
| 2024-01-09T03:00 | 2024-01-09T00:00 | 2024-01-09 | 2024-01-09 | 901.31 | 901.31 |

Las dos hipótesis divergen para 3 horas de cada día (UTC-3) o 4 horas
(UTC-4 durante CLT). La magnitud de la divergencia en este caso fue
**8.24 CLP en 893.07 = 0.92%** — significativa para el cálculo de spreads
de arbitraje.

**Prueba 3 — Transición DST (`§3`).** Confirmó que `zoneinfo` resuelve
correctamente el cambio de offset chileno del 8-sep-2024 (`-04:00` →
`-03:00` a las `04:00 UTC` exactas), incluyendo la regla chilena
restablecida en 2022 por DS 224 del Ministerio del Interior. Sin
ambigüedad ni duplicación de horas en `date_scl`.

### Justificación

**Argumento por la fuente del dato.** El Dólar Observado publicado por
el Banco Central de Chile (Capítulo III.A.1 del Compendio de Normas de
Cambios Internacionales) tiene fecha **calendario Santiago por
construcción**: representa el promedio ponderado de transacciones del
Mercado Cambiario Formal del día hábil anterior, publicado al inicio de
la jornada bancaria de Santiago del día indicado. Aplicar la fecha del
JSON a un timestamp UTC sin conversión es un type error semántico: cruza
dos sistemas de referencia temporales distintos.

**Argumento por la realidad operacional.** Buda.com (la otra fuente del
arbitraje) es un exchange chileno con liquidez en CLP. El trader que
considera el spread a las 22:00 hora de Santiago tiene mentalmente
disponible el FX del día calendario chileno, no el "FX del día UTC". La
hipótesis B introduce un salto del FX a las 21:00 hora local cada día
(cuando el reloj UTC cambia de día) que no corresponde a ningún evento
real del mercado.

**Argumento por la consistencia con feriados.** Cuando ambos `date_utc`
y `date_scl` caen en días sin FX publicado (fin de año, Semana Santa),
ambas hipótesis convergen porque el ffill colapsa al mismo último valor
hábil. La hipótesis correcta solo "se ve" en días hábiles consecutivos
donde el FX cambia. Caso 2024-01-08/09 es exactamente ese caso de
prueba.

### Consecuencias

- **Costo cero en runtime:** la conversión `dt.tz_convert` está
  vectorizada en pandas y opera sobre el DataFrame completo en una sola
  pasada. La fase de FX join sobre 4.5M klines toma <2s en el deploy
  real.
- **Manejo correcto de DST sin código manual:** `zoneinfo` consume la
  base IANA `tz`, que incluye el historial completo de las
  modificaciones erráticas del DST chileno (DST permanente intentado y
  revertido en 2015-2016, suspensión en Magallanes que mantiene UTC-3
  todo el año, restablecimiento de la regla actual en 2022). No hay
  offset fijo en el código.
- **Defensa empírica documentada:** la decisión queda respaldada por
  números concretos (la divergencia de 8.24 CLP), no solo por argumento
  teórico. Cualquier revisión futura puede re-ejecutar
  `verify_fx_join_v2.py` y validar el supuesto.

### Decisión asociada: forward-fill enforcement

El módulo `lambdas/common/fx.py` enforza un bound de `max_back_days=7` en
el forward-fill: si el gap entre la fecha consultada y la última fecha
con valor real excede ese bound, levanta `ValueError`. Esto es defensa
contra escenarios anómalos no anticipados:

- Cierre cambiario prolongado por crisis económica/política.
- Corrupción del JSON Bronze (fechas eliminadas).
- Cambio futuro en la política de publicación del BCCh.

El bound de 7 días deja 2 días de margen sobre el gap máximo histórico
observado (5 días). Cualquier valor que requiera ir más atrás amerita
investigación humana antes de propagar.

---

## Decisión 3: Single-Lambda con criterio de revisita explícito

**Decisión:** silver-binance es un único Lambda (3008 MB / 10 min
timeout) que procesa los 4.5M klines en una sola invocación, idéntico
patrón a silver-buda (ADR-007 Decisión 1).

### Alternativas consideradas

**C1 — Step Function por mes.** 106 invocaciones (una por mes en el
rango histórico), paralelizables. Resuelve cualquier problema de
memoria que pudiera surgir y agrega granularidad de retry.

**C2 — Map sobre años.** ~9 invocaciones, cada una procesa todos los
meses de su año. Menos paralelismo pero menos overhead.

**C3 — Single-Lambda (esta decisión).** Misma forma que silver-buda.

### Justificación

El JOIN FX es O(N) sobre el DataFrame de klines, con dict lookup
vectorizado por pandas. No introduce dependencias inter-particiones.
Estimación: el peak de memoria del JOIN es aditivo respecto al de buda
(que ya validamos a 3008 MB), no multiplicativo. Si buda cabe,
binance cabe con los ajustes de carga descritos en la sección de
incidente OOM más abajo.

C1 y C2 paralelizarían pero introducen complejidad operacional (state
machine, IAM extra, manejo del output) sin beneficio estructural. Step
Function tiene sentido cuando hay dependencias inter-tareas o cuando una
tarea individual no cabe en el Lambda; ninguna de las dos aplica.

### Resultado empírico

Después del refactor de carga (ver incidente OOM): **101.9 segundos
end-to-end, peak 2467 MB (82% del límite), 4,567,534 klines procesados,
4,576,166 velas escritas en 105 particiones Parquet**. Interpolación real
medida: 0.1886%, equivalente a ~8.6K minutos de downtime de Binance en
9 años (≈6 días totales distribuidos, consistente con la reputación de
estabilidad del exchange).

### Consecuencias

**Aceptadas:**

- No paraleliza. Si Binance crece 10× en volumen, se debe revisar.
- 82% de memoria utilizada deja headroom ajustado (18%). Proyección:
  el dataset crece ~44K klines/mes (~1% del total actual por mes), el
  peak proyectado a 12 meses adelante es ~87%. Sigue cabiendo en 3008 MB,
  pero el margen se reduce.
- Cuota de Lambda capada a 3008 MB en la cuenta. Subir requiere request
  a AWS Support, no justificable para portfolio.

**Criterios de revisita:**

- Si `Max Memory Used` excede 90% en alguna corrida: optimizar handler
  (liberar `df_raw` post-reindex con `del`, evaluar si `sort_values`
  necesita un workaround). El `pd.concat` ya está con `copy=False`.
- Si `Duration` excede 480s (80% del timeout actual): considerar
  partition by year/Step Function.
- Si se introduce ingesta incremental en Fase B: la transformación
  incremental requiere arquitectura distinta y este ADR debe
  superseder.

---

## Decisión 4: derivación de `sell_volume_btc` a partir de `volume - buy`

**Decisión:** silver-binance respeta el schema de 11 columnas heredado
del ADR-007. Para los tres campos que ADR-007 documentó como "preservados
a costo cero" (`buy_volume_btc`, `sell_volume_btc`, `trade_count`),
silver-binance los obtiene de la siguiente forma:

- `buy_volume_btc`: lectura directa del índice 9 del kline
  (`taker_buy_base_volume`).
- `trade_count`: lectura directa del índice 8.
- `sell_volume_btc`: **derivado** como `volume_btc - buy_volume_btc`. El
  API de Binance no provee este campo split.

### Justificación

La identidad `volume = buy + sell` es exacta por definición de los lados
del libro. La derivación se hace **después** del forward-fill de
volúmenes (donde los minutos interpolados tienen `volume_btc = 0` y
`buy_volume_btc = 0`), garantizando `sell_volume_btc = 0` también para
esos minutos. Validado en tests unitarios:

```python
assert (df["volume_btc"] - df["buy_volume_btc"] == df["sell_volume_btc"]).all()
```

La identidad se mantiene exactamente (con `==`, no aproximadamente), lo
que confirma que pandas no introduce error numérico en la resta de
float64 para magnitudes de volumen del orden actual (10^6 BTC max por
minuto).

### Consecuencias

- **Simetría semántica con Buda.** Buda silver computa `buy_volume_btc` a
  partir del campo `direction == "buy"` en los trades crudos. Ambos
  exchanges entregan campos con la misma definición operacional (taker
  side = buy), permitiendo comparaciones cross-exchange directas en Gold
  sin transformaciones adicionales.
- **No requiere validación cross-field.** La identidad
  `volume = buy + sell` se mantiene por construcción; no hay forma de
  que falle excepto error de implementación.

### Mapeo completo de índices Binance

Para referencia explícita en el código (`api_discovery.md §1.3` tiene la
documentación completa):

| Índice | Campo Binance | Mapeo Silver |
|---|---|---|
| 0 | `open_time_ms` | `timestamp` (via `pd.to_datetime`, unit=ms) |
| 1 | `open` | `open_usdt` → `open_clp` (× FX) |
| 2 | `high` | `high_usdt` → `high_clp` |
| 3 | `low` | `low_usdt` → `low_clp` |
| 4 | `close` | `close_usdt` → `close_clp` |
| 5 | `volume` | `volume_btc` |
| 6 | `close_time_ms` | omitido (= open_time + 59999) |
| 7 | `quote_volume_usdt` | omitido (no en schema) |
| 8 | `trade_count` | `trade_count` |
| 9 | `taker_buy_base_volume` | `buy_volume_btc` |
| 10 | `taker_buy_quote_volume` | omitido |
| 11 | `ignore` | omitido (campo constante "0") |

La checklist original del proyecto mencionaba "índices 0-8". El API real
tiene 12 índices (0-11). La diferencia es relevante porque el índice 9
(`taker_buy_base`) es **crítico** para el schema unificado: sin él,
silver-binance no podría producir `buy_volume_btc` y rompería la simetría
con buda. Esta corrección está reflejada en `api_discovery.md §1.3`.

---

## Decisión 5: empaquetado del módulo común vía `LAMBDAS_NEEDING_COMMON`

**Decisión:** el script `build_lambdas.sh` se extiende con una lista
explícita de Lambdas que requieren `common/`. Para cada Lambda en esa
lista, el zip resultante incluye una copia del directorio
`lambdas/common/` como package importable.

### Alternativas consideradas

**D1 — Descubrimiento automático.** Inspeccionar cada Lambda con
`grep -l "^from common"` y empaquetar `common/` si se detecta. No
requiere mantener listas.

**D2 — Lambda Layer.** Publicar `common/` como capa Lambda separada,
referenciada via `layers = [..., common_layer_arn]` en Terraform. Patrón
canónico de AWS para código compartido.

**D3 — Lista explícita (esta decisión).** Variable bash al inicio del
script declara qué Lambdas necesitan `common/`.

### Justificación

D1 es más mágico que explícito: la decisión de qué se empaqueta queda
oculta en una regex. Difícil de debuggear cuando algo no funciona.

D2 es el patrón canónico AWS pero overkill para este caso:

- `common/fx.py` tiene ~150 líneas; las layers se justifican típicamente
  para >1 MB de dependencias.
- Solo dos Lambdas lo van a usar (silver-binance, silver-fx pendiente).
- Lambda tiene un límite de 5 layers por función. Reservar un slot para
  código propio chico no es óptimo cuando ya se usa una layer externa
  grande (`AWSSDKPandas-Python311:31`).
- Versionar la layer agrega ciclo de release: cambiar `common/fx.py`
  requiere republicar la layer y bumpear el ARN en Terraform. Para un
  proyecto de portfolio con cambios frecuentes, fricción innecesaria.

D3 es explícito (cualquiera lee `LAMBDAS_NEEDING_COMMON=("silver_binance")`
y entiende qué pasa), trivial de implementar (~15 líneas extra), y
escala sin esfuerzo cuando silver-fx se agregue:

```bash
LAMBDAS_NEEDING_COMMON=("silver_binance" "silver_fx")
```

### Implementación

En `build_lambdas.sh`, una función `needs_common()` chequea pertenencia
a la lista. Si la Lambda califica, `build_one()` monta un directorio
temporal con el handler y `common/` adentro, y zipea desde ahí. Si no,
comportamiento original sin cambios.

El loop "empaqueta todas las Lambdas" se actualiza para saltar
`lambdas/common/` (no es una Lambda; se empaqueta dentro de otras).

### Consecuencias

- silver-binance.zip pesa ~12 KB con `common/` embebido (vs ~7 KB sin
  él). Trivial.
- Cambios en `common/fx.py` requieren rebuild de silver-binance.zip y
  silver-fx.zip; el script lo hace automáticamente si se invoca
  `./build_lambdas.sh` sin argumentos.
- Si en el futuro `common/` crece a >100 KB o aparecen 4+ Lambdas
  consumidoras, esta decisión se revisa: el threshold operacional para
  pasar a Lambda Layer es ese.

---

## Lecciones del primer deploy: incidente OOM + refactor a streaming

El primer deploy de silver-binance falló con `Runtime.OutOfMemory` a los
28 segundos, durante la fase de carga de Bronze (80/112 archivos, 3.17M
klines acumulados). El handler murió **antes** de cualquier
transformación: solo construyendo el DataFrame inicial.

### Análisis

La implementación inicial replicaba el patrón de silver-buda:

```python
all_rows: list[list[Any]] = []
for key in keys:
    rows = load_one_file(s3, bucket, key)
    all_rows.extend(rows)
df = pd.DataFrame(all_rows, ...)  # cast + tipo al final
```

Para Buda (~2.67M trades de 5 strings cada uno) el peak de memoria de la
representación intermedia `list[list[str]]` cabía en 3008 MB. Para
Binance (~4.5M klines de 12 strings cada uno) no: ~4.5M × 12 × ~50
bytes/string Python ≈ 2.7 GB solo en la lista cruda, antes de pandas. La
construcción del DataFrame con list comprehensions duplica
temporalmente esa memoria. OOM inevitable.

### Refactor: streaming-to-DataFrame

`load_all_klines` se reescribió para construir un DataFrame tipado por
archivo y concatenar al final:

```python
dfs = []
for key in keys:
    rows = load_one_file(s3, bucket, key)
    df_one = _rows_to_typed_df(rows)  # parse + cast inmediato
    dfs.append(df_one)
    del rows
df = pd.concat(dfs, ignore_index=True, copy=False)
del dfs
```

El cast a int64/float64 en el momento de la lectura reduce la huella
~10× respecto a la representación `list[list[str]]` (8 bytes vs ~50
bytes por valor). El `pd.concat(..., copy=False)` evita una copia
adicional. El `del` explícito de `rows` y `dfs` libera referencias
inmediatamente, lo cual a este volumen importa.

### Métricas del fix

Misma data, misma cuota:

- **Antes:** muerte a 28s, peak 3008 MB (= límite), nunca terminó.
- **Después:** 101.9s end-to-end, peak 2467 MB (82% del límite),
  refrescos cada 20 archivos para monitoreo.

### Patrón canónico que sienta este refactor

Para cualquier futuro Lambda Silver que procese Bronze tipo "array de
arrays" (que cubre todos los exchanges crypto típicos: Coinbase,
Kraken, Bitstamp, etc.), el patrón es:

1. **Función helper `_rows_to_typed_df(rows)`** que parsea + castea un
   batch de filas crudas a DataFrame tipado.
2. **`load_all_*` itera archivos**, llama al helper, acumula DataFrames
   tipados, libera el batch crudo con `del`.
3. **`pd.concat(..., copy=False)` al final**, libera la lista con `del`.

Este patrón debería estar en `common/` si aparece un tercer caso de uso.
Por ahora, dejarlo replicado entre silver-buda y silver-binance es
aceptable (DRY tempranamente no aporta sobre 2 casos).

### Lección operacional general

Los tests locales (5 scripts, todos pasaron antes del deploy) usaron
subsets pequeños del data real (3 meses Binance, ~133K klines). El OOM
fue invisible hasta ejercer el volumen completo. **El test local con
subset valida correctness funcional, no profile de memoria.** Para
detectar este tipo de problema antes del deploy, una opción habría sido
correr el handler completo localmente con todo el Bronze descargado y
monitorear `Max RSS` con `/usr/bin/time -v`. No se hizo porque
empaquetar 780 MB de Bronze local era costoso comparado con un re-deploy
de Lambda. Aceptable como pragma de Fase A; para producción seria, el
profiling local con el dataset completo debería formar parte del CI.

---

## Convenciones establecidas en este ADR

Se establecen convenciones adicionales que aplican a Silver Lambdas
futuras:

- **Módulos compartidos:** `lambdas/common/` para código que importan
  varias Lambdas Silver. Directorios bajo `lambdas/` sin `handler.py`
  son módulos, no Lambdas. El loop de empaquetado los excluye
  automáticamente.
- **Empaquetado de módulos comunes:** variable
  `LAMBDAS_NEEDING_COMMON` en `build_lambdas.sh` declara explícitamente
  qué Lambdas embeben `common/`. Mantener la lista actualizada cuando se
  agregue silver-fx.
- **Timezone canónica para FX:** `America/Santiago` declarada como
  constante en `common/fx.py` (`SANTIAGO = ZoneInfo("America/Santiago")`).
  Cualquier Lambda que toque FX debe importarla, no construir su propia.
- **Patrón streaming-to-DataFrame:** ver sección OOM. Aplicable a
  cualquier Bronze tipo `array of arrays` con volumen >2M registros.
- **Validación empírica antes de implementar:** decisiones que dependen
  de propiedades del dato (timezone, distribución de gaps, cobertura)
  deben respaldarse por un script en `tmp/fx_verify/scripts/` (o
  ubicación equivalente) con resultado numérico. Las decisiones de este
  ADR todas tienen su contraparte empírica.

---

## Referencias

- `data_quality.md` — comportamiento implementado y resultados empíricos
  (§ pendiente para silver-binance).
- `api_discovery.md` §1.3 — contrato Binance kline 12 índices.
- `docs/adr/adr_007_silver_buda_architecture.md` — patrón heredado.
- `lambdas/silver_binance/handler.py` — implementación.
- `lambdas/common/fx.py` — módulo compartido de carga/lookup FX.
- `infra/main.tf` — definición de la Lambda y recursos asociados.
- `build_lambdas.sh` — empaquetado con soporte de módulos comunes.
- Compendio de Normas de Cambios Internacionales del Banco Central de
  Chile, Capítulo III.A.1 — definición canónica del Dólar Observado.

---

## Trabajo derivado

Este ADR cierra silver-binance y deja explícitas las dependencias para:

1. **`silver-fx`** — usará `lambdas/common/fx.py` para la carga + ffill.
   Decisión de diseño pendiente: ¿partition por year/month como buda y
   binance, o archivo único sin particionar? Tentativa: archivo único,
   dado el volumen minúsculo. Amerita ADR-009.
2. **Validación Athena del schema unificado** — primera vez que ambos
   `buda.parquet` y `binance.parquet` coexisten en `unified_candles`.
   Query de validación queries de count por exchange y muestra cross-row
   son el cierre natural del Hito 6 conceptual.
3. **Validaciones defensivas tipo warning** en ambos handlers —
   pendientes desde ADR-007 (`close ≤ 0`, `volume < 0`). Triviales,
   ~10 líneas por handler.
4. **`data_quality.md` §6** — discontinuidad cambiaria FX, con datos
   empíricos de la verificación: 13 gaps de 4-5 días en 9 años,
   atribuibles a Semana Santa y feriados largos chilenos.

