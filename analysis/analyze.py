"""Turn the raw Garmin data in data/ into the tables the dashboard reads (written to dashboard/).

Outputs (all JSON arrays of row objects):
  status.json    one row: today's training signal, race countdown, marathon readiness numbers
  insights.json  ranked findings, each with the action it implies
  daily.json     one row per day: resting HR vs baseline, sleep, Body Battery, stress, training
  weekly.json    one row per ISO week: running km, long run, intensity mix, gym sessions
  runs.json      one row per run: pace, heart rate, zones, efficiency, heart-rate drift

Design rules:
- Every judgement is relative to the athlete's own baseline, not population norms, except where
  noted (sleep target, ramp-rate heuristics).
- A metric that lacks enough data says so instead of guessing.
- Thresholds live in CONFIG so they can be tuned once we see how they behave.
"""

from __future__ import annotations

import io
import json
import math
import statistics as st
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "dashboard"

CONFIG = {
    "timezone": "Europe/Madrid",
    "race_date": date(2027, 1, 10),
    "race_name": "Marathon",
    "race_km": 42.195,
    "taper_weeks": 3,
    # Finish-focused targets for the last build week before taper (conservative, for a first marathon).
    "target_peak_week_km": 40.0,
    "target_long_run_km": 28.0,
    "sleep_target_h": 7.5,
    "rhr_amber": 3,  # bpm above 30-day baseline
    "rhr_red": 6,
    "bb_amber": 50,  # morning Body Battery peak
    "bb_red": 30,
    "sleep_amber_h": 6.0,
    "sleep_red_h": 5.0,
    "ramp_caution_pct": 10,  # common coaching heuristic; evidence is weak, used as a soft guide
    "ramp_risk_pct": 30,  # >30% over two weeks: Nielsen et al. 2014 (distance-related injuries)
}

# Personal settings can be overridden by config.json at the repository root (race date, sleep target...).
_cfg_file = Path(__file__).resolve().parents[1] / "config.json"
if _cfg_file.exists():
    for _k, _v in json.loads(_cfg_file.read_text()).items():
        if _k.startswith("_"):
            continue
        CONFIG[_k] = date.fromisoformat(_v) if _k == "race_date" else _v

RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "indoor_running"}
STRENGTH_TYPES = {"strength_training"}
LEG_WORDS = ("leg", "legs", "pierna", "piernas", "lower", "squat", "sentadilla")


# ------------------------------------------------------------------ loading

def load_range(dataset: str, key: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted((DATA / "range" / dataset).glob("*.json")):
        for _, v in sorted(json.loads(f.read_text())["chunks"].items()):
            if isinstance(v, dict) and key:
                v = v.get(key, [])
            if isinstance(v, list):
                rows.extend(x for x in v if isinstance(x, dict))
    return rows


def by_date(rows: list[dict[str, Any]], field: str = "calendarDate") -> dict[str, dict[str, Any]]:
    out = {}
    for r in rows:
        d = r.get(field)
        if d:
            out[str(d)[:10]] = r
    return out


def load_zones() -> dict[str, float]:
    try:
        z = json.loads((DATA / "meta" / "hr_zones.json").read_text())
        z = next((x for x in z if x.get("sport") in ("RUNNING", "DEFAULT")), z[0])
        return {
            "lthr": z.get("lactateThresholdHeartRateUsed") or 185,
            "max": z.get("maxHeartRateUsed") or 204,
            "z2": z["zone2Floor"], "z3": z["zone3Floor"], "z4": z["zone4Floor"], "z5": z["zone5Floor"],
        }
    except Exception:
        return {"lthr": 185, "max": 204, "z2": 148, "z3": 165, "z4": 176, "z5": 185}


# ------------------------------------------------------------------ small helpers

def r1(x: float | None, n: int = 1) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


def pace_str(min_per_km: float | None) -> str | None:
    if not min_per_km or min_per_km <= 0 or min_per_km > 20:
        return None
    m = int(min_per_km)
    s = int(round((min_per_km - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def hms(minutes: float) -> str:
    h = int(minutes // 60)
    m = int(round(minutes - h * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}:{m:02d}"


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def ms_to_local_clock(ms: Any) -> str | None:
    # Garmin's "...TimestampLocal" values are local wall-clock time encoded as if UTC.
    if not ms:
        return None
    return datetime.utcfromtimestamp(ms / 1000).strftime("%H:%M")


def bedtime_minutes(ms: Any) -> float | None:
    """Minutes after 18:00, so 23:30 -> 330 and 01:30 -> 450 sort correctly."""
    if not ms:
        return None
    t = datetime.utcfromtimestamp(ms / 1000)
    mins = t.hour * 60 + t.minute
    return (mins - 18 * 60) % (24 * 60)


def minutes_to_clock(m: float | None) -> str | None:
    if m is None:
        return None
    total = int(round(m + 18 * 60)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


# ------------------------------------------------------------------ runs

def parse_fit_records(activity_id: str) -> list[dict[str, Any]] | None:
    path = DATA / "activities" / "fit" / f"{activity_id}.zip"
    if not path.exists():
        return None
    try:
        from fitparse import FitFile

        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".fit"))
            ff = FitFile(io.BytesIO(z.read(name)))
            out = []
            for m in ff.get_messages("record"):
                v = {d.name: d.value for d in m}
                spd = v.get("enhanced_speed") or v.get("speed")
                if v.get("timestamp") and v.get("heart_rate") and spd is not None:
                    out.append({"t": v["timestamp"], "hr": v["heart_rate"], "v": spd,
                                "alt": v.get("enhanced_altitude") or v.get("altitude")})
            return out
    except Exception:
        return None


def decoupling(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Pace:HR decoupling between the two halves, after a 10-minute warm-up.

    Positive % = heart rate drifted up relative to pace in the second half.
    Only reported for runs with >= 30 minutes of usable data after the warm-up, and flagged
    unreliable when pace was very uneven (intervals, stops) or the route was hilly.
    """
    if not records or len(records) < 60:
        return {"decoupling_pct": None, "decoupling_note": "no recording"}
    t0 = records[0]["t"]
    rows = [r for r in records if (r["t"] - t0).total_seconds() >= 600 and r["v"] > 1.5 and r["hr"] > 80]
    if len(rows) < 2:
        return {"decoupling_pct": None, "decoupling_note": "too short"}
    dur = (rows[-1]["t"] - rows[0]["t"]).total_seconds() / 60
    if dur < 30:
        return {"decoupling_pct": None, "decoupling_note": "too short"}
    mid = rows[0]["t"] + (rows[-1]["t"] - rows[0]["t"]) / 2
    a = [r for r in rows if r["t"] < mid]
    b = [r for r in rows if r["t"] >= mid]
    ef_a = st.mean(r["v"] for r in a) / st.mean(r["hr"] for r in a)
    ef_b = st.mean(r["v"] for r in b) / st.mean(r["hr"] for r in b)
    pct = (ef_a / ef_b - 1) * 100
    speeds = [r["v"] for r in rows]
    cv = st.pstdev(speeds) / st.mean(speeds)
    note = "ok"
    if cv > 0.18:
        note = "uneven pace"
    alts = [r["alt"] for r in rows if r["alt"] is not None]
    if alts and (max(alts) - min(alts)) > 60:
        note = "hilly" if note == "ok" else note + ", hilly"
    return {"decoupling_pct": r1(pct), "decoupling_note": note}


def build_runs(index: dict[str, Any], zones: dict[str, float]) -> list[dict[str, Any]]:
    runs = []
    for aid, a in index.items():
        if (a.get("activityType") or {}).get("typeKey") not in RUN_TYPES:
            continue
        km = (a.get("distance") or 0) / 1000
        mins = (a.get("movingDuration") or a.get("duration") or 0) / 60
        if km < 1 or mins < 5:
            continue
        hr = a.get("averageHR")
        z = [a.get(f"hrTimeInZone_{i}") or 0 for i in range(1, 6)]
        ztot = sum(z)
        pace = mins / km
        elev_per_km = (a.get("elevationGain") or 0) / km
        # Efficiency: metres per minute per heartbeat. Higher = more speed for the same effort.
        ef = (1000 / pace) / hr if hr else None
        if hr is None:
            intensity = "unknown"
        elif hr < zones["z3"]:
            intensity = "easy"
        elif hr < zones["z4"]:
            intensity = "moderate"
        else:
            intensity = "hard"
        row = {
            "activity_id": str(aid),
            "date": a["startTimeLocal"][:10],
            "start": a["startTimeLocal"][11:16],
            "name": a.get("activityName"),
            "km": r1(km, 2),
            "minutes": r1(mins),
            "pace_min_km": r1(pace, 3),
            "pace": pace_str(pace),
            "avg_hr": hr,
            "max_hr": a.get("maxHR"),
            "pct_lthr": r1(hr / zones["lthr"] * 100) if hr else None,
            "intensity": intensity,
            "easy_pct": r1((z[0] + z[1]) / ztot * 100) if ztot else None,
            "moderate_pct": r1(z[2] / ztot * 100) if ztot else None,
            "hard_pct": r1((z[3] + z[4]) / ztot * 100) if ztot else None,
            "elev_gain_m": r1(a.get("elevationGain") or 0, 0),
            "elev_per_km": r1(elev_per_km),
            "efficiency": r1(ef, 3) if ef else None,
            # Efficiency is only comparable on steady, fairly flat runs of 20+ minutes.
            "efficiency_comparable": bool(ef and mins >= 20 and elev_per_km <= 12),
            "training_load": r1(a.get("activityTrainingLoad"), 0),
            "temp_max_c": a.get("maxTemperature"),
        }
        row.update(decoupling(parse_fit_records(str(aid))) if mins >= 40 else
                   {"decoupling_pct": None, "decoupling_note": "under 40 min"})
        runs.append(row)
    runs.sort(key=lambda r: (r["date"], r["start"]))
    return runs


# ------------------------------------------------------------------ daily

def build_daily(index: dict[str, Any], runs: list[dict[str, Any]], today: date, days: int = 180) -> list[dict[str, Any]]:
    summaries = by_date(load_range("daily_summaries", "data"))
    sleep = by_date(load_range("sleep_summaries"))
    stress = by_date(load_range("stress"))

    acts_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in index.values():
        acts_by_day[a["startTimeLocal"][:10]].append(a)
    run_km = defaultdict(float)
    for r in runs:
        run_km[r["date"]] += r["km"]

    start = today - timedelta(days=days + 30)
    rows = []
    rhr_hist: list[tuple[date, float]] = []
    d = start
    while d <= today:
        k = d.isoformat()
        s = summaries.get(k, {})
        sl = sleep.get(k, {})
        rhr = (s.get("heartRate") or {}).get("restingValue")
        bb = s.get("bodyBattery") or {}
        sleep_s = sl.get("sleepTimeSeconds")
        score = ((sl.get("sleepScores") or {}).get("overall") or {}).get("value")
        stress_avg = ((stress.get(k) or {}).get("values") or {}).get("overallStressLevel")
        if stress_avg is None:
            stress_avg = (s.get("stress") or {}).get("avgLevel")
        if stress_avg is not None and stress_avg < 0:
            stress_avg = None

        window = [v for (dd, v) in rhr_hist if (d - dd).days <= 30]
        base = st.median(window) if len(window) >= 10 else None

        day_acts = acts_by_day.get(k, [])
        strength = [a for a in day_acts if (a.get("activityType") or {}).get("typeKey") in STRENGTH_TYPES]
        leg = any(any(w in (a.get("activityName") or "").lower() for w in LEG_WORDS) for a in strength)
        other = sorted({(a.get("activityType") or {}).get("typeKey", "") for a in day_acts}
                       - RUN_TYPES - STRENGTH_TYPES)

        row = {
            "date": k,
            "weekday": d.strftime("%a"),
            "rhr": rhr,
            "rhr_baseline": r1(base),
            "rhr_delta": r1(rhr - base) if (rhr and base) else None,
            "sleep_h": r1(sleep_s / 3600, 2) if sleep_s else None,
            "sleep_score": score,
            "bedtime": ms_to_local_clock(sl.get("sleepStartTimestampLocal")) if sleep_s else None,
            "bedtime_min": bedtime_minutes(sl.get("sleepStartTimestampLocal")) if sleep_s else None,
            "wake": ms_to_local_clock(sl.get("sleepEndTimestampLocal")) if sleep_s else None,
            "deep_h": r1((sl.get("deepSleepSeconds") or 0) / 3600, 2) if sleep_s else None,
            "bb_max": bb.get("maxValue"),
            "bb_min": bb.get("minValue"),
            "stress_avg": stress_avg,
            "run_km": r1(run_km.get(k, 0), 2),
            "strength_sessions": len(strength),
            "leg_day": leg,
            "other_activity": ", ".join(other) or None,
        }
        # Sleep stages (hours) for the stage donut.
        for stage, key in (("light_h", "lightSleepSeconds"), ("rem_h", "remSleepSeconds"), ("awake_h", "awakeSleepSeconds")):
            row[stage] = r1((sl.get(key) or 0) / 3600, 2) if sleep_s else None
        # Minutes by kind of training, and running minutes by heart-rate zone, for the pies.
        def kind(a: dict[str, Any]) -> str:
            t = (a.get("activityType") or {}).get("typeKey")
            return "run" if t in RUN_TYPES else "strength" if t in STRENGTH_TYPES else "other"
        for kname in ("run", "strength", "other"):
            row[f"{kname}_min"] = r1(sum((a.get("duration") or 0) / 60 for a in day_acts if kind(a) == kname), 0)
        for z in range(1, 6):
            row[f"run_z{z}_min"] = r1(sum((a.get(f"hrTimeInZone_{z}") or 0) / 60 for a in day_acts if kind(a) == "run"), 1)
        # Strain, 0-21: Garmin's training load for the day on a log scale (WHOOP-style, our own scale).
        load = sum(a.get("activityTrainingLoad") or 0 for a in day_acts)
        if not load:
            im = s.get("intensityMinutes") or {}
            load = ((im.get("moderate") or 0) + 2 * (im.get("vigorous") or 0)) * 0.7
        # Everyday movement adds a little strain, so rest days aren't zero (as on WHOOP).
        steps = ((s.get("movement") or {}).get("steps") or {}).get("value") or 0
        load += steps / 1000 * 0.5
        row["load"] = r1(load, 0)
        row["strain"] = r1(min(21.0, 21 * math.log1p(load) / math.log1p(500)), 1) if (s or day_acts) else None
        # Recovery, 0-100: resting HR vs your normal (45%), sleep vs target (30%), Body Battery peak (25%).
        parts = []
        if row["rhr_delta"] is not None:
            parts.append((0.45, max(0.0, min(100.0, (8 - row["rhr_delta"]) / 11 * 100))))
        if row["sleep_h"]:
            parts.append((0.30, min(100.0, row["sleep_h"] / CONFIG["sleep_target_h"] * 100)))
        if row["bb_max"] is not None:
            parts.append((0.25, float(row["bb_max"])))
        row["recovery"] = r1(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 0) if len(parts) >= 2 else None
        row["sleep_perf"] = r1(min(100.0, row["sleep_h"] / CONFIG["sleep_target_h"] * 100), 0) if row["sleep_h"] else None
        if rhr:
            rhr_hist.append((d, rhr))
        if d > today - timedelta(days=days):
            rows.append(row)
        d += timedelta(days=1)
    return rows


# ------------------------------------------------------------------ weekly

def build_weekly(runs: list[dict[str, Any]], daily: list[dict[str, Any]], today: date, weeks: int = 26) -> list[dict[str, Any]]:
    first = week_start(today) - timedelta(weeks=weeks - 1)
    agg: dict[date, dict[str, Any]] = {}
    for i in range(weeks):
        ws = first + timedelta(weeks=i)
        agg[ws] = {"km": 0.0, "runs": 0, "long": 0.0, "easy_s": 0.0, "mod_s": 0.0, "hard_s": 0.0,
                   "load": 0.0, "strength": 0, "sleep": [], "rhr": []}
    for r in runs:
        ws = week_start(date.fromisoformat(r["date"]))
        if ws not in agg:
            continue
        w = agg[ws]
        w["km"] += r["km"]
        w["runs"] += 1
        w["long"] = max(w["long"], r["km"])
        w["load"] += r["training_load"] or 0
        secs = r["minutes"] * 60
        if r["easy_pct"] is not None:
            w["easy_s"] += secs * r["easy_pct"] / 100
            w["mod_s"] += secs * r["moderate_pct"] / 100
            w["hard_s"] += secs * r["hard_pct"] / 100
    for d in daily:
        ws = week_start(date.fromisoformat(d["date"]))
        if ws not in agg:
            continue
        agg[ws]["strength"] += d["strength_sessions"]
        if d["sleep_h"]:
            agg[ws]["sleep"].append(d["sleep_h"])
        if d["rhr"]:
            agg[ws]["rhr"].append(d["rhr"])

    rows = []
    prev_km: list[float] = []
    for ws in sorted(agg):
        w = agg[ws]
        zt = w["easy_s"] + w["mod_s"] + w["hard_s"]
        two_wk_prev = sum(prev_km[-4:-2]) if len(prev_km) >= 4 else None
        two_wk_now = (prev_km[-1] if prev_km else 0) + w["km"]
        rows.append({
            "week": ws.isoformat(),
            "label": ws.strftime("%d %b"),
            "partial": today < ws + timedelta(days=6),
            "run_km": r1(w["km"]),
            "runs": w["runs"],
            "long_run_km": r1(w["long"]),
            "easy_pct": r1(w["easy_s"] / zt * 100, 0) if zt else None,
            "moderate_pct": r1(w["mod_s"] / zt * 100, 0) if zt else None,
            "hard_pct": r1(w["hard_s"] / zt * 100, 0) if zt else None,
            "run_load": r1(w["load"], 0),
            "strength_sessions": w["strength"],
            "sleep_avg_h": r1(st.mean(w["sleep"]), 2) if w["sleep"] else None,
            "nights_tracked": len(w["sleep"]),
            "rhr_avg": r1(st.mean(w["rhr"])) if w["rhr"] else None,
            "two_week_change_pct": r1((two_wk_now / two_wk_prev - 1) * 100, 0)
            if two_wk_prev and two_wk_prev >= 5 else None,
        })
        prev_km.append(w["km"])
    return rows


# ------------------------------------------------------------------ today + readiness

def today_signal(daily: list[dict[str, Any]], today: date) -> dict[str, Any]:
    c = CONFIG
    recent = [d for d in daily if date.fromisoformat(d["date"]) >= today - timedelta(days=1)]
    last = {}
    for key in ("rhr", "rhr_delta", "rhr_baseline", "sleep_h", "bb_max", "sleep_score", "bedtime"):
        for d in reversed(recent):
            if d.get(key) is not None:
                last[key] = d[key]
                last[key + "_date"] = d["date"]
                break
    three = [d["sleep_h"] for d in daily[-3:] if d["sleep_h"]]

    reasons, level = [], 0  # 0 green, 1 amber, 2 red
    delta = last.get("rhr_delta")
    if delta is not None:
        if delta >= c["rhr_red"]:
            level = max(level, 2)
            reasons.append(f"Resting HR {last['rhr']} is {delta:+.0f} bpm above your 30-day normal ({last['rhr_baseline']:.0f})")
        elif delta >= c["rhr_amber"]:
            level = max(level, 1)
            reasons.append(f"Resting HR {last['rhr']} is {delta:+.0f} bpm above your normal ({last['rhr_baseline']:.0f})")
        else:
            reasons.append(f"Resting HR {last['rhr']} is at your normal ({last['rhr_baseline']:.0f})")
    sh = last.get("sleep_h")
    if sh is not None:
        if sh < c["sleep_red_h"]:
            level = max(level, 2)
            reasons.append(f"Only {sh:.1f} h of sleep last night")
        elif sh < c["sleep_amber_h"]:
            level = max(level, 1)
            reasons.append(f"Short night: {sh:.1f} h of sleep")
        else:
            reasons.append(f"Slept {sh:.1f} h")
    else:
        reasons.append("No sleep data for last night yet (watch not synced, or not worn)")
    if len(three) >= 2 and st.mean(three) < c["sleep_amber_h"]:
        level = max(level, 1)
        reasons.append(f"Sleep has averaged {st.mean(three):.1f} h over the last {len(three)} nights")
    bb = last.get("bb_max")
    if bb is not None:
        if bb < c["bb_red"]:
            level = max(level, 2)
            reasons.append(f"Body Battery only reached {bb}")
        elif bb < c["bb_amber"]:
            level = max(level, 1)
            reasons.append(f"Body Battery peaked at {bb}")

    known = sum(x is not None for x in (delta, sh, bb))
    if known == 0:
        verdict, action = "No data", "Sync your watch (and wear it overnight) to get today's signal."
        level_name = "unknown"
    elif level == 2:
        verdict, action, level_name = "Recover", "Rest or 30-40 min very easy (talk-easy pace, HR under %d). Skip intervals and heavy legs." % load_zones()["z3"], "bad"
    elif level == 1:
        verdict, action, level_name = "Go easy", "Keep it easy: an easy run or upper-body gym is fine; move any hard session to tomorrow.", "warn"
    else:
        verdict, action, level_name = "Train", "Recovered. A good day for the long run or a quality session.", "ok"
    return {"signal": verdict, "signal_level": level_name, "signal_action": action,
            "signal_reasons": " · ".join(reasons), "signal_inputs_known": known,
            "rhr_today": last.get("rhr"), "rhr_baseline": last.get("rhr_baseline"),
            "rhr_delta": delta, "sleep_last_h": sh, "bb_today": bb,
            "signal_date": max((last.get(k + "_date", "") for k in ("rhr", "sleep_h", "bb_max")), default=None)}


def marathon_status(runs: list[dict[str, Any]], weekly: list[dict[str, Any]], today: date) -> dict[str, Any]:
    c = CONFIG
    now = date.today()  # countdown uses the real date even if the watch hasn't synced today
    days_left = (c["race_date"] - now).days
    weeks_left = days_left / 7
    taper_start = c["race_date"] - timedelta(weeks=c["taper_weeks"])
    build_weeks = max((taper_start - now).days / 7, 0)
    if days_left < 0:
        phase = "Race done"
    elif now >= taper_start:
        phase = "Taper"
    else:
        phase = "Build"

    full_weeks = [w for w in weekly if not w["partial"]]
    last4 = full_weeks[-4:]
    avg4 = st.mean(w["run_km"] for w in last4) if last4 else 0
    runs_per_wk = st.mean(w["runs"] for w in last4) if last4 else 0
    six_wk = [r for r in runs if date.fromisoformat(r["date"]) >= today - timedelta(weeks=6)]
    long6 = max((r["km"] for r in six_wk), default=0)
    long_all = max(runs, key=lambda r: r["km"]) if runs else None

    def needed_growth(now: float, target: float, weeks: float) -> float | None:
        if weeks < 1 or now <= 0:
            return None
        return ((target / now) ** (1 / weeks) - 1) * 100

    vol_growth = needed_growth(max(avg4, 1), c["target_peak_week_km"], build_weeks)
    long_runs_left = int(build_weeks)  # one long run per build week
    long_step = (c["target_long_run_km"] - long6) / long_runs_left if long_runs_left else None

    # Marathon estimate from the best recent effort >= 5 km in the last 8 weeks (Riegel).
    # 1.06 = Riegel's original exponent (trained runners); 1.15 = a common, more realistic
    # exponent for runners with low weekly volume. Shown as a range, not a prediction.
    recent = [r for r in runs if date.fromisoformat(r["date"]) >= today - timedelta(weeks=8) and r["km"] >= 5]
    proj = {}
    if recent:
        best = min(recent, key=lambda r: r["minutes"] * (c["race_km"] / r["km"]) ** 1.06)
        fast = best["minutes"] * (c["race_km"] / best["km"]) ** 1.06
        slow = best["minutes"] * (c["race_km"] / best["km"]) ** 1.15
        proj = {"proj_fast": hms(fast), "proj_slow": hms(slow),
                "proj_from": f"{best['km']:.1f} km in {hms(best['minutes'])} on {best['date']}",
                "proj_pace_fast": pace_str(fast / c["race_km"]), "proj_pace_slow": pace_str(slow / c["race_km"])}

    if avg4 <= 0:
        readiness = "No recent running"
    elif vol_growth is None:
        readiness = "Race window"
    elif vol_growth <= c["ramp_caution_pct"] and long6 >= 16:
        readiness = "On track"
    elif vol_growth <= 18:
        readiness = "Tight"
    else:
        readiness = "Behind"

    return {
        "race_name": c["race_name"], "race_date": c["race_date"].isoformat(), "days_to_race": days_left,
        "weeks_to_race": r1(weeks_left), "phase": phase, "taper_start": taper_start.isoformat(),
        "build_weeks_left": r1(build_weeks), "avg_km_4wk": r1(avg4), "runs_per_week_4wk": r1(runs_per_wk),
        "long_run_6wk_km": r1(long6), "longest_run_km": r1(long_all["km"]) if long_all else None,
        "longest_run_date": long_all["date"] if long_all else None,
        "target_peak_week_km": c["target_peak_week_km"], "target_long_run_km": c["target_long_run_km"],
        "required_weekly_growth_pct": r1(vol_growth, 0), "long_run_step_km": r1(long_step),
        "readiness": readiness, **proj,
    }


# ------------------------------------------------------------------ insights

def build_insights(status: dict[str, Any], runs: list[dict[str, Any]], weekly: list[dict[str, Any]],
                   daily: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    c = CONFIG
    out: list[dict[str, Any]] = []

    def add(priority: int, area: str, finding: str, action: str, level: str) -> None:
        out.append({"priority": priority, "area": area, "finding": finding, "action": action, "level": level})

    # 1. Marathon volume
    g = status.get("required_weekly_growth_pct")
    if status["readiness"] == "Behind" and g is not None:
        add(1, "Marathon",
            f"You're averaging {status['avg_km_4wk']:.0f} km/week ({status['runs_per_week_4wk']:.1f} runs) and your longest run in "
            f"6 weeks is {status['long_run_6wk_km']:.0f} km. Reaching ~{c['target_peak_week_km']:.0f} km/week and a "
            f"{c['target_long_run_km']:.0f} km long run before the taper means growing about {g:.0f}% every week for "
            f"{status['build_weeks_left']:.0f} weeks.",
            "That ramp is well above the usual ~10%/week guide. Decide now: (a) aim to finish with run-walk and accept a slower time, "
            "(b) switch to the half marathon, or (c) move to a spring marathon. Whatever you choose, add runs before adding km per run.",
            "bad")
    elif status["readiness"] == "Tight" and g is not None:
        add(1, "Marathon", f"Reaching race-ready volume needs about {g:.0f}% more running each week.",
            "Doable, but there's no slack: don't skip the long run, and keep every other run easy.", "warn")
    elif status["readiness"] == "On track":
        add(3, "Marathon", "Volume and long-run progression are on track for race day.",
            "Keep the long run growing 2-3 km at a time and protect it from heavy leg days.", "ok")

    # 2. Easy running
    last8 = [r for r in runs if date.fromisoformat(r["date"]) >= today - timedelta(weeks=8)]
    if len(last8) >= 3:
        easy_runs = sum(1 for r in last8 if r["intensity"] == "easy")
        easy_time = [w["easy_pct"] for w in weekly[-8:] if w["easy_pct"] is not None]
        med_hr = st.median(r["avg_hr"] for r in last8 if r["avg_hr"])
        z3 = load_zones()["z3"]
        if easy_runs / len(last8) < 0.5:
            add(2, "Intensity",
                f"Only {easy_runs} of your last {len(last8)} runs were easy. Your typical run averages {med_hr:.0f} bpm, "
                f"above the {z3} bpm where your moderate zone starts."
                + (f" Across those weeks about {st.mean(easy_time):.0f}% of running time was easy." if easy_time else ""),
                f"Marathon fitness is built mostly at easy effort. Run most sessions under {z3} bpm, even if it feels slow "
                "or needs walk breaks, and keep one run a week for faster work.",
                "bad" if easy_runs / len(last8) < 0.25 else "warn")
        else:
            add(4, "Intensity", f"{easy_runs} of your last {len(last8)} runs were easy.", "Good balance; keep it.", "ok")

    # 3. Ramp spikes
    spikes = [w for w in weekly[-4:] if (w["two_week_change_pct"] or 0) > c["ramp_risk_pct"]]
    if spikes:
        w = spikes[-1]
        add(2, "Injury risk",
            f"Running distance jumped {w['two_week_change_pct']:.0f}% over the two weeks to {w['label']}.",
            "Jumps above ~30% in two weeks have been linked to more distance-related injuries in novice runners. "
            "Hold distance flat next week before adding more.", "warn")

    # 4. Gym before key runs
    strength_days = {d["date"]: d for d in daily if d["strength_sessions"]}
    clashes = []
    for r in last8:
        key_run = r["km"] >= 10 or r["intensity"] == "hard"
        prev = (date.fromisoformat(r["date"]) - timedelta(days=1)).isoformat()
        same = r["date"]
        if key_run and (prev in strength_days or same in strength_days):
            clashes.append(r)
    any_leg_named = any(d["leg_day"] for d in daily)
    if clashes:
        leg_clash = [r for r in clashes if strength_days.get((date.fromisoformat(r["date"]) - timedelta(days=1)).isoformat(), {}).get("leg_day")
                     or strength_days.get(r["date"], {}).get("leg_day")]
        add(3, "Gym vs running",
            f"{len(clashes)} of your key runs in the last 8 weeks came the day after (or the same day as) a gym session"
            + (f", {len(leg_clash)} after a leg day." if any_leg_named else "."),
            "Keep leg day at least 48 h before the long run, ideally right after it. "
            + ("" if any_leg_named else "Rename leg sessions to 'Legs' on your watch or in Garmin Connect so this check can tell them apart."),
            "warn")
    elif not any_leg_named:
        add(5, "Gym vs running", "Your gym sessions are all called 'Strength', so leg days can't be told apart.",
            "Rename leg sessions to 'Legs' (and push/pull to 'Push'/'Pull') so leg days can be kept away from the long run.", "info")

    # 5. Sleep
    last14 = [d for d in daily[-14:]]
    tracked = [d for d in last14 if d["sleep_h"]]
    if len(tracked) >= 5:
        avg = st.mean(d["sleep_h"] for d in tracked)
        short = sum(1 for d in tracked if d["sleep_h"] < 7)
        beds = [d["bedtime_min"] for d in tracked if d["bedtime_min"] is not None]
        bed_med = minutes_to_clock(st.median(beds)) if beds else None
        bed_spread = (max(beds) - min(beds)) / 60 if len(beds) >= 3 else None
        lvl = "bad" if avg < 6.5 else "warn" if avg < c["sleep_target_h"] else "ok"
        add(2 if lvl != "ok" else 4, "Sleep",
            f"Over the last 2 weeks you slept {avg:.1f} h on average ({short} of {len(tracked)} tracked nights under 7 h), "
            f"usually falling asleep around {bed_med}"
            + (f", with bedtimes spread over {bed_spread:.0f} hours." if bed_spread and bed_spread >= 2 else "."),
            "Sleep is where the training turns into fitness. The easiest lever is a consistent bedtime: pick one "
            "you can hit 5 nights a week and protect the night before the long run." if lvl != "ok"
            else "Sleep is supporting your training. Keep the routine.", lvl)
    missing = sum(1 for d in last14 if not d["sleep_h"])
    if missing >= 4:
        add(4, "Data", f"No sleep recorded on {missing} of the last 14 nights.",
            "Wear the watch to bed. Without it the daily signal is guessing.", "info")

    # 6. Aerobic efficiency trend
    comp = [r for r in runs if r["efficiency_comparable"]]
    if len(comp) >= 6:
        old = comp[-12:-6] if len(comp) >= 12 else comp[:len(comp) // 2]
        new = comp[-6:] if len(comp) >= 12 else comp[len(comp) // 2:]
        a, b = st.mean(r["efficiency"] for r in old), st.mean(r["efficiency"] for r in new)
        chg = (b / a - 1) * 100
        trend = "flat" if abs(chg) < 2 else ("up %.0f%%" % chg if chg > 0 else "down %.0f%%" % -chg)
        add(4, "Fitness",
            f"Aerobic efficiency (speed per heartbeat on steady flat runs) is {trend} "
            f"across your last {len(new)} comparable runs versus the {len(old)} before.",
            "This is the best fitness signal available without VO2 max. It rises when easy running builds your engine."
            if chg >= 0 else "A drop usually means fatigue, heat, or too few runs. Check sleep and resting HR before pushing harder.",
            "ok" if chg >= 2 else "info" if chg > -2 else "warn")

    # 7. Heart-rate drift on longer runs
    drift = [r for r in runs if r["decoupling_pct"] is not None and r["decoupling_note"] == "ok"]
    if drift:
        recent_d = drift[-3:]
        avg_d = st.mean(r["decoupling_pct"] for r in recent_d)
        lst = ", ".join(f"{r['km']:.0f} km {r['decoupling_pct']:+.0f}%" for r in recent_d)
        if avg_d > 5:
            add(2, "Endurance",
                f"On your longer steady runs your heart rate drifts up a lot in the second half ({lst}). "
                "Under ~5% is a sign of a solid aerobic base.",
                "Your engine fades after about an hour, which is exactly what a marathon exposes. Long runs at truly easy "
                "effort (under %d bpm) are the fix; drift should fall as they get longer." % load_zones()["z3"], "warn")
        else:
            add(4, "Endurance", f"Heart-rate drift on recent long runs is low ({lst}).",
                "Your aerobic base is holding up over longer distances.", "ok")

    out.sort(key=lambda x: x["priority"])
    for i, x in enumerate(out):
        x["rank"] = i + 1
    return out


def _mmss(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _te_label(te: float | None) -> str | None:
    if te is None:
        return None
    return ("no effect" if te < 1 else "minor" if te < 2 else "maintaining" if te < 3 else
            "improving" if te < 4 else "highly improving" if te < 5 else "overreaching")


def build_last_run(runs: list[dict[str, Any]], zones: dict[str, float]) -> dict[str, Any] | None:
    """Lap-by-lap breakdown of the most recent run that has a recorded FIT file."""
    from fitparse import FitFile
    for run in reversed(runs):
        path = DATA / "activities" / "fit" / f"{run['activity_id']}.zip"
        if path.exists():
            break
    else:
        return None
    try:
        with zipfile.ZipFile(path) as z:
            ff = FitFile(io.BytesIO(z.read(next(n for n in z.namelist() if n.lower().endswith(".fit")))))
            laps_raw = [{d.name: d.value for d in m} for m in ff.get_messages("lap")]
            sess = next(({d.name: d.value for d in m} for m in ff.get_messages("session")), {})
    except Exception:
        return None

    def zone_of(hr: float | None) -> int | None:
        if hr is None:
            return None
        return 5 if hr >= zones["z5"] else 4 if hr >= zones["z4"] else 3 if hr >= zones["z3"] else 2 if hr >= zones["z2"] else 1

    laps = []
    for v in laps_raw:
        dist, t = v.get("total_distance") or 0, v.get("total_timer_time") or 0
        if dist < 30 or t < 15:
            continue
        cad = v.get("avg_running_cadence")
        laps.append({"trigger": v.get("lap_trigger"), "km": r1(dist / 1000, 2), "sec": t,
                     "time": _mmss(t), "pace_min_km": r1(t / 60 / (dist / 1000), 3), "pace": pace_str(t / 60 / (dist / 1000)),
                     "avg_hr": v.get("avg_heart_rate"), "max_hr": v.get("max_heart_rate"),
                     "zone": zone_of(v.get("avg_heart_rate")),
                     "cadence": round((cad + (v.get("avg_fractional_cadence") or 0)) * 2) if cad else None,
                     "ascent": v.get("total_ascent"), "descent": v.get("total_descent")})
    if not laps:
        return None

    # Detect a structured session: several manual laps that split into clearly fast and slow groups.
    manual = [l for l in laps if l["trigger"] == "manual"]
    structured = False
    if len(manual) >= 5:
        speeds = sorted(1 / l["pace_min_km"] for l in manual)
        lo, hi = speeds[len(speeds) // 4], speeds[-(len(speeds) // 4) - 1]
        if hi / lo > 1.25:
            structured, cut = True, (lo + hi) / 2
    if structured:
        work_idx = [i for i, l in enumerate(laps) if l["trigger"] == "manual" and 1 / l["pace_min_km"] > cut]
        first, last = work_idx[0], work_idx[-1]
        for i, l in enumerate(laps):
            l["type"] = ("warm-up" if i < first else
                         ("recovery" if i == last + 1 and l["trigger"] == "manual" and l["sec"] <= 150 else "cool-down") if i > last else
                         "rep" if i in work_idx else "recovery")
    else:
        for l in laps:
            l["type"] = "steady"
    rep_no = 0
    for i, l in enumerate(laps):
        l["n"] = i + 1
        if l["type"] == "rep":
            rep_no += 1
            l["label"] = f"Rep {rep_no}"
        else:
            l["label"] = l["type"].capitalize()

    total_s = sess.get("total_timer_time") or sum(l["sec"] for l in laps)
    total_km = (sess.get("total_distance") or sum(l["km"] * 1000 for l in laps)) / 1000
    out = {"activity_id": run["activity_id"], "name": run["name"], "date": run["date"], "start": run["start"],
           "km": r1(total_km, 2), "time": _mmss(total_s), "pace": pace_str(total_s / 60 / total_km),
           "avg_hr": sess.get("avg_heart_rate") or run["avg_hr"], "max_hr": sess.get("max_heart_rate") or run["max_hr"],
           "ascent": sess.get("total_ascent"), "descent": sess.get("total_descent"), "calories": sess.get("total_calories"),
           "cadence": round(((sess.get("avg_running_cadence") or 0) + (sess.get("avg_fractional_cadence") or 0)) * 2) or None,
           "te_aerobic": r1(sess.get("total_training_effect")), "te_anaerobic": r1(sess.get("total_anaerobic_training_effect")),
           "te_aerobic_label": _te_label(sess.get("total_training_effect")),
           "te_anaerobic_label": _te_label(sess.get("total_anaerobic_training_effect")),
           "easy_pct": run.get("easy_pct"), "moderate_pct": run.get("moderate_pct"), "hard_pct": run.get("hard_pct"),
           "decoupling_pct": run.get("decoupling_pct"), "training_load": run.get("training_load"),
           "structured": structured, "laps": laps}

    # Plain-language description (descriptive, no advice).
    txt = []
    if structured:
        reps = [l for l in laps if l["type"] == "rep"]
        recs = [l for l in laps if l["type"] == "recovery"]
        wu = [l for l in laps if l["type"] == "warm-up"]
        rep_s = st.median(l["sec"] for l in reps)
        rp = sum(l["sec"] for l in reps) / 60 / sum(l["km"] for l in reps)
        rhr = st.mean(l["avg_hr"] for l in reps if l["avg_hr"])
        out["kind"] = f"Intervals: {len(reps)} × {_mmss(rep_s)}"
        txt.append(f"Interval session: {out['km']:.1f} km in {out['time']}.")
        if wu:
            wkm = sum(l["km"] for l in wu); wsec = sum(l["sec"] for l in wu)
            txt.append(f"You warmed up for {wkm:.1f} km at {pace_str(wsec / 60 / wkm)}/km,")
        txt.append(f"then ran {len(reps)} reps of about {_mmss(rep_s)} averaging {pace_str(rp)}/km at {rhr:.0f} bpm.")
        a, b = reps[0], reps[-1]
        dp = (b["pace_min_km"] - a["pace_min_km"]) * 60
        pace_word = "held steady" if abs(dp) < 8 else ("got faster" if dp < 0 else "slowed")
        s_ = f"From the first rep to the last, pace {pace_word} ({a['pace']} → {b['pace']}/km)"
        if a.get("max_hr") and b.get("max_hr"):
            s_ += f" and peak heart rate went from {a['max_hr']} to {b['max_hr']} bpm"
            if b["max_hr"] - a["max_hr"] >= 5 and dp < 8:
                s_ += ", so the later reps took more out of you"
        txt.append(s_ + ".")
        if recs:
            txt.append(f"Recoveries averaged {st.mean(l['avg_hr'] for l in recs if l['avg_hr']):.0f} bpm.")
        out["rep_pace"], out["rep_hr"] = pace_str(rp), round(rhr)
    else:
        out["kind"] = "Steady run"
        txt.append(f"Steady run: {out['km']:.1f} km in {out['time']} at {out['pace']}/km, average HR {out['avg_hr']}.")
        full = [l for l in laps if l["km"] >= 0.9]
        if len(full) >= 2:
            fa, fb = min(full, key=lambda l: l["pace_min_km"]), max(full, key=lambda l: l["pace_min_km"])
            txt.append(f"Fastest km was lap {fa['n']} ({fa['pace']}), slowest lap {fb['n']} ({fb['pace']}).")
        if run.get("decoupling_pct") is not None:
            txt.append(f"Heart rate drifted {run['decoupling_pct']:+.0f}% from the first half to the second.")
    if out["te_aerobic"] is not None:
        txt.append(f"Garmin rates it {out['te_aerobic']} aerobic ({out['te_aerobic_label']}) and "
                   f"{out['te_anaerobic']} anaerobic ({out['te_anaerobic_label']}).")
    out["summary"] = " ".join(txt)
    for l in laps:
        l.pop("trigger", None)
    return out


def watch_last_upload(today: date, bb_time: str | None) -> dict[str, Any]:
    """Latest moment the watch's data reached Garmin, from today's wellness timestamps (local time)."""
    f = DATA / "range" / "daily_summaries" / f"{today:%Y-%m}.json"
    stamps: list[str] = []
    if f.exists():
        for c in json.loads(f.read_text()).get("chunks", {}).values():
            for r in (c.get("data", []) if isinstance(c, dict) else c):
                if r.get("calendarDate") != today.isoformat():
                    continue
                for k in ("pulseOx", "respiration"):
                    t = (r.get(k) or {}).get("latestTimestampLocal")
                    if t:
                        stamps.append(t[11:16])
                if (r.get("respiration") or {}).get("latestTimestampGmt") and not (r.get("respiration") or {}).get("latestTimestampLocal"):
                    from zoneinfo import ZoneInfo
                    g = datetime.fromisoformat(r["respiration"]["latestTimestampGmt"][:19]).replace(tzinfo=ZoneInfo("UTC"))
                    stamps.append(g.astimezone(ZoneInfo(CONFIG["timezone"])).strftime("%H:%M"))
    if bb_time:
        stamps.append(bb_time)
    return {"watch_sync_time": max(stamps) if stamps else None, "watch_sync_date": today.isoformat() if stamps else None}


def body_battery_now(today: date) -> dict[str, Any]:
    """Latest intraday Body Battery reading for today, plus today's high/low and charge/drain."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(CONFIG["timezone"])
    f = DATA / "range" / "body_battery" / f"{today:%Y-%m}.json"
    if not f.exists():
        return {}
    days = [d for chunk in json.loads(f.read_text()).get("chunks", {}).values() for d in chunk]
    day = next((d for d in days if d.get("date") == today.isoformat()), None)
    if not day:
        return {}
    vals = [(t, v) for t, v in (day.get("bodyBatteryValuesArray") or []) if v is not None]
    if not vals:
        return {}
    t_last, v_last = vals[-1]
    levels = [v for _, v in vals]
    hi = max(levels)
    return {"bb_now": v_last,
            "bb_now_time": datetime.fromtimestamp(t_last / 1000, tz).strftime("%H:%M"),
            "bb_high": hi, "bb_low": min(levels),
            "bb_charged": day.get("charged"),
            "bb_drained_since_high": hi - v_last}


def build_snapshot(status: dict[str, Any], daily: list[dict[str, Any]], runs: list[dict[str, Any]]) -> str:
    """Plain-language read of the day. Order: energy now, today's training, then recovery and sleep."""
    parts: list[str] = []
    td = daily[-1] if daily and daily[-1]["date"] == status["as_of"] else None

    # 1. Energy right now (Body Battery)
    bb = status.get("bb_now")
    if bb is not None:
        word = "high" if bb >= 60 else "moderate" if bb >= 30 else "low"
        s = f"Your energy is {word}: Body Battery is at {bb} (as of {status['bb_now_time']})"
        if status.get("bb_high") is not None and status["bb_high"] > bb:
            s += f", down from {status['bb_high']} at its high today"
        if status.get("bb_charged"):
            s += f"; sleep recharged it by {status['bb_charged']}"
        parts.append(s + ".")

    # 2. What you did today
    if td is not None:
        done = []
        if td.get("strength_min"):
            done.append(f"{td['strength_min']:.0f} min of gym")
        if td.get("run_min"):
            km = f"{td['run_km']:.1f} km " if td.get("run_km") else ""
            done.append(f"a {km}run ({td['run_min']:.0f} min)")
        if td.get("other_min"):
            done.append(f"{td['other_min']:.0f} min of other activity")
        strain = td.get("strain") or 0
        lvl = "a very hard day" if strain >= 18 else "a hard day" if strain >= 14 else "a moderate day" if strain >= 10 else "a light day"
        if done:
            parts.append(f"Today you did {' and '.join(done)}: strain {strain:.0f}/21, {lvl} on the body.")
        else:
            parts.append(f"No training logged today yet (strain {strain:.0f}/21 so far).")

    # 3. Recovery and sleep (morning readings)
    rec, rhr, base = status.get("recovery_latest"), status.get("rhr_today"), status.get("rhr_baseline")
    slh = status.get("sleep_h_latest")
    if rec is not None:
        word = "well recovered" if rec >= 67 else "partly recovered" if rec >= 34 else "run down"
        s = f"You woke up {word} (recovery {rec:.0f}%"
        if rhr is not None and base is not None:
            d = rhr - base
            s += ", resting HR " + (f"{rhr}, right at your normal" if abs(d) <= 2 else f"{rhr}, {abs(d):.0f} bpm {'below' if d < 0 else 'above'} normal")
        s += ")"
        if slh is not None:
            tgt = status["sleep_target_h"]
            q = "well above" if slh >= tgt + 1 else "on" if slh >= tgt - 0.5 else "short of"
            s += f" after {slh:.1f} h of sleep, {q} your {tgt:g} h target"
            if status.get("deep_h_latest") is not None and status.get("rem_h_latest") is not None:
                s += f" ({status['deep_h_latest']:.1f} h deep, {status['rem_h_latest']:.1f} h REM)"
        parts.append(s + ".")

    # 4. Week and marathon, briefly
    last7 = daily[-7:]
    n_run = sum(1 for d in last7 if d.get("run_min"))
    n_gym = sum(1 for d in last7 if d.get("strength_min"))
    short = [d for d in last7 if d.get("sleep_h") is not None and d["sleep_h"] < 6]
    wk = f"Last 7 days: {n_run} run{'s' if n_run != 1 else ''}, {n_gym} gym session{'s' if n_gym != 1 else ''}"
    if status.get("sleep_h_7d") is not None:
        wk += f", {status['sleep_h_7d']:.1f} h average sleep"
        if short:
            wk += f" ({len(short)} night{'s' if len(short) != 1 else ''} under 6 h)"
    parts.append(wk + ".")
    if status.get("avg_km_4wk") is not None:
        gap = {"Behind": "behind where the build needs to be", "Tight": "close, but tight on time",
               "On track": "on track"}.get(status.get("readiness"), "still being measured")
        parts.append(f"Marathon in {status['days_to_race']} days; at {status['avg_km_4wk']:.0f} km a week you're {gap}.")
    return " ".join(parts)


# ------------------------------------------------------------------ main

def main() -> None:
    index = json.loads((DATA / "activities" / "index.json").read_text())
    zones = load_zones()
    daily_all = load_range("daily_summaries", "data")
    last_day = max((d["calendarDate"] for d in daily_all), default=date.today().isoformat())
    today = min(date.fromisoformat(last_day), date.today())

    runs = build_runs(index, zones)
    daily = build_daily(index, runs, today)
    weekly = build_weekly(runs, daily, today)
    status = {"timezone": CONFIG["timezone"], "as_of": today.isoformat(), "generated_at": datetime.now().isoformat(timespec="minutes"),
              "lthr": zones["lthr"], "easy_hr_ceiling": zones["z3"], "max_hr": zones["max"],
              "sleep_target_h": CONFIG["sleep_target_h"], "ramp_guide_pct": CONFIG["ramp_caution_pct"]}
    status.update(today_signal(daily, today))
    # Latest values for the three rings (most recent day that has each one).
    for key in ("recovery", "strain", "sleep_perf", "sleep_h", "deep_h", "light_h", "rem_h", "awake_h", "bedtime", "wake"):
        latest = next((d for d in reversed(daily) if d.get(key) is not None), None)
        status[f"{key}_latest"] = latest[key] if latest else None
        status[f"{key}_latest_date"] = latest["date"] if latest else None
    last7 = [d for d in daily[-7:]]
    for key in ("recovery", "strain", "sleep_h"):
        vals = [d[key] for d in last7 if d.get(key) is not None]
        status[f"{key}_7d"] = r1(st.mean(vals), 1) if vals else None
    status.update(marathon_status(runs, weekly, today))
    status.update(body_battery_now(today))
    status.update(watch_last_upload(today, status.get("bb_now_time")))
    status["snapshot"] = build_snapshot(status, daily, runs)
    insights = build_insights(status, runs, weekly, daily, today)

    OUT.mkdir(exist_ok=True)
    (OUT / "last_run.json").write_text(json.dumps([build_last_run(runs, zones)], indent=1, ensure_ascii=False, default=str) + "\n")
    for name, rows in (("status", [status]), ("insights", insights), ("daily", daily),
                       ("weekly", weekly), ("runs", runs)):
        (OUT / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    print(f"as of {today}: {len(runs)} runs, {len(daily)} days, {len(weekly)} weeks, {len(insights)} insights")
    print(f"signal: {status['signal']} ({status['signal_reasons']})")
    print(f"marathon: {status['readiness']}, {status['days_to_race']} days left")


if __name__ == "__main__":
    main()
