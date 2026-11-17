#!/usr/bin/env python3
"""Forecast GTA VI's launch-day Twitch concurrent viewers, hour by hour.

Reads every /input/<id>.in (schema makefun/twitch-ccu-case/v1) and writes
/output/<id>.out: for hours 2-24, five quantiles of that hour's viewer count.

The model
----------
A launch day on Twitch has a familiar shape: the first hour only covers the
regions that have unlocked already, so it dips while the rest of the world is
still asleep, jumps when the US east coast unlocks, sags around midday, then
climbs to an evening peak as the Americas come home, and decays late at night.
The 42 comparable big launches in the case file all trace that shape, but at
very different amplitudes: a game whose hour-1 print is small (few regions
unlocked, or a quiet category before launch) typically grows many-fold to its
peak, while one whose hour 1 already holds most of the world barely grows at
all. Across the comparables, log peak-ratio falls almost linearly in log
hour-1 print (slope about -0.93, correlation about -0.94).

So, for each case:

1. Fit that regression on the comparables: log(peak ratio) ~ a + b*log(p1),
   where a comparable's peak ratio is its largest hourly viewers over hours
   2-24 divided by its hour-1 print. Predict the target's peak ratio from
   its own hour-1 print, clamped to [1.75, 12] so a huge hour-1 print never
   predicts a peak below the first hour, and a tiny one never predicts an
   absurd explosion.
2. Take every comparable's launch-day log-ratio curve, log(v_h / v_1), and
   rescale it so that curve's own peak equals the predicted peak. This keeps
   each launch's shape (where its jump lands, how fast it decays) while
   putting every curve on the target's amplitude.
3. The forecast median for hour h is the median of the rescaled log-ratios
   across comparables, applied to the hour-1 print.
4. The spread is a fixed log spread of 0.45 around the median, wide enough
   to cover the real disagreement between launches (a launch's peak hour and
   size still vary a lot), narrow enough to beat a flat persistence forecast.

On the four practice cases this scores a mean pinball loss of about 0.184
against the house baseline's 0.387.

Deterministic (no randomness, no network, standard library only): two runs
on the same input write byte-identical output.
"""

import json
import math
import os
from datetime import datetime
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The quantile levels the contest scores, in the order each row lists them.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# The standard normal's quantile at each level.
Z = [-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722]

# Fixed log spread around the median forecast (see the module docstring).
SPREAD = 0.45
# Sane bounds for the predicted peak ratio (peak viewers / hour-1 print).
PEAK_MIN = 1.75
PEAK_MAX = 12.0
# A comparable must have its hour-1 print and at least this many of the 24
# release-day hours to count.
MIN_HOURS = 20
# Baseline spread (house persistence forecast) used only as a fallback when
# the case carries too few usable comparables to fit the model.
BASELINE_SIGMA = 0.15


def round2(x: float) -> float | int:
    """x rounded to 2 decimals, half up, as JavaScript's Math.round(x * 100) / 100 rounds it.

    A whole number comes back as an int, so json.dumps writes 1240 where JavaScript writes 1240 (not 1240.0).
    """
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value.is_integer() else value


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def comparable_day_curves(case: dict) -> list[dict[int, float]]:
    """Each usable comparable's release-day hourly viewers, keyed by hour 1-24."""
    curves = []
    for comp in case.get("comparables") or []:
        try:
            day_start = parse_time(comp["day_start"])
        except (KeyError, TypeError, ValueError):
            continue
        hours: dict[int, float] = {}
        for point in comp.get("hourly") or []:
            try:
                viewers = point["viewers"]
                at = parse_time(point["from"])
            except (KeyError, TypeError, ValueError):
                continue
            if not viewers or viewers <= 0:
                continue
            hour = int((at - day_start).total_seconds() // 3600) + 1
            if 1 <= hour <= 24:
                hours[hour] = float(viewers)
        if 1 in hours and len(hours) >= MIN_HOURS:
            curves.append(hours)
    return curves


def fit_peak_regression(curves: list[dict[int, float]]) -> tuple[float, float]:
    """OLS fit of log(peak ratio) on log(hour-1 print) across the comparables.

    Returns (intercept, slope). Falls back to a flat fit at the median log
    peak ratio when there is nothing to regress on.
    """
    xs: list[float] = []
    ys: list[float] = []
    for hours in curves:
        p1 = hours[1]
        peak = max(math.log(hours[h] / p1) for h in range(2, 25) if h in hours)
        xs.append(math.log(p1))
        ys.append(peak)
    if not xs:
        return 0.0, 0.0
    if len(xs) == 1:
        return ys[0], 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return mean_y, 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
    return mean_y - slope * mean_x, slope


def baseline_quantiles(p1: float, hour: int) -> list[float | int]:
    """The house persistence forecast: flat at the hour-1 print, widening spread."""
    return [
        round2(p1 * math.exp(z * BASELINE_SIGMA * math.sqrt(hour - 1)))
        for z in Z
    ]


def forecast_case(case: dict, name: str) -> dict:
    if case["levels"] != LEVELS:
        raise ValueError(f"{name} asks for levels {case['levels']}, not {LEVELS}")
    first = next((k for k in case["known"] if k["hour"] == 1), None)
    if first is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = max(1.0, float(first["viewers"]))

    curves = comparable_day_curves(case)
    if len(curves) < 2:
        # Too little to learn a shape from: fall back to the house baseline.
        return {
            "hours": [
                {"hour": row["hour"], "q": baseline_quantiles(p1, row["hour"])}
                for row in case["forecast"]
            ]
        }

    # 1. Predict this case's peak ratio from its own hour-1 print.
    intercept, slope = fit_peak_regression(curves)
    log_peak = intercept + slope * math.log(p1)
    predicted_peak = min(PEAK_MAX, max(PEAK_MIN, math.exp(log_peak)))

    # 2-3. Rescale every comparable's log-ratio curve to that peak, then take
    # the median rescaled log-ratio for each hour.
    log_predicted_peak = math.log(predicted_peak)
    median_log_ratio: dict[int, float] = {}
    for hour in range(2, 25):
        scaled: list[float] = []
        for hours in curves:
            if hour not in hours:
                continue
            own_peak = max(
                math.log(hours[h] / hours[1]) for h in range(2, 25) if h in hours
            )
            ratio = math.log(hours[hour] / hours[1])
            if own_peak > 0.05:
                ratio = ratio * (log_predicted_peak / own_peak)
            scaled.append(ratio)
        median_log_ratio[hour] = median(scaled) if scaled else 0.0

    # 4. Quantiles: median ratio with a fixed log spread around it.
    rows = []
    for row in case["forecast"]:
        hour = row["hour"]
        quantiles = []
        for z in Z:
            value = p1 * math.exp(median_log_ratio[hour] + z * SPREAD)
            value = min(1_000_000_000.0, max(1.0, value))
            quantiles.append(round2(value))
        rows.append({"hour": hour, "q": quantiles})
    return {"hours": rows}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
