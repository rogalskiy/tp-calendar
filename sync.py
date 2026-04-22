"""TrainingPeaks → Google Calendar sync.

Flow per run:
  1. Log into TrainingPeaks via Playwright (needed to pass reCAPTCHA v3).
  2. Exchange the Production_tpAuth cookie for an OAuth access token.
  3. Fetch the athlete's planned workouts for the sync window.
  4. Diff against existing Google Calendar events (tagged with a private
     extended property) and create/update/delete to match.
  5. Exit non-zero on any failure so GitHub Actions emails us.

Env vars (set as GitHub Actions secrets / workflow env):
  TP_USERNAME, TP_PASSWORD                     — TrainingPeaks credentials
  GOOGLE_CREDENTIALS_JSON                      — JSON with client_id, client_secret, refresh_token
  GOOGLE_CALENDAR_ID   (default: "primary")    — target calendar
  TIMEZONE             (default: "Europe/Warsaw")
  SYNC_DAYS            (default: 14)           — how far ahead to sync
  SYNC_PAST_DAYS       (default: 1)            — how far back (to catch same-day edits)
  DRY_RUN              (default: "false")      — if "true", print actions without writing
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import sys
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Config

TP_USERNAME = os.environ["TP_USERNAME"]
TP_PASSWORD = os.environ["TP_PASSWORD"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Warsaw")
SYNC_DAYS = int(os.environ.get("SYNC_DAYS", "14"))
SYNC_PAST_DAYS = int(os.environ.get("SYNC_PAST_DAYS", "1"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

TP_API_BASE = "https://tpapi.trainingpeaks.com"
LOGIN_URL = "https://home.trainingpeaks.com/login"

# Marker we attach to every event we create, so we never touch user's own events.
EVENT_TAG_KEY = "tp_sync"
EVENT_TAG_VALUE = "1"
EVENT_WORKOUT_ID_KEY = "tp_workout_id"
EVENT_FINGERPRINT_KEY = "tp_fingerprint"

# Sport code → emoji (small nicety; coach titles are usually the main signal)
SPORT_EMOJI = {
    1: "🏊",  # Swim
    2: "🚴",  # Bike
    3: "🏃",  # Run
    4: "🏋️",  # Strength
    5: "🚴",  # MTB
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("tp-sync")


# ---------------------------------------------------------------------------
# TrainingPeaks


async def tp_get_auth_cookie() -> str:
    """Use Playwright to log in and return the Production_tpAuth cookie value."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="networkidle")

            # Wait for reCAPTCHA v3 to populate the hidden token field.
            await page.wait_for_function(
                "document.getElementById('captcha-token') "
                "&& document.getElementById('captcha-token').value.length > 0",
                timeout=20_000,
            )

            await page.fill("#Username", TP_USERNAME)
            await page.fill("#Password", TP_PASSWORD)
            await page.click("#btnSubmit")

            # After successful login TP redirects to app.trainingpeaks.com.
            # If credentials are bad, it stays on /login with an error message.
            try:
                await page.wait_for_url("**app.trainingpeaks.com/**", timeout=20_000)
            except Exception:
                body = await page.content()
                if "Invalid username or password" in body or "Login" in (
                    await page.title()
                ):
                    raise RuntimeError(
                        "TrainingPeaks login failed — check TP_USERNAME / TP_PASSWORD."
                    )
                raise

            cookies = await context.cookies()
        finally:
            await browser.close()

    for c in cookies:
        if c["name"] == "Production_tpAuth":
            return c["value"]
    raise RuntimeError("Login succeeded but Production_tpAuth cookie not found.")


async def tp_exchange_cookie_for_token(cookie: str) -> str:
    """Exchange the session cookie for a short-lived Bearer token."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{TP_API_BASE}/users/v3/token",
            headers={
                "Cookie": f"Production_tpAuth={cookie}",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
    token = data.get("token", {}).get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in /users/v3/token response: {data!r}")
    return token


async def tp_get_athlete_id(access_token: str) -> int:
    """Fetch the athleteId associated with this account."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{TP_API_BASE}/users/v3/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
    user = data.get("user", {})
    athletes = user.get("athletes") or []
    if athletes and athletes[0].get("athleteId"):
        return int(athletes[0]["athleteId"])
    # Fallbacks seen in the wild.
    for key in ("personId", "userId"):
        if user.get(key):
            return int(user[key])
    raise RuntimeError(f"Could not determine athleteId from /users/v3/user: {data!r}")


async def tp_fetch_workouts(
    access_token: str, athlete_id: int, start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    """Fetch planned & completed workouts in [start, end] inclusive."""
    url = (
        f"{TP_API_BASE}/fitness/v6/athletes/{athlete_id}"
        f"/workouts/{start.isoformat()}/{end.isoformat()}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
    # API returns either a list or an object with a list; be defensive.
    if isinstance(data, list):
        return data
    for key in ("workouts", "items", "data"):
        if isinstance(data.get(key), list):
            return data[key]
    raise RuntimeError(f"Unexpected workouts response shape: {type(data).__name__}")


# ---------------------------------------------------------------------------
# Workout → Google Calendar event


def _parse_workout_day(raw: str | None) -> dt.date | None:
    if not raw:
        return None
    # TP returns "YYYY-MM-DDT00:00:00" (naive, server time)
    return dt.date.fromisoformat(raw.split("T")[0])


def _workout_start_time(w: dict[str, Any]) -> dt.time:
    """Derive a start time. TP workouts don't carry a scheduled clock time;
    we place planned workouts at 07:00 local by default."""
    # If a start timestamp is present (completed workouts), honour it.
    for key in ("startTime", "startTimePlanned"):
        val = w.get(key)
        if val and isinstance(val, str) and "T" in val:
            try:
                return dt.time.fromisoformat(val.split("T")[1][:8])
            except ValueError:
                pass
    return dt.time(7, 0)


def _duration_minutes(w: dict[str, Any]) -> int:
    """Planned duration in minutes. TP stores totalTimePlanned as hours (float)."""
    val = w.get("totalTimePlanned") or w.get("totalTime") or 0
    try:
        minutes = int(round(float(val) * 60))
    except (TypeError, ValueError):
        minutes = 0
    return max(minutes, 30)  # minimum 30 min block for visibility


def _fingerprint(w: dict[str, Any]) -> str:
    """Stable hash of the fields we care about — lets us skip unchanged events."""
    import hashlib

    payload = json.dumps(
        {
            "title": w.get("title") or "",
            "desc": w.get("description") or "",
            "coach": w.get("coachComments") or "",
            "day": w.get("workoutDay"),
            "dur": w.get("totalTimePlanned"),
            "sport": w.get("workoutTypeFamilyId"),
            "tss": w.get("tssPlanned"),
            "km": w.get("distancePlanned"),
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def workout_to_event(w: dict[str, Any]) -> dict[str, Any] | None:
    """Transform a TP workout dict into a Google Calendar event body."""
    day = _parse_workout_day(w.get("workoutDay"))
    if not day:
        return None

    start_time = _workout_start_time(w)
    start_dt = dt.datetime.combine(day, start_time)
    duration = _duration_minutes(w)
    end_dt = start_dt + dt.timedelta(minutes=duration)

    sport_id = w.get("workoutTypeFamilyId")
    emoji = SPORT_EMOJI.get(sport_id, "🏅") if isinstance(sport_id, int) else "🏅"
    title = (w.get("title") or "Workout").strip()
    summary = f"{emoji} {title}"

    description_parts: list[str] = []
    if w.get("description"):
        description_parts.append(str(w["description"]).strip())
    if w.get("coachComments"):
        description_parts.append("Coach notes:\n" + str(w["coachComments"]).strip())
    stats = []
    if w.get("totalTimePlanned"):
        stats.append(f"Planned: {_duration_minutes(w)} min")
    if w.get("distancePlanned"):
        stats.append(f"Distance: {w['distancePlanned']/1000:.1f} km")
    if w.get("tssPlanned"):
        stats.append(f"TSS: {w['tssPlanned']}")
    if stats:
        description_parts.append(" · ".join(stats))
    description_parts.append("— synced from TrainingPeaks")
    description = "\n\n".join(description_parts)

    return {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "reminders": {"useDefault": True},
        "extendedProperties": {
            "private": {
                EVENT_TAG_KEY: EVENT_TAG_VALUE,
                EVENT_WORKOUT_ID_KEY: str(w.get("workoutId")),
                EVENT_FINGERPRINT_KEY: _fingerprint(w),
            }
        },
    }


# ---------------------------------------------------------------------------
# Google Calendar


def gcal_service():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials(
        token=None,
        refresh_token=info["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=["https://www.googleapis.com/auth/calendar.events"],
    )
    creds.refresh(GoogleRequest())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_existing_events(service, window_start: dt.date, window_end: dt.date):
    """Return {workout_id: event} for events this script previously created."""
    time_min = dt.datetime.combine(window_start, dt.time.min).isoformat() + "Z"
    time_max = dt.datetime.combine(window_end, dt.time.max).isoformat() + "Z"
    page_token = None
    out: dict[str, dict[str, Any]] = {}
    while True:
        resp = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                privateExtendedProperty=f"{EVENT_TAG_KEY}={EVENT_TAG_VALUE}",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        for e in resp.get("items", []):
            props = (e.get("extendedProperties") or {}).get("private") or {}
            wid = props.get(EVENT_WORKOUT_ID_KEY)
            if wid:
                out[wid] = e
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def sync_events(service, workouts: list[dict[str, Any]], window_start, window_end):
    existing = list_existing_events(service, window_start, window_end)
    log.info("Found %d existing synced events in calendar window.", len(existing))

    seen: set[str] = set()
    created = updated = deleted = unchanged = 0

    for w in workouts:
        wid = str(w.get("workoutId") or "")
        if not wid:
            continue
        body = workout_to_event(w)
        if body is None:
            continue
        seen.add(wid)

        if wid in existing:
            old_fp = (
                (existing[wid].get("extendedProperties") or {})
                .get("private", {})
                .get(EVENT_FINGERPRINT_KEY)
            )
            new_fp = body["extendedProperties"]["private"][EVENT_FINGERPRINT_KEY]
            if old_fp == new_fp:
                unchanged += 1
                continue
            if DRY_RUN:
                log.info("[dry-run] UPDATE workout %s — %s", wid, body["summary"])
            else:
                service.events().update(
                    calendarId=GOOGLE_CALENDAR_ID,
                    eventId=existing[wid]["id"],
                    body=body,
                ).execute()
            updated += 1
        else:
            if DRY_RUN:
                log.info("[dry-run] CREATE workout %s — %s", wid, body["summary"])
            else:
                service.events().insert(
                    calendarId=GOOGLE_CALENDAR_ID, body=body
                ).execute()
            created += 1

    # Delete events whose TP workout has disappeared from the window.
    for wid, event in existing.items():
        if wid in seen:
            continue
        if DRY_RUN:
            log.info("[dry-run] DELETE stale event for workout %s", wid)
        else:
            try:
                service.events().delete(
                    calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]
                ).execute()
            except HttpError as e:
                if e.resp.status == 410:  # already gone
                    pass
                else:
                    raise
        deleted += 1

    log.info(
        "Sync summary: %d created, %d updated, %d deleted, %d unchanged.",
        created,
        updated,
        deleted,
        unchanged,
    )
    return created, updated, deleted, unchanged


# ---------------------------------------------------------------------------
# Health check


def health_check(workouts: list[dict[str, Any]]) -> None:
    """Fail loudly if the result looks suspiciously empty.

    Coach-scheduled athletes should virtually always have at least one planned
    workout in the next week. A totally-empty response likely means the API
    call silently returned nothing (auth drift, schema change, etc.)."""
    today = dt.date.today()
    week = today + dt.timedelta(days=7)
    upcoming = [
        w
        for w in workouts
        if (d := _parse_workout_day(w.get("workoutDay"))) and today <= d <= week
    ]
    if not upcoming:
        raise RuntimeError(
            "Health check failed: 0 workouts found for the next 7 days. "
            "This is almost certainly a sync problem — investigate before "
            "trusting calendar state."
        )


# ---------------------------------------------------------------------------
# Main


async def main() -> int:
    today = dt.date.today()
    window_start = today - dt.timedelta(days=SYNC_PAST_DAYS)
    window_end = today + dt.timedelta(days=SYNC_DAYS)

    log.info("Logging into TrainingPeaks as %s...", TP_USERNAME)
    cookie = await tp_get_auth_cookie()
    log.info("Got cookie, exchanging for access token...")
    token = await tp_exchange_cookie_for_token(cookie)
    athlete_id = await tp_get_athlete_id(token)
    log.info("Athlete ID: %d", athlete_id)

    log.info(
        "Fetching workouts %s → %s...",
        window_start.isoformat(),
        window_end.isoformat(),
    )
    workouts = await tp_fetch_workouts(token, athlete_id, window_start, window_end)
    log.info("Fetched %d workouts from TrainingPeaks.", len(workouts))

    health_check(workouts)

    log.info("Syncing to Google Calendar (%s)...", GOOGLE_CALENDAR_ID)
    service = gcal_service()
    sync_events(service, workouts, window_start, window_end)

    log.info("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:  # noqa: BLE001 — we want a non-zero exit on *anything*
        log.exception("Sync failed: %s", e)
        sys.exit(1)
