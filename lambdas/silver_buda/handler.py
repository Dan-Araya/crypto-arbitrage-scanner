"""Bronze -> Silver transformation for Buda BTC-CLP trades.

Reads all Buda trade JSONs from S3 Bronze, reconstructs OHLCV 1-minute candles,
forward-fills minutes with no activity, and writes Parquet partitioned by
year/month into the unified_candles Silver table.

Design notes:
- Single-Lambda processes all of Buda in one invocation. Volume is small
  (~1.6M trades total, max file size ~1MB) and forward-fill across month
  boundaries is trivial when the full series lives in memory.
- Output schema is unified with Binance (column `exchange` distinguishes
  rows). Both Lambdas write to the same partition path with different
  filenames (buda.parquet, binance.parquet) — no concurrency conflicts.
- is_interpolated=true marks minutes where no trades occurred and the
  candle is a flat carry-forward of the last known close.
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars for local testing)
# ---------------------------------------------------------------------------
BUCKET = os.environ.get("DATA_LAKE_BUCKET", "btc-arbitrage-data-lake-001")
BRONZE_PREFIX = os.environ.get("BRONZE_PREFIX", "bronze/backtest/buda/")
SILVER_PREFIX = os.environ.get(
    "SILVER_PREFIX", "silver/backtest/unified_candles/"
)
EXCHANGE_LABEL = "buda"
OUTPUT_FILENAME = "buda.parquet"

# Bronze trade row indices (validated empirically against real file):
#   [ts_ms_str, amount_str, price_str, direction_str, trade_id_int]
IDX_TS, IDX_AMOUNT, IDX_PRICE, IDX_DIRECTION, IDX_TRADE_ID = 0, 1, 2, 3, 4


# ---------------------------------------------------------------------------
# Bronze loading
# ---------------------------------------------------------------------------
def list_bronze_keys(s3, bucket: str, prefix: str) -> list[str]:
    """List all JSON keys under the Bronze prefix using paginator."""
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                keys.append(obj["Key"])
    keys.sort()  # chronological by filename (epoch_ms in name)
    return keys


def load_one_file(s3, bucket: str, key: str) -> list[list[Any]]:
    """Download and parse a single Buda trades JSON, return raw data array."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    payload = json.loads(obj["Body"].read())
    return payload.get("data", [])


def load_all_trades(s3, bucket: str, prefix: str) -> pd.DataFrame:
    """Load all Bronze files and return a single DataFrame of trades."""
    keys = list_bronze_keys(s3, bucket, prefix)
    logger.info("Found %d Bronze JSON files under %s", len(keys), prefix)

    all_rows: list[list[Any]] = []
    for i, key in enumerate(keys):
        rows = load_one_file(s3, bucket, key)
        all_rows.extend(rows)
        if (i + 1) % 50 == 0:
            logger.info("Loaded %d/%d files (%d trades so far)",
                        i + 1, len(keys), len(all_rows))

    logger.info("Total trades loaded: %d", len(all_rows))

    df = pd.DataFrame(
        all_rows,
        columns=["ts_ms", "amount", "price", "direction", "trade_id"],
    )
    # Bronze stores numerics as strings — coerce explicitly
    df["ts_ms"] = pd.to_numeric(df["ts_ms"], errors="raise").astype("int64")
    df["amount"] = pd.to_numeric(df["amount"], errors="raise").astype("float64")
    df["price"] = pd.to_numeric(df["price"], errors="raise").astype("float64")
    df["direction"] = df["direction"].astype("string")
    df = df.drop(columns=["trade_id"])  # not needed for Silver

    df["datetime"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()

    logger.info("Trade range: %s -> %s", df.index[0], df.index[-1])
    return df


# ---------------------------------------------------------------------------
# OHLCV reconstruction
# ---------------------------------------------------------------------------
def trades_to_ohlcv(df_trades: pd.DataFrame) -> pd.DataFrame:
    """Resample trades to 1-minute OHLCV with continuous grid + forward fill.

    Returns a DataFrame indexed by minute timestamp (UTC), with the unified
    Silver schema. Minutes with no trades carry forward the last close,
    volumes set to 0, is_interpolated=true.
    """
    # Real-activity OHLC (NaN for empty minutes)
    ohlc = df_trades["price"].resample("1min").ohlc()

    # Aggregations on real trades
    volume = df_trades["amount"].resample("1min").sum()
    trade_count = df_trades["amount"].resample("1min").count()

    # Buy / sell split — single groupby, then unstack
    buy_mask = df_trades["direction"] == "buy"
    buy_volume = df_trades.loc[buy_mask, "amount"].resample("1min").sum()

    # Build base candles frame on the OHLC index (minutes with >=1 trade)
    candles = ohlc.copy()
    candles["volume_btc"] = volume
    candles["trade_count"] = trade_count.astype("int64")
    candles["buy_volume_btc"] = buy_volume

    # Continuous minute grid from first to last observed minute
    full_index = pd.date_range(
        start=candles.index.min(),
        end=candles.index.max(),
        freq="1min",
        tz="UTC",
    )
    candles = candles.reindex(full_index)

    # Flag interpolation BEFORE filling: a minute is interpolated iff it had
    # no real trades (its OHLC came back NaN from resample().ohlc())
    candles["is_interpolated"] = candles["close"].isna()

    # Forward-fill close, then propagate to a flat candle for empty minutes
    candles["close"] = candles["close"].ffill()
    interp_mask = candles["is_interpolated"]
    candles.loc[interp_mask, "open"] = candles.loc[interp_mask, "close"]
    candles.loc[interp_mask, "high"] = candles.loc[interp_mask, "close"]
    candles.loc[interp_mask, "low"] = candles.loc[interp_mask, "close"]

    # Volumes and counts are 0 on empty minutes
    candles["volume_btc"] = candles["volume_btc"].fillna(0.0)
    candles["buy_volume_btc"] = candles["buy_volume_btc"].fillna(0.0)
    candles["trade_count"] = candles["trade_count"].fillna(0).astype("int64")
    candles["sell_volume_btc"] = candles["volume_btc"] - candles["buy_volume_btc"]

    # If the very first minute was empty, close is still NaN (no prior value
    # to carry). Drop the leading NaN block to keep Silver clean.
    leading_nan = candles["close"].isna()
    if leading_nan.any():
        first_valid = (~leading_nan).idxmax()
        dropped = leading_nan.sum()
        logger.info("Dropping %d leading minutes with no prior close "
                    "(series effectively starts at %s)",
                    int(dropped), first_valid)
        candles = candles.loc[first_valid:]

    # Final unified schema
    out = pd.DataFrame({
        "timestamp": candles.index,
        "exchange": EXCHANGE_LABEL,
        "open_clp": candles["open"].astype("float64"),
        "high_clp": candles["high"].astype("float64"),
        "low_clp": candles["low"].astype("float64"),
        "close_clp": candles["close"].astype("float64"),
        "volume_btc": candles["volume_btc"].astype("float64"),
        "buy_volume_btc": candles["buy_volume_btc"].astype("float64"),
        "sell_volume_btc": candles["sell_volume_btc"].astype("float64"),
        "trade_count": candles["trade_count"].astype("int64"),
        "is_interpolated": candles["is_interpolated"].astype("bool"),
    }).reset_index(drop=True)

    return out


# ---------------------------------------------------------------------------
# Quality summary
# ---------------------------------------------------------------------------
def log_quality_summary(candles: pd.DataFrame) -> None:
    """Emit liquidity and interpolation stats to CloudWatch."""
    total = len(candles)
    interp = int(candles["is_interpolated"].sum())
    pct = 100.0 * interp / total if total else 0.0
    logger.info("Total minutes: %d | interpolated: %d (%.2f%%)",
                total, interp, pct)

    # Per-year breakdown — proxy for historical liquidity
    by_year = candles.assign(
        year=candles["timestamp"].dt.year
    ).groupby("year").agg(
        minutes=("timestamp", "count"),
        interp=("is_interpolated", "sum"),
    )
    by_year["pct_interpolated"] = (100.0 * by_year["interp"]
                                   / by_year["minutes"]).round(2)
    logger.info("Interpolation by year:\n%s", by_year.to_string())


# ---------------------------------------------------------------------------
# Partition + write
# ---------------------------------------------------------------------------
def write_partitioned(s3, candles: pd.DataFrame, bucket: str,
                      silver_prefix: str) -> int:
    """Split by (year, month) and write one Parquet per partition to S3."""
    # Explicit pyarrow schema — must match Binance Lambda's output exactly
    schema = pa.schema([
        ("timestamp", pa.timestamp("ns", tz="UTC")),
        ("exchange", pa.string()),
        ("open_clp", pa.float64()),
        ("high_clp", pa.float64()),
        ("low_clp", pa.float64()),
        ("close_clp", pa.float64()),
        ("volume_btc", pa.float64()),
        ("buy_volume_btc", pa.float64()),
        ("sell_volume_btc", pa.float64()),
        ("trade_count", pa.int64()),
        ("is_interpolated", pa.bool_()),
    ])

    candles = candles.copy()
    candles["_year"] = candles["timestamp"].dt.year
    candles["_month"] = candles["timestamp"].dt.month

    partitions_written = 0
    for (year, month), group in candles.groupby(["_year", "_month"], sort=True):
        group = group.drop(columns=["_year", "_month"])
        table = pa.Table.from_pandas(group, schema=schema, preserve_index=False)

        buf = io.BytesIO()
        pq.write_table(table, buf, compression="snappy")
        buf.seek(0)

        key = (f"{silver_prefix}year={year}/month={month:02d}/"
               f"{OUTPUT_FILENAME}")
        s3.put_object(
            Bucket=bucket, Key=key, Body=buf.getvalue(),
            ContentType="application/octet-stream",
        )
        logger.info("Wrote %d rows -> s3://%s/%s",
                    len(group), bucket, key)
        partitions_written += 1

    return partitions_written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(event: dict, context: Any) -> dict:
    started = datetime.now(timezone.utc)
    logger.info("Buda Bronze->Silver started at %s", started.isoformat())
    logger.info("Bucket=%s | Bronze=%s | Silver=%s",
                BUCKET, BRONZE_PREFIX, SILVER_PREFIX)

    s3 = boto3.client("s3")

    trades = load_all_trades(s3, BUCKET, BRONZE_PREFIX)
    candles = trades_to_ohlcv(trades)
    log_quality_summary(candles)

    # Defensive invariants check (warn-only, no filtering). Silver no juzga: solo
    # observa y reporta violaciones físicas. close<=0 y volume<0 son no-físicos
    # (corrupción), no señal. volume==0 es válido (minutos sin trades).
    bad_close = candles["close_clp"] <= 0
    bad_volume = candles["volume_btc"] < 0
    if bad_close.any() or bad_volume.any():
        n_close = int(bad_close.sum())
        n_volume = int(bad_volume.sum())
        violated = candles[bad_close | bad_volume]
        first_ts = violated["timestamp"].min()
        last_ts = violated["timestamp"].max()
        logger.warning(
            f"Silver invariants buda: close<=0={n_close}, volume<0={n_volume}, "
            f"first_ts={first_ts}, last_ts={last_ts}"
        )

    n_partitions = write_partitioned(s3, candles, BUCKET, SILVER_PREFIX)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary = {
        "status": "ok",
        "trades_loaded": int(len(trades)),
        "candles_written": int(len(candles)),
        "partitions_written": int(n_partitions),
        "interpolated_pct": round(
            100.0 * float(candles["is_interpolated"].mean()), 2
        ),
        "range_start": candles["timestamp"].iloc[0].isoformat(),
        "range_end": candles["timestamp"].iloc[-1].isoformat(),
        "elapsed_seconds": round(elapsed, 1),
    }
    logger.info("DONE %s", summary)
    return summary


if __name__ == "__main__":
    # Local invocation: AWS_PROFILE=... python handler.py
    print(json.dumps(main({}, None), indent=2, default=str))
