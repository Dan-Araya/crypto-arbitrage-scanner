"""silver-fx Lambda: reads bronze FX JSON, writes a single Parquet to Silver.

Semantics (ADR-009):
- Reads s3://{BUCKET}/bronze/backtest/fx/usdclp_dolar_mindicador.json
- Builds a fully ffilled daily series via common.fx.build_fx_dict
- Writes a single non-partitioned Parquet to
  s3://{BUCKET}/silver/backtest/fx/usdclp.parquet
- Schema: date (date32, Santiago calendar), usdclp (float64), is_ffilled (bool)

Silver does not judge: every day in [min, max] is emitted, with is_ffilled
flagging carry-forward entries for downstream consumers to filter or weight
as they see fit.
"""

import io
import json
import logging
import os
import time
from datetime import date

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from common.fx import build_fx_dict

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

BUCKET = os.environ["BUCKET_NAME"]
BRONZE_KEY = "bronze/backtest/fx/usdclp_dolar_mindicador.json"
SILVER_KEY = "silver/backtest/fx/usdclp.parquet"

SILVER_SCHEMA = pa.schema([
    pa.field("date", pa.date32()),
    pa.field("usdclp", pa.float64()),
    pa.field("is_ffilled", pa.bool_()),
])


def main(event, context):
    t0 = time.monotonic()

    # Stage 1: read bronze FX JSON
    obj = s3.get_object(Bucket=BUCKET, Key=BRONZE_KEY)
    fx_bytes = obj["Body"].read()
    logger.info("Bronze FX read: %d bytes from s3://%s/%s",
                len(fx_bytes), BUCKET, BRONZE_KEY)

    # Stage 2: identify which dates are originals (pre-ffill).
    # We parse the raw payload once to know the source dates; build_fx_dict
    # will return the ffilled superset.
    raw_payload = json.loads(fx_bytes.decode("utf-8"))
    original_dates: set[str] = {row["fecha"] for row in raw_payload["data"]}
    logger.info("Original (non-ffilled) dates: %d", len(original_dates))

    # Stage 3: build ffilled daily dict (reuses validated common module)
    fx_dict = build_fx_dict(fx_bytes)

    # Stage 4: assemble columnar arrays sorted by date asc
    sorted_iso = sorted(fx_dict.keys())
    dates = [date.fromisoformat(d) for d in sorted_iso]
    usdclp = [fx_dict[d] for d in sorted_iso]
    is_ffilled = [d not in original_dates for d in sorted_iso]

    n_total = len(sorted_iso)
    n_ffilled = sum(is_ffilled)
    pct_ffilled = (n_ffilled / n_total * 100.0) if n_total else 0.0
    logger.info(
        "Silver FX built: total=%d, ffilled=%d (%.2f%%), range %s -> %s",
        n_total, n_ffilled, pct_ffilled, sorted_iso[0], sorted_iso[-1],
    )

    # Stage 5: build PyArrow Table with explicit schema (no inference)
    table = pa.Table.from_arrays(
        [
            pa.array(dates, type=pa.date32()),
            pa.array(usdclp, type=pa.float64()),
            pa.array(is_ffilled, type=pa.bool_()),
        ],
        schema=SILVER_SCHEMA,
    )

    # Stage 6: write Parquet to in-memory buffer and upload to S3
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)
    s3.put_object(Bucket=BUCKET, Key=SILVER_KEY, Body=buf.getvalue())

    elapsed = time.monotonic() - t0
    parquet_size = buf.getbuffer().nbytes
    logger.info("Silver FX written: s3://%s/%s (%d bytes) in %.2fs",
                BUCKET, SILVER_KEY, parquet_size, elapsed)

    return {
        "status": "ok",
        "rows_total": n_total,
        "rows_ffilled": n_ffilled,
        "pct_ffilled": round(pct_ffilled, 2),
        "range_start": sorted_iso[0],
        "range_end": sorted_iso[-1],
        "bytes_written": parquet_size,
        "elapsed_seconds": round(elapsed, 2),
    }