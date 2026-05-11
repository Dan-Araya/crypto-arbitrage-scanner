"""Bronze -> Silver transformation for Binance BTCUSDT 1m klines.

Reads all Binance kline JSONs from S3 Bronze, parses the 12-field array
shape into a typed DataFrame, joins with USD/CLP FX rates (calendar-aware
in Santiago timezone), converts OHLC to CLP, forward-fills minutes lost
to Binance maintenance windows, and writes Parquet partitioned by
year/month into the unified_candles Silver table.

Hito 2 stub: parsing only. FX join (Hito 4), interpolation (Hito 3),
and write (Hito 5) added incrementally.

Design references:
- ADR-007 (Silver Buda) for the unified schema contract
- ADR-008 (pending) for FX semantics: date_santiago(ts_utc) lookup
- api_discovery.md §1.3 for Binance 12-field kline shape
"""

from __future__ import annotations

import json
import io
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
# Configuration
# ---------------------------------------------------------------------------
BUCKET = os.environ.get("DATA_LAKE_BUCKET", "btc-arbitrage-data-lake-001")
BRONZE_PREFIX = os.environ.get("BRONZE_PREFIX", "bronze/backtest/binance/")
SILVER_PREFIX = os.environ.get(
    "SILVER_PREFIX", "silver/backtest/unified_candles/"
)
FX_BRONZE_KEY = os.environ.get(
    "FX_BRONZE_KEY", "bronze/backtest/fx/usdclp_dolar_mindicador.json"
)
EXCHANGE_LABEL = "binance"
OUTPUT_FILENAME = "binance.parquet"

# Binance kline 12-field shape (validated against real Bronze file):
#   [0] open_time_ms          int
#   [1] open                  str (USDT, parse to float64)
#   [2] high                  str
#   [3] low                   str
#   [4] close                 str
#   [5] volume_btc            str (base asset volume)
#   [6] close_time_ms         int (open_time + 59999)
#   [7] quote_volume_usdt     str (quote asset volume; not used in Silver)
#   [8] trade_count           int
#   [9] taker_buy_base_volume str → buy_volume_btc in unified schema
#   [10] taker_buy_quote_volume str (not used in Silver)
#   [11] ignore               str (always '0')
IDX_OPEN_TIME = 0
IDX_OPEN = 1
IDX_HIGH = 2
IDX_LOW = 3
IDX_CLOSE = 4
IDX_VOLUME_BTC = 5
IDX_TRADE_COUNT = 8
IDX_TAKER_BUY_BASE = 9


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
    """Download and parse a single Binance klines JSON, return raw data array."""
    obj = s3.get_object(Bucket=bucket, Key=key)
    payload = json.loads(obj["Body"].read())
    return payload.get("data", [])


def _rows_to_typed_df(rows: list[list[Any]]) -> pd.DataFrame:
    """Convert one Binance kline array-of-arrays to a typed DataFrame.

    Extracted to its own helper so load_all_klines can process files in
    a streaming fashion: parse + cast each file's rows to a compact
    typed frame immediately, then drop the raw list[list[str]] before
    moving to the next file. This keeps peak memory ~10x lower than
    accumulating all raw rows and building one giant DataFrame at the
    end (see Hito 6 incident: OOM at 3008 MB with the accumulator
    approach against 4.5M klines).
    """
    df = pd.DataFrame({
        "open_time_ms":   [r[IDX_OPEN_TIME] for r in rows],
        "open_usdt":      [r[IDX_OPEN] for r in rows],
        "high_usdt":      [r[IDX_HIGH] for r in rows],
        "low_usdt":       [r[IDX_LOW] for r in rows],
        "close_usdt":     [r[IDX_CLOSE] for r in rows],
        "volume_btc":     [r[IDX_VOLUME_BTC] for r in rows],
        "trade_count":    [r[IDX_TRADE_COUNT] for r in rows],
        "buy_volume_btc": [r[IDX_TAKER_BUY_BASE] for r in rows],
    })
    df["open_time_ms"] = pd.to_numeric(df["open_time_ms"], errors="raise").astype("int64")
    for col in ["open_usdt", "high_usdt", "low_usdt", "close_usdt",
                "volume_btc", "buy_volume_btc"]:
        df[col] = pd.to_numeric(df[col], errors="raise").astype("float64")
    df["trade_count"] = pd.to_numeric(df["trade_count"], errors="raise").astype("int64")
    df["timestamp"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True).astype("datetime64[ns, UTC]")
    df = df.drop(columns=["open_time_ms"])
    return df


def load_all_klines(s3, bucket: str, prefix: str) -> pd.DataFrame:
    """Load all Bronze files into a single typed DataFrame, streaming.

    Memory-efficient version: per file, build a small typed DataFrame
    and append it to a list. Concatenate at the end. Avoids holding the
    full list[list[str]] of all 4.5M klines in memory simultaneously,
    which OOMs at 3008 MB Lambda memory cap.

    Output columns identical to previous accumulator-based version
    (callers downstream don't need to change):
        timestamp        datetime64[ns, UTC]
        open_usdt        float64
        high_usdt        float64
        low_usdt         float64
        close_usdt       float64
        volume_btc       float64
        buy_volume_btc   float64
        trade_count      int64
    """
    keys = list_bronze_keys(s3, bucket, prefix)
    logger.info("Found %d Bronze JSON files under %s", len(keys), prefix)

    dfs: list[pd.DataFrame] = []
    total_klines = 0
    for i, key in enumerate(keys):
        rows = load_one_file(s3, bucket, key)
        df_one = _rows_to_typed_df(rows)
        dfs.append(df_one)
        total_klines += len(df_one)
        del rows  # liberar list[list[str]] antes de seguir
        if (i + 1) % 20 == 0:
            logger.info("Loaded %d/%d files (%d klines so far)",
                        i + 1, len(keys), total_klines)

    logger.info("Concatenating %d per-file DataFrames (%d klines total)",
                len(dfs), total_klines)
    df = pd.concat(dfs, ignore_index=True, copy=False)
    del dfs  # liberar las referencias intermedias

    df = df.sort_values("timestamp").reset_index(drop=True)
    logger.info("Kline range: %s -> %s",
                df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


# ---------------------------------------------------------------------------
# Reindex + intra-month interpolation
# ---------------------------------------------------------------------------
def reindex_and_interpolate(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing 1-minute slots in the kline series.

    Binance occasionally has maintenance windows where klines are absent.
    Silver schema requires a continuous 1-min grid: missing minutes are
    flagged is_interpolated=true, OHLC flattens to the previous close,
    and volumes/trade_count are zero.

    Input:  DataFrame from load_all_klines (USDT prices, no FX yet).
    Output: Same columns plus is_interpolated (bool) and sell_volume_btc
            (derived). timestamp is preserved as a column with continuous
            1-min spacing.
    """
    if df.empty:
        raise ValueError("Cannot reindex empty kline frame")

    df_idx = df.set_index("timestamp").sort_index()

    full_index = pd.date_range(
        start=df_idx.index.min(),
        end=df_idx.index.max(),
        freq="1min",
        tz="UTC",
    )
    n_before = len(df_idx)
    df_idx = df_idx.reindex(full_index)
    n_after = len(df_idx)
    n_inserted = n_after - n_before
    logger.info(
        "Reindex: %d klines -> %d (continuous grid), inserted %d (%.4f%%)",
        n_before, n_after, n_inserted,
        100.0 * n_inserted / n_after if n_after else 0.0,
    )

    # Flag BEFORE filling: NaN open means no real kline at that minute.
    df_idx["is_interpolated"] = df_idx["open_usdt"].isna()

    # Forward-fill close, then flatten OHL to close on interpolated rows.
    df_idx["close_usdt"] = df_idx["close_usdt"].ffill()
    mask = df_idx["is_interpolated"]
    for col in ["open_usdt", "high_usdt", "low_usdt"]:
        df_idx.loc[mask, col] = df_idx.loc[mask, "close_usdt"]

    # Volumes and counts are zero on interpolated minutes.
    df_idx["volume_btc"] = df_idx["volume_btc"].fillna(0.0)
    df_idx["buy_volume_btc"] = df_idx["buy_volume_btc"].fillna(0.0)
    df_idx["trade_count"] = df_idx["trade_count"].fillna(0).astype("int64")

    # Derive sell_volume after fills. Identity: total = buy + sell.
    df_idx["sell_volume_btc"] = df_idx["volume_btc"] - df_idx["buy_volume_btc"]

    # Drop leading NaN closes (if the very first minute was interpolated,
    # ffill has nothing to propagate).
    leading_nan = df_idx["close_usdt"].isna()
    if leading_nan.any():
        first_valid_idx = (~leading_nan).idxmax()
        dropped = int(leading_nan.sum())
        logger.info(
            "Dropping %d leading minutes with no prior close "
            "(series effectively starts at %s)",
            dropped, first_valid_idx,
        )
        df_idx = df_idx.loc[first_valid_idx:]

    out = df_idx.reset_index().rename(columns={"index": "timestamp"})

    total = len(out)
    interp = int(out["is_interpolated"].sum())
    pct = 100.0 * interp / total if total else 0.0
    logger.info(
        "Interpolation summary: %d total minutes, %d interpolated (%.4f%%)",
        total, interp, pct,
    )

    return out




# ---------------------------------------------------------------------------
# FX conversion (USDT -> CLP)
# ---------------------------------------------------------------------------
# Imported here to keep the section self-contained. The module path assumes
# `lambdas/` is in sys.path (true both locally via the test stub and in the
# deployment zip where common/ is bundled at the package root).
from common.fx import lookup_fx_for_utc_ms, SANTIAGO


def apply_fx_conversion(
    df: pd.DataFrame,
    fx_dict: dict[str, float],
) -> pd.DataFrame:
    """Convert OHLC from USDT to CLP using calendar-aware FX lookup.

    Per ADR-008 §Decisión 2: the FX rate for a UTC-indexed kline is
    determined by its Santiago calendar date, NOT its UTC date. This
    matters during the ~3-4h window each day when the UTC and Santiago
    dates disagree (UTC-3 or UTC-4 depending on DST).

    Implementation note: we vectorize the FX lookup by computing the
    Santiago date once per row and using pd.Series.map for the actual
    dict lookup. This avoids a Python-level loop over millions of rows.

    Input:  DataFrame from reindex_and_interpolate (USDT prices).
    Output: Same DataFrame with *_usdt columns replaced by *_clp columns
            (open/high/low/close). volume_btc and buy/sell remain in BTC.
    """
    if df.empty:
        raise ValueError("Cannot apply FX to empty kline frame")

    # Vectorized timezone conversion: pandas handles tz-aware -> tz-aware.
    # The .dt.date returns Python date objects; we format to ISO for the
    # dict lookup (matches the keys produced by common.fx.build_fx_dict).
    ts_scl = df["timestamp"].dt.tz_convert(SANTIAGO)
    date_keys = ts_scl.dt.strftime("%Y-%m-%d")

    # Vectorized lookup. Missing keys produce NaN, which we surface as
    # an error rather than silently propagating.
    fx_series = date_keys.map(fx_dict)
    n_missing = int(fx_series.isna().sum())
    if n_missing > 0:
        missing_dates = sorted(set(date_keys[fx_series.isna()].unique()))
        raise ValueError(
            f"FX coverage gap: {n_missing} klines have no FX rate. "
            f"Missing Santiago dates (up to 10 shown): "
            f"{missing_dates[:10]}. Check FX Bronze coverage vs kline range."
        )

    # Apply conversion. Multiplying OHLC by the same rate per row preserves
    # the OHLC invariants (low <= open,close <= high) because rate > 0.
    df = df.copy()
    df["open_clp"] = df["open_usdt"] * fx_series
    df["high_clp"] = df["high_usdt"] * fx_series
    df["low_clp"] = df["low_usdt"] * fx_series
    df["close_clp"] = df["close_usdt"] * fx_series

    # Drop USDT columns; not part of the unified Silver schema.
    df = df.drop(columns=["open_usdt", "high_usdt", "low_usdt", "close_usdt"])

    # Quick stats for CloudWatch
    logger.info(
        "FX conversion applied: %d klines, FX range [%.4f, %.4f], "
        "distinct daily rates used: %d",
        len(df), float(fx_series.min()), float(fx_series.max()),
        int(date_keys.nunique()),
    )

    return df
# ---------------------------------------------------------------------------
# Schema enforcement + Parquet write
# ---------------------------------------------------------------------------
# Canonical Silver schema (ADR-007). Must match silver_buda exactly.
# Column order matters: pyarrow Table.from_pandas respects the schema.
SILVER_SCHEMA = pa.schema([
    ("timestamp",        pa.timestamp("ns", tz="UTC")),
    ("exchange",         pa.string()),
    ("open_clp",         pa.float64()),
    ("high_clp",         pa.float64()),
    ("low_clp",          pa.float64()),
    ("close_clp",        pa.float64()),
    ("volume_btc",       pa.float64()),
    ("buy_volume_btc",   pa.float64()),
    ("sell_volume_btc",  pa.float64()),
    ("trade_count",      pa.int64()),
    ("is_interpolated",  pa.bool_()),
])


def build_final_schema_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Project the pipeline output to the canonical Silver schema.

    Single point where we enforce column order, types, and presence of
    the `exchange` label. Any deviation here breaks reads of the unified
    partition in Athena.
    """
    out = pd.DataFrame({
        "timestamp":       df["timestamp"],
        "exchange":        EXCHANGE_LABEL,
        "open_clp":        df["open_clp"].astype("float64"),
        "high_clp":        df["high_clp"].astype("float64"),
        "low_clp":         df["low_clp"].astype("float64"),
        "close_clp":       df["close_clp"].astype("float64"),
        "volume_btc":      df["volume_btc"].astype("float64"),
        "buy_volume_btc":  df["buy_volume_btc"].astype("float64"),
        "sell_volume_btc": df["sell_volume_btc"].astype("float64"),
        "trade_count":     df["trade_count"].astype("int64"),
        "is_interpolated": df["is_interpolated"].astype("bool"),
    })
    return out


def write_partitioned(
    s3, df: pd.DataFrame, bucket: str, silver_prefix: str,
) -> int:
    """Split by (year, month) and write one Parquet per partition to S3.

    The output filename is `binance.parquet`. silver_buda writes
    `buda.parquet` to the same partition path; both files coexist and
    Athena reads them together via the unified table.
    """
    df = df.copy()
    df["_year"] = df["timestamp"].dt.year
    df["_month"] = df["timestamp"].dt.month

    partitions_written = 0
    for (year, month), group in df.groupby(["_year", "_month"], sort=True):
        group = group.drop(columns=["_year", "_month"])
        table = pa.Table.from_pandas(
            group, schema=SILVER_SCHEMA, preserve_index=False,
        )

        buf = io.BytesIO()
        pq.write_table(table, buf, compression="snappy")
        buf.seek(0)

        key = (f"{silver_prefix}year={year}/month={month:02d}/"
               f"{OUTPUT_FILENAME}")
        s3.put_object(
            Bucket=bucket, Key=key, Body=buf.getvalue(),
            ContentType="application/octet-stream",
        )
        logger.info("Wrote %d rows -> s3://%s/%s", len(group), bucket, key)
        partitions_written += 1

    return partitions_written




# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def log_parse_summary(df: pd.DataFrame) -> None:
    """Sanity-check the parsed klines and emit summary to CloudWatch."""
    n = len(df)
    logger.info("=" * 60)
    logger.info("PARSE SUMMARY (Hito 2 — pre-reindex, pre-FX, pre-write)")
    logger.info("=" * 60)
    logger.info("Klines parsed:        %d", n)
    logger.info("Range:                %s → %s",
                df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    logger.info("Distinct dates (UTC): %d",
                df["timestamp"].dt.date.nunique())

    # Expected vs actual minute coverage (rough): max possible = (end-start)/1min + 1
    span_min = int((df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() // 60) + 1
    coverage = 100.0 * n / span_min
    logger.info("Span (minutes):       %d", span_min)
    logger.info("Coverage:             %.4f%% (gaps = %d minutes)",
                coverage, span_min - n)

    # Per-year sanity
    by_year = df.assign(year=df["timestamp"].dt.year).groupby("year").size()
    logger.info("Klines by year:\n%s", by_year.to_string())

    # Type assertions (cheap; fail fast if upstream changes shape)
    assert df["open_usdt"].dtype == "float64"
    assert df["trade_count"].dtype == "int64"
    assert df["timestamp"].dt.tz is not None, "timestamp must be tz-aware UTC"
    logger.info("Type assertions OK.")

# ---------------------------------------------------------------------------
# FX loading from Bronze
# ---------------------------------------------------------------------------
def load_fx_from_s3(s3, bucket: str, key: str) -> dict[str, float]:
    """Download the FX JSON from Bronze and build the ffilled lookup dict.

    Per ADR-008 §Decisión 1: silver-binance reads FX directly from Bronze
    (not from a Silver FX table) to avoid orchestration overhead. The
    forward-fill logic lives in common/fx.py and is shared with any
    future Lambda that needs USD→CLP conversion.
    """
    from common.fx import build_fx_dict

    logger.info("Loading FX from s3://%s/%s", bucket, key)
    obj = s3.get_object(Bucket=bucket, Key=key)
    fx_bytes = obj["Body"].read()
    fx_dict = build_fx_dict(fx_bytes)
    logger.info("FX dict ready: %d days covered", len(fx_dict))
    return fx_dict


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(event: dict, context: Any) -> dict:
    started = datetime.now(timezone.utc)
    logger.info("Binance Bronze->Silver started at %s", started.isoformat())
    logger.info(
        "Bucket=%s | Bronze=%s | Silver=%s | FX=%s",
        BUCKET, BRONZE_PREFIX, SILVER_PREFIX, FX_BRONZE_KEY,
    )

    s3 = boto3.client("s3")

    # Stage 1: load klines from Bronze
    df_raw = load_all_klines(s3, BUCKET, BRONZE_PREFIX)
    log_parse_summary(df_raw)

    # Stage 2: load FX dict (small, fits in memory)
    fx_dict = load_fx_from_s3(s3, BUCKET, FX_BRONZE_KEY)

    # Stage 3: reindex + interpolate missing minutes (Binance downtime)
    df_interp = reindex_and_interpolate(df_raw)

    # Stage 4: USDT -> CLP with calendar-aware FX lookup
    df_clp = apply_fx_conversion(df_interp, fx_dict)

    # Stage 5: project to canonical Silver schema
    df_final = build_final_schema_frame(df_clp)

    # Defensive invariants check (warn-only, no filtering). Silver no juzga: solo
    # observa y reporta violaciones físicas. close<=0 y volume<0 son no-físicos
    # (corrupción), no señal. volume==0 es válido (minutos sin trades).
    bad_close = df_final["close_clp"] <= 0
    bad_volume = df_final["volume_btc"] < 0
    if bad_close.any() or bad_volume.any():
        n_close = int(bad_close.sum())
        n_volume = int(bad_volume.sum())
        violated = df_final[bad_close | bad_volume]
        first_ts = violated["timestamp"].min()
        last_ts = violated["timestamp"].max()
        logger.warning(
            f"Silver invariants binance: close<=0={n_close}, volume<0={n_volume}, "
            f"first_ts={first_ts}, last_ts={last_ts}"
        )

    # Stage 6: write Parquet partitions to Silver
    n_partitions = write_partitioned(s3, df_final, BUCKET, SILVER_PREFIX)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary = {
        "status": "ok",
        "klines_loaded": int(len(df_raw)),
        "candles_written": int(len(df_final)),
        "partitions_written": int(n_partitions),
        "interpolated_pct": round(
            100.0 * float(df_final["is_interpolated"].mean()), 4
        ),
        "range_start": df_final["timestamp"].iloc[0].isoformat(),
        "range_end": df_final["timestamp"].iloc[-1].isoformat(),
        "elapsed_seconds": round(elapsed, 1),
    }
    logger.info("DONE %s", summary)
    return summary


if __name__ == "__main__":
    # Local invocation against real S3 (requires AWS_PROFILE):
    #   AWS_PROFILE=... python lambdas/silver_binance/handler.py
    print(json.dumps(main({}, None), indent=2, default=str))
