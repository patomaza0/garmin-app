"""Pull your Garmin data through Garmin's official Personal API and store it as raw JSON.

Runs daily from GitHub Actions. The first run backfills history (BACKFILL_DAYS, default 365);
later runs only refresh the recent window, because Garmin keeps updating the last few days
(late watch syncs, sleep finalised in the morning, edited activities).

Layout written under data/:
  range/<dataset>/<YYYY-MM>.json   raw responses for date-range endpoints, one file per month
  activities/index.json            every activity summary, keyed by activityId
  activities/details/<id>.json     full activity detail (splits, laps, HR, etc.)
  activities/fit/<id>.zip          original recording for running activities (for HR-drift analysis)
  meta/profile.json, meta/hr_zones.json
  state.json                       backfill status, last run, errors from the last run

Nothing here is interpreted. Analysis happens in a separate step so the raw data stays intact.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"

# How many days back to re-fetch on every normal run.
REFRESH_DAYS = 10
# Only runs get their original file downloaded (needed for heart-rate drift / pacing analysis).
FIT_TYPES = ("running", "trail_running", "treadmill_running", "track_running", "indoor_running")
PAUSE = 0.4  # seconds between requests, to be polite to Garmin's API


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- file helpers

def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- calling the API safely

class Caller:
    """Wraps API calls with retries and records failures instead of crashing the whole run."""

    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.calls = 0

    def __call__(self, label: str, fn: Callable[[], Any]) -> Any:
        from garmin_personal_api import HttpError

        for attempt in range(4):
            self.calls += 1
            try:
                result = fn()
                time.sleep(PAUSE)
                return result
            except HttpError as exc:
                status = exc.status_code
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    wait = 5 * (attempt + 1) ** 2
                    log(f"  {label}: HTTP {status}, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                if status == 401:
                    raise  # bad or expired key: stop the whole run, it can't be fixed here
                self.errors.append({"call": label, "status": str(status), "detail": exc.response_text[:300]})
                log(f"  {label}: HTTP {status} (skipped)")
                return None
            except Exception as exc:  # network hiccup, bad JSON, etc.
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                self.errors.append({"call": label, "status": "error", "detail": repr(exc)[:300]})
                log(f"  {label}: {exc!r} (skipped)")
                return None
        return None


# ---------------------------------------------------------------- date-range datasets

RANGE_DATASETS: dict[str, Callable[[Any, date, date], Any]] = {
    "daily_summaries": lambda c, s, e: c.get_daily_summaries(s, e),
    "sleep_summaries": lambda c, s, e: c.get_daily_sleep_summaries(s, e),
    "hrv_summaries": lambda c, s, e: c.get_daily_hrv_summaries(s, e),
    "body_battery": lambda c, s, e: c.get_daily_body_battery(s, e),
    "stress": lambda c, s, e: c.get_daily_stress(s, e),
    "weight": lambda c, s, e: c.get_weight_measurements(s, e),
}


WEEKLY_ONLY = {"stress"}


def month_spans(start: date, end: date) -> list[tuple[date, date]]:
    spans = []
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        spans.append((max(cur, start), min(nxt - timedelta(days=1), end)))
        cur = nxt
    return spans


def week_spans(start: date, end: date) -> list[tuple[date, date]]:
    spans, cur = [], start
    while cur <= end:
        spans.append((cur, min(cur + timedelta(days=6), end)))
        cur += timedelta(days=7)
    return spans


def sync_range_datasets(client: Any, call: Caller, start: date, end: date) -> None:
    # Always re-fetch whole months so a refreshed month file never loses its earlier days.
    start = start.replace(day=1)
    for name, fn in RANGE_DATASETS.items():
        log(f"{name}: {start} → {end}")
        for m_start, m_end in month_spans(start, end):
            path = DATA / "range" / name / f"{m_start:%Y-%m}.json"
            stored = read_json(path, {"chunks": {}})
            chunks: dict[str, Any] = stored.get("chunks", {})

            # The stress endpoint only accepts ranges of up to a week; go straight to weekly calls.
            result = None if name in WEEKLY_ONLY else call(
                f"{name} {m_start}..{m_end}", lambda: fn(client, m_start, m_end))
            if result is not None:
                pieces = {f"{m_start}..{m_end}": result}
            else:
                # Some endpoints cap the range length; fall back to one week at a time.
                pieces = {}
                for w_start, w_end in week_spans(m_start, m_end):
                    r = call(f"{name} {w_start}..{w_end}", lambda: fn(client, w_start, w_end))
                    if r is not None:
                        pieces[f"{w_start}..{w_end}"] = r
            if not pieces:
                continue
            # Replace chunks that overlap the refreshed span; keep older ones.
            for key in list(chunks):
                k_start, k_end = (date.fromisoformat(x) for x in key.split(".."))
                if k_start <= m_end and k_end >= m_start:
                    del chunks[key]
            chunks.update(pieces)
            write_json(path, {"dataset": name, "chunks": chunks,
                              "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


# ---------------------------------------------------------------- activities

def activity_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [a for a in result if isinstance(a, dict)]
    if isinstance(result, dict):
        for key in ("activityList", "activities", "data", "items"):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def activity_type(a: dict[str, Any]) -> str:
    t = a.get("activityType")
    if isinstance(t, dict):
        return str(t.get("typeKey", ""))
    return str(t or "")


def sync_activities(client: Any, call: Caller, start: date, end: date) -> None:
    index_path = DATA / "activities" / "index.json"
    index: dict[str, Any] = read_json(index_path, {})
    log(f"activities: {start} → {end}")

    found: list[dict[str, Any]] = []
    page = 0
    while True:
        result = call(f"activities page {page}",
                      lambda: client.search_activities(start=page * 100, limit=100,
                                                       start_date=start, end_date=end))
        batch = activity_list(result)
        found.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    new_ids = []
    for a in found:
        aid = str(a.get("activityId", ""))
        if not aid:
            continue
        if aid not in index:
            new_ids.append(aid)
        index[aid] = a
    write_json(index_path, index)
    log(f"  {len(found)} activities in window, {len(new_ids)} new, {len(index)} total")

    recent_cutoff = (end - timedelta(days=REFRESH_DAYS)).isoformat()
    for aid, a in index.items():
        started = str(a.get("startTimeLocal", ""))[:10]
        detail_path = DATA / "activities" / "details" / f"{aid}.json"
        if not detail_path.exists() or started >= recent_cutoff:
            detail = call(f"activity {aid}", lambda: client.get_activity(aid))
            if detail is not None:
                write_json(detail_path, detail)

        fit_path = DATA / "activities" / "fit" / f"{aid}.zip"
        if activity_type(a) in FIT_TYPES and not fit_path.exists():
            blob = call(f"fit {aid}", lambda: client.download_activity(aid))
            if blob:
                fit_path.parent.mkdir(parents=True, exist_ok=True)
                fit_path.write_bytes(blob)


# ---------------------------------------------------------------- main

def main() -> int:
    api_key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not api_key:
        # Fail loudly (red X in GitHub) so a missing key is never mistaken for a working sync.
        log("ERROR: GARMIN_PERSONAL_API_KEY is not set. Add it under the repository's "
            "Settings → Secrets and variables → Actions, with exactly that name.")
        return 1

    from garmin_personal_api import GarminPersonalApiClient, HttpError, PersonalApiAuthenticator

    state = read_json(STATE_FILE, {})
    today = date.today()
    backfill_days = int(os.environ.get("BACKFILL_DAYS") or 365)
    force_backfill = os.environ.get("FORCE_BACKFILL", "").lower() == "true"

    if not state.get("backfill_done") or force_backfill:
        start = today - timedelta(days=backfill_days)
        log(f"Backfill run: {backfill_days} days, from {start}")
    else:
        start = today - timedelta(days=REFRESH_DAYS)
        log(f"Daily run: refreshing from {start}")

    call = Caller()
    try:
        with PersonalApiAuthenticator() as auth:
            token = auth.obtain_token(api_key)
        with GarminPersonalApiClient(token.access_token) as client:
            for name, fn in (("profile", client.get_user_profile_settings),
                             ("hr_zones", client.get_heart_rate_zones)):
                result = call(name, fn)
                if result is not None:
                    write_json(DATA / "meta" / f"{name}.json", result)
            sync_range_datasets(client, call, start, today)
            sync_activities(client, call, start, today)
    except HttpError as exc:
        if exc.status_code == 401:
            log("Garmin rejected the API key (HTTP 401). It may have expired or been deleted - "
                "create a new one in Garmin Connect and update the GitHub secret.")
            state["last_error"] = "401 - API key rejected"
            state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            write_json(STATE_FILE, state)
            return 1
        raise

    if call.errors and len(call.errors) >= call.calls * 0.5:
        log(f"Too many failures ({len(call.errors)}/{call.calls}); not marking backfill done.")
    else:
        if not state.get("backfill_done") or force_backfill:
            state["backfill_done"] = True
            state["backfill_from"] = start.isoformat()
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["last_run_errors"] = call.errors
    state.pop("last_error", None)
    write_json(STATE_FILE, state)
    log(f"Done: {call.calls} requests, {len(call.errors)} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
