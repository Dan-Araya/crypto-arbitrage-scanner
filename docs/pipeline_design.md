## Cobertura temporal de la capa Bronze y rango válido para Gold
 
**Validado con `scripts/verify_bronze_coverage.py` el 2026-05-05.**
 
| Fuente       | Inicio      | Fin         | Archivos                              |
|--------------|-------------|-------------|---------------------------------------|
| Binance      | 2017-08-17  | 2026-04-30  | 105 con datos + 7 vacíos pre-listing  |
| Buda         | 2015-01-01  | 2026-05-01  | 246                                   |
| FX (USD/CLP) | 2015-01-02  | 2026-05-06  | 1 (archivo único)                     |
 
Las tres fuentes presentan cero gaps y cero solapes internos.
 
**Rango válido para Gold:** `2017-08-17 → 2026-04-30` (3178 días).
Limitado por Binance en ambos extremos: BTCUSDT no existía antes del
listing en agosto 2017, y el backfill histórico se ejecutó hasta
abril 2026.
 
Los archivos pre-listing de Binance (enero–julio 2017, 7 archivos con
`records_count: 0`) son válidos por diseño — el handler los genera
para cubrir el rango solicitado aunque la API no devuelva datos. No
se consideran gaps ni errores.
