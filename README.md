# Crypto Arbitrage Scanner

Este es un proyecto de Data Engineering cuyo objetivo es reconstruir el histórico de brechas de arbitraje (spreads entre exchanges) de Bitcoin entre Binance (BTC-USDT) y Buda.com (BTC-CLP), normalizado por el tipo de cambio USD/CLP.

> **English summary:** End-to-end AWS data pipeline that ingests Bitcoin market data from Binance and Buda.com at minute-level granularity, combined with daily USD/CLP FX rates from the Central Bank of Chile. The pipeline normalizes data into a unified Silver layer in Parquet and exposes it via Athena with partition projection for cost-controlled analytical queries. Built with Lambda, Step Functions, S3, Glue, and Terraform. Demo query identifies sustained arbitrage episodes (Buda premium ≥ 1%) across 9 years of data. 
---

## Resultados
En los casi 9 años de datos analizados (agosto 2017 a abril 2026), se identificaron 34 horas de oportunidades de arbitraje significativas. Se define como "oportunidad significativa" aquella que presentó estas dos características simultáneamente:
- **Persistencia:** Una diferencia de precio sostenida por más de 5 minutos consecutivos para mitigar el efecto de anomalías temporales o baja liquidez puntual.
- **Rentabilidad:** Un sobreprecio de Buda respecto a Binance superior al 1%, definido como el umbral mínimo para compensar costos operativos, comisiones de intercambio y el tipo de cambio diario.

![Top 20 episodios sostenidos de prima Buda > Binance](assets/top_episodes_by_spread.png)

Dentro del periodo analizado, los momentos de mayor intensidad llegaron a diferencias promedio de **24%** con picos cercanos al **30%** (los 5 primeros episodios resaltados en la imagen, correspondientes al 7 de diciembre de 2017, el día en que BTC cruzó los USD 15.000 por primera vez). Mientras que el episodio de mayor duración alcanzó las 5 horas con 3 minutos (febrero 2021, destacado en rojo).


