import awswrangler as wr
import pandas as pd
from datetime import datetime

def main(event, context):
    year = int(event.get('year', datetime.utcnow().year))
    month = int(event.get('month', datetime.utcnow().month))
    
    print(f"Iniciando descarga para: {year}-{month}")

    df = pd.DataFrame({
        "timestamp": [datetime(year, month, 1)],
        "price": [100000], 
        "exchange": ["binance"],
        "year": [year],
        "month": [month]
    })

    wr.s3.to_parquet(
        df=df,
        path="s3://btc-arbitrage-data-lake-001/bronze/backtest/binance/",
        dataset=True,
        mode="append",
        partition_cols=["year", "month"]
    )

    return {
        "status": "ok",
        "processed_period": f"{year}-{month}"
    }
