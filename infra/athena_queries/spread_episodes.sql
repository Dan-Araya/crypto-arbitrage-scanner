-- ============================================================================
-- Query de demostración: episodios sostenidos de prima Buda > Binance
-- ============================================================================
-- Hipótesis: existen episodios sostenidos (no solo picos esporádicos) donde
-- BTC en Buda.com cotiza con prima sobre Binance. Esto se conecta con la
-- literatura de "persistent arbitrage deviation" en mercados cripto
-- regionales (cf. Choi, Lehar & Stentoft 2022 sobre kimchi premium en Corea).
--
-- DEFINICIÓN DE EPISODIO:
--   Secuencia de minutos donde:
--   1. Buda tiene trade REAL (is_interpolated = false). Buda interpolada
--      no es prima sostenida, es ausencia de liquidez.
--   2. spread_pct = (close_buda - close_binance) / close_binance > 0.01 (1%)
--   3. El minuto siguiente del episodio está exactamente 60 segundos después
--      (gap rompe episodio: criterio estricto).
--   4. Duración total >= 5 minutos.
--
-- UMBRAL 1%: cubre fees round-trip conservadores (Binance 0.1% + Buda 0.4-0.8%
-- + movimiento de fondos). Bajo 1% no hay oportunidad ejecutable.
--
-- COSTO: ~600 MB escaneados. Requiere bytes_scanned_cutoff_per_query >= 1GB.
--
-- LIMITACIONES EXPLÍCITAS (Silver describe, Gold juzga — ADR principle):
--   - No descuenta fees reales por episodio.
--   - No considera liquidez disponible (un episodio de 1% con 0.1 BTC de
--     liquidez en el orderbook no es la misma oportunidad que con 10 BTC).
--   - Sesgo de selección: filtrar interpoladas selecciona minutos con
--     liquidez real, que pueden correlacionar con momentos de prima.
-- ============================================================================

WITH binance_close AS (
  SELECT
    timestamp,
    close_clp AS close_binance
  FROM unified_candles
  WHERE exchange = 'binance'
),
buda_close_real AS (
  -- Filtro de interpoladas: solo minutos con trade real en Buda
  SELECT
    timestamp,
    close_clp AS close_buda
  FROM unified_candles
  WHERE exchange = 'buda'
    AND is_interpolated = false
),
spread_minuto AS (
  SELECT
    timestamp,
    bd.close_buda,
    bn.close_binance,
    (bd.close_buda - bn.close_binance) / bn.close_binance AS spread_pct
  FROM buda_close_real bd
  INNER JOIN binance_close bn USING (timestamp)
  WHERE (bd.close_buda - bn.close_binance) / bn.close_binance > 0.01
),
-- Sessionization: marcar inicio de cada episodio
-- Un minuto es "inicio" si: no hay minuto anterior, o el anterior está
-- a más de 60 segundos de distancia.
marcado AS (
  SELECT
    timestamp,
    close_buda,
    close_binance,
    spread_pct,
    CASE
      WHEN LAG(timestamp) OVER (ORDER BY timestamp) IS NULL THEN 1
      WHEN date_diff('second',
                     LAG(timestamp) OVER (ORDER BY timestamp),
                     timestamp) > 60 THEN 1
      ELSE 0
    END AS es_inicio_episodio
  FROM spread_minuto
),
-- Asignar ID a cada episodio: suma acumulada de los flags de inicio
con_episodio_id AS (
  SELECT
    timestamp,
    spread_pct,
    SUM(es_inicio_episodio) OVER (ORDER BY timestamp
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
      AS episodio_id
  FROM marcado
),
-- Agregar por episodio
episodios AS (
  SELECT
    episodio_id,
    MIN(timestamp) AS inicio_utc,
    MAX(timestamp) AS fin_utc,
    COUNT(*) AS duracion_minutos,
    AVG(spread_pct) AS spread_promedio,
    MAX(spread_pct) AS spread_maximo
  FROM con_episodio_id
  GROUP BY episodio_id
  HAVING COUNT(*) >= 5  -- mínima duración 5 min
)
SELECT
  inicio_utc AT TIME ZONE 'America/Santiago' AS inicio_scl,
  fin_utc AT TIME ZONE 'America/Santiago' AS fin_scl,
  duracion_minutos,
  ROUND(duracion_minutos / 60.0, 2) AS duracion_horas,
  ROUND(spread_promedio * 100, 2) AS spread_promedio_pct,
  ROUND(spread_maximo * 100, 2) AS spread_maximo_pct,
  ROUND(duracion_minutos * spread_promedio * 100, 1) AS area_pct_minutos
FROM episodios
ORDER BY duracion_minutos * spread_promedio DESC
LIMIT 20;
