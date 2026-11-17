"""Forecast GTA VI's launch-day Twitch viewers, hour by hour.

Method: for each comparable past launch, take its release-day hourly curve
and form the ratio of each hour's viewers to that launch's hour-1 viewers.
For a forecast hour h, the log-ratios across comparables are summarised by a
magnitude-weighted lognormal fit (mean mu_h, spread sd_h), and the forecast
quantiles are p1 * exp(mu_h + z_tau * sd_h), where p1 is this case's hour-1
print. Comparables are weighted by how close their hour-1 size is to this
case's hour-1 (Gaussian kernel on log size, bandwidth 1.0), with a x2 boost
for console-only launches: GTA VI launches on PS5 and Xbox Series only, and
console-only launches follow a different daily shape from PC launches (no
simultaneous global PC unlock). The log-spread is scaled by 0.9, which the
leave-one-out and practice validation showed is honest (about 90% coverage)
without paying for excess width.

Everything is deterministic: no randomness, no wall clock, no network.
Reads every *.in in $MAKEFUN_INPUT_DIR (else /input/), in name order, and
writes one <id>.out per case to $MAKEFUN_OUTPUT_DIR (else /output/).
"""

import json
import math
import os
from datetime import datetime
from pathlib import Path

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]

# Gaussian kernel bandwidth (log units) for hour-1 size similarity.
BW = 1.0
# Weight multiplier for console-only comparables (target is console-only).
CONSOLE_BOOST = 2.0
# The target game (GTA VI) is PlayStation 5 + Xbox Series X|S only.
TARGET_CONSOLE_ONLY = True
# Shrink the fitted log-spread: the raw cross-launch spread is a little
# wider than an honest 5-95 band needs.
SPREAD_SCALE = 0.9
# Fallback persistence spread (house baseline) if a forecast hour has no
# comparable data at all.
BASE_SIGMA = 0.15


def parse_time(text):
    """ISO 8601 UTC timestamp -> datetime (handles the trailing Z)."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def console_only(platforms):
    """True when a comparable's platform list has no PC release."""
    return "PC" not in (platforms or "").upper()


def load_curves(case):
    """One usable curve per comparable: (hour-1 viewers, [24 hourly
    viewers or None], console-only flag). A comparable counts only when it
    has a positive hour-1 print and at least 20 of the day's 24 hours; the
    GTA VI category's own pre-release history has no hour-1 and drops out."""
    curves = []
    for comp in case.get("comparables") or []:
        try:
            day_start = parse_time(comp["day_start"])
        except (KeyError, TypeError, ValueError):
            continue
        hours = [None] * 24
        for point in comp.get("hourly") or []:
            try:
                t = parse_time(point["from"])
                viewers = point["viewers"]
            except (KeyError, TypeError, ValueError):
                continue
            idx = int((t - day_start).total_seconds() // 3600)
            if 0 <= idx < 24 and viewers and viewers > 0:
                hours[idx] = viewers
        p1 = hours[0]
        if p1 is None or p1 <= 0:
            continue
        if sum(1 for v in hours if v is not None) < 20:
            continue
        curves.append((p1, hours, console_only(comp.get("platforms"))))
    return curves


def round2(x):
    """x rounded to 2 decimals, half up (as the house baseline rounds)."""
    scaled = x * 100.0
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100.0
    return int(value) if value.is_integer() else value


def baseline_quantiles(p1, h):
    """Persistence fallback: every hour stays at hour 1, spread widening
    like a random walk in log space (the house baseline's form)."""
    return [round2(p1 * math.exp(z * BASE_SIGMA * math.sqrt(h - 1))) for z in Z]


def hour_quantiles(curves, p1, h):
    """The five quantiles for hour h from the weighted lognormal fit of the
    comparables' log-ratios at that hour. If hour 1 is so extreme that the
    size kernel leaves almost no effective comparable behind, the kernel is
    widened once so the fit still has something to stand on."""
    idx = h - 1
    log_p1 = math.log(p1)

    def fit(bw):
        sw = sx = sxx = 0.0
        for comp_p1, hours, comp_console in curves:
            v = hours[idx]
            if v is None or v <= 0:
                continue
            d = (math.log(comp_p1) - log_p1) / bw
            w = math.exp(-0.5 * d * d)
            if TARGET_CONSOLE_ONLY and comp_console:
                w *= CONSOLE_BOOST
            lr = math.log(v / comp_p1)
            sw += w
            sx += w * lr
            sxx += w * lr * lr
        if sw <= 0.0:
            return 0.0, 0.0, 0.0
        mu = sx / sw
        var = sxx / sw - mu * mu
        sd = math.sqrt(var) if var > 0.0 else 0.0
        return sw, mu, sd

    sw, mu, sd = fit(BW)
    if sw < 3.0:
        sw2, mu2, sd2 = fit(2.0 * BW)
        if sw2 > sw:
            sw, mu, sd = sw2, mu2, sd2
    if sw <= 0.0:
        return baseline_quantiles(p1, h)
    sd *= SPREAD_SCALE
    qs = [p1 * math.exp(mu + z * sd) for z in Z]
    out = []
    prev = 0.0
    for q in qs:
        q = min(max(q, 1.0), 1e9)
        q = round2(q)
        q = min(max(q, 1.0), 1e9)
        if q < prev:
            q = prev
        out.append(q)
        prev = q
    return out


def forecast_case(case, name):
    first = next((k for k in case.get("known") or [] if k.get("hour") == 1), None)
    if first is None:
        raise ValueError(f"{name} has no hour-1 print")
    p1 = first["viewers"]
    if p1 is None or p1 < 1:
        p1 = 1
    curves = load_curves(case)
    hours = []
    for row in case["forecast"]:
        h = row["hour"]
        hours.append({"hour": h, "q": hour_quantiles(curves, p1, h)})
    return {"hours": hours}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case, path.name)
        text = json.dumps(forecast, separators=(",", ":")) + "\n"
        (OUTPUT_DIR / f"{path.stem}.out").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
