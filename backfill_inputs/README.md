# backfill_inputs/

Inputs de las Step Functions de ingesta. Cada subdirectorio tiene un propósito
distinto:

## `active_v2/`
Inputs canónicos del estado actual del pipeline. Estos son los archivos que
generarías hoy ejecutando los scripts de `scripts/`. Si necesitas reproducir
el backfill desde cero, parte de aquí.

- `binance_input.json` — período mensual completo para Binance (112 períodos).
- `backfill_buda_input_v2.json` — Buda completo con thresholds 400/1000
  (264 períodos, granularidad adaptativa).
- `backfill_buda_lote_4_2021_v2.json` — lote 4 extraído del input v2.
- `backfill_buda_lote_5_2022_2026_v2.json` — lote 5 extraído del input v2.

## `archived_v1/`
Inputs originales con thresholds 600/1500. Reemplazados por la versión v2
después del fallo del lote 3 (timeout en nov-2020), que reveló que el factor
de seguridad de 1.5x era insuficiente. Se preservan como evidencia de la
calibración previa, no para uso operativo. Ver `docs/adr/` para el contexto
completo.

## `buda_overrides/`
Inputs puntuales para resolver casos donde la heurística adaptativa de
`generate_buda_periods.py` subestimó el volumen real:

- `backfill_buda_lote_3_bis_2020_pendientes.json` — quincenas pendientes
  tras el fallo del lote 3.
- `backfill_buda_lote_3_ter_dic2020_q2_semanal.json` — override semanal
  para diciembre 2020 q2 (rally final del bull run; sample del día 15 cayó
  en zona calma).
- `backfill_buda_lote_4_2021_v2_pendientes.json` — pendientes tras el
  ajuste de throttle a 2.0s.
