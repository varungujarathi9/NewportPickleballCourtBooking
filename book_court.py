#!/usr/bin/env python3
"""
Auto-book a court on Newport Racquet Club (PodPlay) the moment it opens up.

Setup (one-time):
  1. Run the extraction snippet described in README.md in your browser's
     DevTools console while logged into the site. It saves your Firebase
     refresh token to ~/.podplay_auth.json on YOUR machine. This never
     passes through any chat or third party.
  2. pip install requests --break-system-packages
  3. Adjust the CONFIG section below to match your target booking.
  4. Test manually: python3 book_court.py --dry-run
  5. Schedule via cron (see README.md).

This script:
  - Refreshes a short-lived Firebase ID token using your stored refresh token
  - Polls the /apis/v2/sessions endpoint for the target date/pod
  - Once the target time slot shows availability, submits the booking
  - Retries with backoff if the slot isn't open yet or the booking POST
    fails transiently (503/429), and gives up cleanly on hard failures
    (401 = your refresh token is no longer valid; 422 = slot unavailable
    or bad request).
"""

import json
import sys
import time
import argparse
import datetime as dt
from pathlib import Path

import requests

# ============ CONFIG - edit these for your booking ============

TENANT_HOST = "https://newportracquetclub.podplay.app"
FIREBASE_API_KEY = "AIzaSyBBm_DN59sHmw4OW5HiwSVwyH_jMypYCpE"  # public client key, not secret

POD_ID = "019ee159-05b2-7cc6-b5d3-9f608eff4eb5"  # Ground Level Courts
POD_SLUG = "ground-level"
POD_QUERY = "ground-level-courts"

BOOKING_HOUR = 18       # 18 = 6:00pm. 24h format, local club time.
BOOKING_HOUR_MINUTE = 0
DURATION_MINUTES = 60   # 1 hour, matches your example (two 30-min chunks)
GROUP_SIZE = 4          # "1-4 Players" bucket seen in your example
DAYS_OUT = 7            # book exactly 7 days ahead

AUTH_FILE = Path.home() / ".podplay_auth.json"

# Poll behavior once the target day is within reach
POLL_INTERVAL_SECONDS = 5
MAX_POLL_MINUTES = 15    # give up trying after this long past the target unlock

# ================================================================


def load_refresh_token() -> str:
    if not AUTH_FILE.exists():
        sys.exit(
            f"No auth file found at {AUTH_FILE}.\n"
            "Run the one-time extraction step in README.md first."
        )
    data = json.loads(AUTH_FILE.read_text())
    token = data.get("refresh_token")
    if not token:
        sys.exit(f"{AUTH_FILE} is missing 'refresh_token'.")
    return token


def get_id_token(refresh_token: str) -> str:
    """Exchange the long-lived Firebase refresh token for a fresh ID token."""
    url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
    resp = requests.post(
        url,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    if resp.status_code != 200:
        sys.exit(
            f"Failed to refresh auth token ({resp.status_code}): {resp.text}\n"
            "Your refresh token may have been revoked - log in on the site "
            "again and redo the one-time extraction step."
        )
    return resp.json()["id_token"]


def target_date() -> dt.date:
    return dt.date.today() + dt.timedelta(days=DAYS_OUT)


def epoch_ms(d: dt.date, hour: int, minute: int) -> int:
    # Uses local system time; adjust if the club's timezone differs from
    # the machine running this script.
    local_dt = dt.datetime(d.year, d.month, d.day, hour, minute)
    return int(local_dt.timestamp() * 1000)


def fetch_sessions(id_token: str, d: dt.date) -> dict:
    start = dt.datetime(d.year, d.month, d.day)
    end = start + dt.timedelta(days=1)
    url = f"{TENANT_HOST}/apis/v2/sessions"
    params = {
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "podId": POD_ID,
    }
    headers = {"Authorization": f"Bearer {id_token}", "Accept": "*/*"}
    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def is_target_slot_open(sessions_payload: dict, start_ms: int) -> bool:
    """
    Inspect the sessions payload for whether the target start time has an
    open table. NOTE: the exact shape of this payload wasn't captured during
    setup - inspect a real response (see README's `--dump-sessions` step)
    and adjust this function to match. This is a best-effort default.
    """
    items = sessions_payload.get("items") or sessions_payload.get("data") or []
    for item in items:
        item_start = item.get("startTime") or item.get("start")
        if item_start is None:
            continue
        # Normalize to epoch ms if given as ISO string
        if isinstance(item_start, str):
            try:
                item_start = int(
                    dt.datetime.fromisoformat(
                        item_start.replace("Z", "+00:00")
                    ).timestamp()
                    * 1000
                )
            except ValueError:
                continue
        if item_start == start_ms:
            available = item.get("availableTables") or item.get("openCourts")
            if available:
                return True
    return False


def build_booking_payload(d: dt.date) -> dict:
    start_ms = epoch_ms(d, BOOKING_HOUR, BOOKING_HOUR_MINUTE)
    half_hour_ms = 30 * 60 * 1000
    items = []
    slots_needed = max(1, DURATION_MINUTES // 30)
    for i in range(slots_needed):
        slot_start = start_ms + i * half_hour_ms
        items.append(
            {
                "session": {"id": f"{POD_ID}@{slot_start}"},
                "sessionTable": {"id": f"{POD_ID}@auto@{slot_start}"},
                "sessionGuests": {"id": f"{POD_ID}@{GROUP_SIZE}@{slot_start}"},
            }
        )
    return {
        "type": "BOOK",
        "items": items,
        "chargeStrategy": "ONLY_OWNER",
        "passesStrategy": "USE_ALL",
        "couponCode": None,
        "virtualCredits": None,
        "termsAgreed": True,
        "bookingMode": "USER_BOOKED",
        "coachTypes": ["ROBOT"],
    }


def submit_booking(id_token: str, payload: dict) -> requests.Response:
    url = f"{TENANT_HOST}/apis/v2/bookings"
    headers = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": TENANT_HOST,
        "Referer": f"{TENANT_HOST}/book/{POD_SLUG}/preview/{POD_QUERY}",
    }
    return requests.post(url, headers=headers, json=payload, timeout=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="Do everything except submit the booking"
    )
    parser.add_argument(
        "--dump-sessions",
        action="store_true",
        help="Print the raw /sessions response for the target date and exit "
        "(use this once to see the real shape and fix is_target_slot_open)",
    )
    args = parser.parse_args()

    refresh_token = load_refresh_token()
    id_token = get_id_token(refresh_token)
    d = target_date()
    start_ms = epoch_ms(d, BOOKING_HOUR, BOOKING_HOUR_MINUTE)

    if args.dump_sessions:
        sessions = fetch_sessions(id_token, d)
        print(json.dumps(sessions, indent=2))
        return

    print(f"Targeting {d.isoformat()} {BOOKING_HOUR:02d}:{BOOKING_HOUR_MINUTE:02d} "
          f"({DURATION_MINUTES} min, group size {GROUP_SIZE})")

    deadline = time.time() + MAX_POLL_MINUTES * 60
    while time.time() < deadline:
        try:
            sessions = fetch_sessions(id_token, d)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                print("ID token expired mid-run, refreshing...")
                id_token = get_id_token(refresh_token)
                continue
            print(f"Error checking sessions: {e}. Retrying...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if is_target_slot_open(sessions, start_ms):
            print("Slot is open. Booking now...")
            payload = build_booking_payload(d)
            if args.dry_run:
                print("[dry run] Would POST:", json.dumps(payload, indent=2))
                return
            resp = submit_booking(id_token, payload)
            print(f"Booking response: {resp.status_code} {resp.text}")
            if resp.status_code in (200, 201):
                print("Booked successfully.")
                return
            if resp.status_code == 401:
                print("Token expired, refreshing and retrying once...")
                id_token = get_id_token(refresh_token)
                resp = submit_booking(id_token, payload)
                print(f"Retry response: {resp.status_code} {resp.text}")
                if resp.status_code in (200, 201):
                    print("Booked successfully on retry.")
                return
            if resp.status_code in (503, 429):
                print("Transient error, retrying shortly...")
                time.sleep(2)
                continue
            print("Booking failed with a non-retryable error. Stopping.")
            return

        time.sleep(POLL_INTERVAL_SECONDS)

    print(f"Gave up after {MAX_POLL_MINUTES} minutes - slot never showed as open.")


if __name__ == "__main__":
    main()
