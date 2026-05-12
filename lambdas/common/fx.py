# lambdas/common/fx.py
"""FX rate lookup for USD→CLP conversion with calendar-aware semantics.

This module is shared between silver-binance (reads USDT prices, needs FX
to convert to CLP) and any future Lambda that needs the USD/CLP rate.

Key semantic decision (ADR-008 §Decisión 2):
    FX rates from mindicador.cl carry dates in Santiago calendar time.
    To map a UTC-indexed market event to its applicable FX rate, we must
    convert the UTC timestamp to America/Santiago and use THAT date for
    lookup, not the UTC date. The two diverge for ~3-4 hours every day
    (depending on DST) and would otherwise introduce systematic FX errors
    of ~0.5-1% during those windows.

Forward-fill: dates without FX (weekends, Chilean holidays) take the
previous available value. Empirical analysis (verify_fx_join.py §1)
shows max gap is 5 consecutive days in the 2017-2026 range, so a
7-day max_back default is safe with margin.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Union
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SANTIAGO = ZoneInfo("America/Santiago")
UTC = timezone.utc

# Sentinel for ffill bound. Set to 7 because empirical max gap in the
# 2015-2026 FX series is 5 days; 7 gives 2-day margin without masking
# data quality issues (e.g. an unannounced 10-day forex closure would
# raise rather than silently propagate stale data).
DEFAULT_MAX_BACK_DAYS = 7


def build_fx_dict(
    fx_json_bytes: Union[bytes, str],
    max_back_days: int = DEFAULT_MAX_BACK_DAYS,
) -> dict[str, float]:
    """Parse mindicador FX JSON and return a {date_iso: rate} dict with ffill.

    The output dict has an entry for EVERY day in [min_date, max_date]:
    original days carry their reported value, missing days carry the most
    recent prior value (forward-fill, up to max_back_days back).

    Args:
        fx_json_bytes: raw JSON contents (bytes or str) of the mindicador
            file. Expected shape: {"metadata": {...}, "data": [{"fecha":
            "YYYY-MM-DD", "valor": float}, ...]}.
        max_back_days: max consecutive missing days to ffill. If a gap
            exceeds this, raises ValueError (defensive against unanticipated
            forex closures or corrupted data).

    Returns:
        Dict mapping ISO date string -> CLP rate, fully filled.

    Raises:
        ValueError: if input shape is unexpected or ffill window exceeded.
    """
    if isinstance(fx_json_bytes, bytes):
        fx_json_bytes = fx_json_bytes.decode("utf-8")
    payload = json.loads(fx_json_bytes)

    if "data" not in payload:
        raise ValueError("FX JSON missing 'data' key")
    raw = payload["data"]
    if not raw:
        raise ValueError("FX JSON 'data' is empty")

    # Build {date: value} from source. Source is asc-ordered per metadata.
    src: dict[date, float] = {}
    for row in raw:
        d = date.fromisoformat(row["fecha"])
        src[d] = float(row["valor"])

    min_d, max_d = min(src), max(src)
    logger.info("FX source: %d records, range %s -> %s",
                len(src), min_d, max_d)

    # Forward-fill: walk the full range, carry last known value forward.
    out: dict[str, float] = {}
    last_value: float | None = None
    last_value_date: date | None = None

    d = min_d
    while d <= max_d:
        if d in src:
            last_value = src[d]
            last_value_date = d
            out[d.isoformat()] = last_value
        else:
            if last_value is None:
                # Shouldn't happen given min_d in src, but defensive.
                raise ValueError(f"No prior FX value to ffill from at {d}")
            gap = (d - last_value_date).days
            if gap > max_back_days:
                raise ValueError(
                    f"FX gap of {gap} days exceeds max_back_days="
                    f"{max_back_days} at {d} (last value from "
                    f"{last_value_date}={last_value}). Investigate data "
                    f"source before relaxing this bound."
                )
            out[d.isoformat()] = last_value
        d += timedelta(days=1)

    logger.info("FX dict built: %d days covered (incl. ffilled)", len(out))
    return out


def lookup_fx_for_utc_ms(
    ts_ms: int,
    fx_dict: dict[str, float],
) -> float:
    """Look up the FX rate applicable to a UTC-indexed market timestamp.

    Converts the UTC timestamp to America/Santiago calendar date and looks
    up that date in fx_dict. See module docstring for the timezone
    justification.

    Args:
        ts_ms: UTC timestamp in milliseconds (epoch).
        fx_dict: output of build_fx_dict (must be ffilled).

    Returns:
        The CLP rate to apply to a USD-denominated value at ts_ms.

    Raises:
        KeyError: if the Santiago date is outside the FX dict range.
            Callers should ensure FX coverage encompasses kline range
            before calling, or catch and handle (e.g. skip the kline).
    """
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    date_scl_iso = dt_utc.astimezone(SANTIAGO).date().isoformat()
    return fx_dict[date_scl_iso]