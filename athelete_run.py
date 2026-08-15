import base64
import hashlib
import hmac
import html
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import psycopg2
import requests
import streamlit as st


# =========================================================
# OLLU ROSTER — CSV IS THE SOURCE OF TRUTH
# =========================================================

ROSTER_PATH = Path(__file__).with_name("ollu_roster_csv")
TEAM_IMAGES_DIR = Path(__file__).with_name("team_images")




def get_team_image(team_id):
    """
    Return the VEKDYN-controlled image saved for a team.
    Visitors can view team branding but cannot upload or replace it.
    """
    for extension in (".png", ".jpg", ".jpeg", ".webp"):
        image_path = TEAM_IMAGES_DIR / f"{team_id}{extension}"
        if image_path.exists():
            return image_path
    return None

def clean_csv_value(value, default=""):
    """Turn blank/NaN CSV values into safe VEKDYN values."""
    if pd.isna(value):
        return default
    value = str(value).strip()
    return default if value.lower() == "nan" else value


def load_ollu_roster():
    """
    Load the complete VEKDYN roster from ollu_roster_csv.
    athlete_id is the permanent ID used by the dashboard, Strava and notes.
    """
    roster = pd.read_csv(ROSTER_PATH, dtype=str, keep_default_na=False).fillna("")

    required = {
        "athlete_id", "first_name", "last_name",
        "school", "team", "class_year",
    }
    missing = required - set(roster.columns)
    if missing:
        raise RuntimeError(
            "Missing CSV columns: " + ", ".join(sorted(missing))
        )

    roster["athlete_id"] = roster["athlete_id"].astype(str).str.strip()

    duplicate_ids = roster.loc[
        roster["athlete_id"].duplicated(keep=False), "athlete_id"
    ].tolist()
    if duplicate_ids:
        raise RuntimeError(
            "Duplicate athlete_id values: "
            + ", ".join(sorted(set(duplicate_ids)))
        )

    athletes = {}

    for _, row in roster.iterrows():
        athlete_id = clean_csv_value(row.get("athlete_id"))
        if not athlete_id:
            continue

        first_name = clean_csv_value(row.get("first_name"))
        last_name = clean_csv_value(row.get("last_name"))
        full_name = f"{first_name} {last_name}".strip()

        pbs = {}
        pb_map = {
            "800": "pb_800",
            "1500": "pb_1500",
            "mile": "pb_mile",
            "3k": "pb_3k",
            "5k": "pb_5k",
        }
        for event, column in pb_map.items():
            value = clean_csv_value(row.get(column))
            if value:
                pbs[event] = value

        xc_results = {}
        xc_8k = clean_csv_value(row.get("xc_8k_pb"))
        xc_10k = clean_csv_value(row.get("xc_10k_pb"))

        if xc_8k:
            xc_results["8k"] = [
                {"time": xc_8k, "meet": "XC Personal Best", "date": ""}
            ]
        if xc_10k:
            xc_results["10k"] = [
                {"time": xc_10k, "meet": "XC Personal Best", "date": ""}
            ]

        threshold = {
            "short_reps": {
                "pace": clean_csv_value(row.get("threshold_short_pace"), "--"),
                "lactate": clean_csv_value(row.get("threshold_short_lactate"), "--"),
            },
            "medium_reps": {
                "pace": clean_csv_value(row.get("threshold_medium_pace"), "--"),
                "lactate": clean_csv_value(row.get("threshold_medium_lactate"), "--"),
            },
            "long_reps": {
                "pace": clean_csv_value(row.get("threshold_long_pace"), "--"),
                "lactate": clean_csv_value(row.get("threshold_long_lactate"), "--"),
            },
        }

        athletes[athlete_id] = {
            "profile": {
                "name": full_name or athlete_id,
                "first_name": first_name,
                "last_name": last_name,
                "school": clean_csv_value(row.get("school"), "OLLU"),
                "team": clean_csv_value(row.get("team")),
                "class": clean_csv_value(row.get("class_year"), "--"),
            },
            "pbs": pbs,
            "xc_results": xc_results,
            "threshold": threshold,

            # Live training/HR is intentionally NOT stored in the CSV.
            # Strava fills these areas after each athlete connects/syncs.
            "training": {},
            "recovery": {},

            "strava_connected_csv": (
                clean_csv_value(row.get("strava_connected")).lower() == "true"
            ),
            "coros_connected_csv": (
                clean_csv_value(row.get("coros_connected")).lower() == "true"
            ),
        }

    if not athletes:
        raise RuntimeError("No athletes were loaded from ollu_roster_csv.")

    return roster, athletes


ollu_roster, athletes = load_ollu_roster()


# =========================================================
# PAGE SETTINGS
# =========================================================

st.set_page_config(
    page_title="VEKDYN",
    page_icon="🏃",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =========================================================
# AUTHENTICATION / PERSISTENT LOGIN / PAGE ROUTING
# =========================================================

LOGIN_SESSION_DAYS = 7


TEAM_CONFIG = {
    "ollu_distance": {
        "name": "OLLU Distance",
        "short_name": "OLLU",
    },
    "sam_houston": {
        "name": "Sam Houston Distance",
        "short_name": "Sam Houston",
    },
}


def team_config(team_id):
    """Return display settings for one VEKDYN team."""
    return TEAM_CONFIG.get(
        team_id,
        {"name": "VEKDYN Team", "short_name": "Team"},
    )


def check_login(team_id, username, password):
    """
    Check credentials for the selected team.

    secrets.toml format:

    [team_logins.ollu_distance]
    username = "..."
    password = "..."

    [team_logins.sam_houston]
    username = "..."
    password = "..."
    """
    try:
        team_login = st.secrets["team_logins"][team_id]
        correct_username = team_login["username"]
        correct_password = team_login["password"]
    except (KeyError, TypeError, FileNotFoundError):
        return False

    return (
        hmac.compare_digest(str(username), str(correct_username))
        and hmac.compare_digest(str(password), str(correct_password))
    )


def get_login_signing_secret():
    """
    Sign VEKDYN login sessions without ever storing the coach password
    in the browser. A dedicated LOGIN_SECRET can be added later.
    """
    try:
        return str(st.secrets["LOGIN_SECRET"])
    except (KeyError, FileNotFoundError):
        return str(st.secrets["STRAVA_CLIENT_SECRET"])


def create_login_token(username, team_id):
    """Create a signed login token tied to one VEKDYN team."""
    expires_at = int(
        time.time() + timedelta(days=LOGIN_SESSION_DAYS).total_seconds()
    )
    payload = f"{team_id}:{username}:{expires_at}"
    signature = hmac.new(
        get_login_signing_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}:{signature}"


def validate_login_token(token):
    """Return (username, team_id) when a signed team session is valid."""
    if not token:
        return None

    try:
        team_id, username, expires_at, returned_signature = token.rsplit(":", 3)
        expires_at = int(expires_at)
    except (ValueError, TypeError):
        return None

    if team_id not in TEAM_CONFIG or time.time() > expires_at:
        return None

    payload = f"{team_id}:{username}:{expires_at}"
    expected_signature = hmac.new(
        get_login_signing_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(returned_signature, expected_signature):
        return None

    return username, team_id


def restore_login_session():
    """Restore the correct team workspace after a browser refresh."""
    if st.session_state.get("logged_in"):
        return

    login_token = st.query_params.get("session")
    restored = validate_login_token(login_token)

    if restored:
        username, team_id = restored
        st.session_state["logged_in"] = True
        st.session_state["logged_in_user"] = username
        st.session_state["active_team"] = team_id
        st.session_state["page"] = "dashboard"

def log_out():
    """End the current VEKDYN login session."""
    st.session_state["logged_in"] = False
    st.session_state.pop("logged_in_user", None)
    st.session_state.pop("active_team", None)
    st.session_state.pop("pending_team", None)

    if "session" in st.query_params:
        del st.query_params["session"]

    st.session_state["page"] = "home"
    st.rerun()


if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if "page" not in st.session_state:
    st.session_state.page = "home"

if "pending_team" not in st.session_state:
    st.session_state.pending_team = None


restore_login_session()

# =========================================================
# NEON DATABASE
# =========================================================

def get_database_connection():
    """Open a connection to the VEKDYN Neon PostgreSQL database."""
    return psycopg2.connect(st.secrets["DATABASE_URL"])


def test_neon_connection():
    """Test whether VEKDYN can reach Neon."""
    try:
        conn = get_database_connection()
        conn.close()
        return True

    except Exception as e:
        st.error(f"Neon connection failed: {e}")
        return False


# =========================================================
# STRAVA SETTINGS
# =========================================================

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"

STRAVA_REDIRECT_URI = "https://atheleterunpy-n8eens7b7gdf4xcsn9vvzf.streamlit.app"


# =========================================================
# STRAVA DATABASE — NEON POSTGRESQL
# =========================================================

def initialize_strava_database():
    """
    Create the Strava connection table in Neon if it does not
    already exist.
    """

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS strava_connections (
                    athlete_key TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at BIGINT NOT NULL,
                    scope TEXT,
                    strava_athlete_id BIGINT UNIQUE,
                    strava_name TEXT
                )
                """
            )


def load_saved_strava_connection(athlete_key):
    """
    Load one athlete's saved Strava connection from Neon.
    """

    initialize_strava_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    athlete_key,
                    access_token,
                    refresh_token,
                    expires_at,
                    scope,
                    strava_athlete_id,
                    strava_name
                FROM strava_connections
                WHERE athlete_key = %s
                """,
                (athlete_key,),
            )

            row = cursor.fetchone()

    if not row:
        return {}

    return {
        "athlete_key": row[0],
        "access_token": row[1],
        "refresh_token": row[2],
        "expires_at": row[3],
        "scope": row[4],
        "strava_athlete_id": row[5],
        "strava_name": row[6],
    }


def saved_owner_of_strava_account(strava_athlete_id):
    """
    Check whether this Strava account is already connected
    to another VEKDYN athlete.
    """

    if not strava_athlete_id:
        return None

    initialize_strava_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:

            cursor.execute(
                """
                SELECT athlete_key
                FROM strava_connections
                WHERE strava_athlete_id = %s
                """,
                (strava_athlete_id,),
            )

            row = cursor.fetchone()

    return row[0] if row else None

def persist_strava_connection(athlete_key, connection):
    """
    Save an athlete's Strava OAuth tokens to Neon.

    If the athlete already exists, update their tokens instead
    of creating another record.
    """

    initialize_strava_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:

            cursor.execute(
                """
                INSERT INTO strava_connections (
                    athlete_key,
                    access_token,
                    refresh_token,
                    expires_at,
                    scope,
                    strava_athlete_id,
                    strava_name
                )

                VALUES (%s, %s, %s, %s, %s, %s, %s)

                ON CONFLICT (athlete_key)

                DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_at = EXCLUDED.expires_at,
                    scope = EXCLUDED.scope,
                    strava_athlete_id = EXCLUDED.strava_athlete_id,
                    strava_name = EXCLUDED.strava_name
                """,

                (
                    athlete_key,
                    connection["access_token"],
                    connection["refresh_token"],
                    int(connection["expires_at"]),
                    connection.get("scope", ""),
                    connection.get("strava_athlete_id"),
                    connection.get("strava_name", ""),
                ),
            )


# =========================================================
# ATHLETE + COACH NOTES — NEON POSTGRESQL
# =========================================================

TEAM_TIMEZONE = ZoneInfo("America/Chicago")


def initialize_notes_database():
    """Create a shared athlete/coach notes feed in Neon."""
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_notes (
                    id BIGSERIAL PRIMARY KEY,
                    athlete_key TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    author_role TEXT NOT NULL,
                    note_text TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT athlete_notes_role_check
                        CHECK (author_role IN ('COACH', 'ATHLETE'))
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS athlete_notes_athlete_created_idx
                ON athlete_notes (athlete_key, created_at DESC)
                """
            )


def save_athlete_note(athlete_key, author_name, author_role, note_text):
    """Save one coach or athlete note to the selected athlete's Neon feed."""
    clean_note = str(note_text).strip()
    clean_author = str(author_name).strip()
    clean_role = str(author_role).strip().upper()

    if not clean_note:
        raise ValueError("Write a note before saving.")
    if clean_role not in {"COACH", "ATHLETE"}:
        raise ValueError("Note role must be COACH or ATHLETE.")
    if not clean_author:
        raise ValueError("The note needs an author name.")

    initialize_notes_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO athlete_notes (
                    athlete_key,
                    author_name,
                    author_role,
                    note_text
                )
                VALUES (%s, %s, %s, %s)
                """,
                (athlete_key, clean_author, clean_role, clean_note),
            )


def load_athlete_notes(athlete_key, limit=40, role_filter=None):
    """Load the newest shared notes for one athlete from Neon."""
    initialize_notes_database()

    query = """
        SELECT id, author_name, author_role, note_text, created_at
        FROM athlete_notes
        WHERE athlete_key = %s
    """
    params = [athlete_key]

    if role_filter in {"COACH", "ATHLETE"}:
        query += " AND author_role = %s"
        params.append(role_filter)

    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(int(limit))

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()

    return [
        {
            "id": row[0],
            "author_name": row[1],
            "author_role": row[2],
            "note_text": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


def format_note_timestamp(created_at):
    """Format a Neon timestamp in the OLLU/San Antonio timezone."""
    if not created_at:
        return ""
    try:
        local_time = created_at.astimezone(TEAM_TIMEZONE)
        return local_time.strftime("%b %d, %Y · %-I:%M %p")
    except (AttributeError, ValueError):
        return str(created_at)


# =========================================================
# STRAVA SECRET HELPERS
# =========================================================

def strava_secret(name, default=None):
    """Read a Strava setting without crashing if it has not been added yet."""

    try:
        return st.secrets[name]

    except (KeyError, FileNotFoundError):
        return default


def strava_connections():
    """
    Return the in-session Strava connection store,
    keyed by VEKDYN athlete key.
    """

    return st.session_state.setdefault(
        "strava_connections",
        {}
    )


def athlete_strava_connection(athlete_key):
    """
    First look for the athlete's Strava connection in the
    current Streamlit session.

    If it isn't there, load it from Neon.
    """

    connection = strava_connections().get(athlete_key)

    if connection:
        return connection

    connection = load_saved_strava_connection(athlete_key)

    if connection:
        strava_connections()[athlete_key] = connection

    return connection



def strava_secret(name, default=None):
    """Read a Strava setting without crashing if it has not been added yet."""
    try:
        return st.secrets[name]
    except (KeyError, FileNotFoundError):
        return default


def strava_connections():
    """Return the in-session connection store keyed by VEKDYN athlete key."""
    return st.session_state.setdefault("strava_connections", {})


def athlete_strava_connection(athlete_key):
    connection = strava_connections().get(athlete_key)
    if connection:
        return connection

    connection = load_saved_strava_connection(athlete_key)
    if connection:
        strava_connections()[athlete_key] = connection
    return connection


def configured_refresh_token(athlete_key):
    """Read an optional saved refresh token for one athlete from secrets.toml."""
    try:
        token = st.secrets["strava_athletes"][athlete_key]["refresh_token"]
        if token:
            return token
    except (KeyError, TypeError, FileNotFoundError):
        pass

    # Preserve the original single-athlete setup while Diego transitions to
    # the new per-athlete format.
    if athlete_key.strip().lower() == "diego":
        return strava_secret("STRAVA_REFRESH_TOKEN")

    return None


def strava_is_connected(athlete_key):
    connection = athlete_strava_connection(athlete_key)
    return bool(
        connection.get("refresh_token")
        or configured_refresh_token(athlete_key)
    )


def create_strava_login_url(athlete_key):
    client_id = strava_secret("STRAVA_CLIENT_ID")
    client_secret = strava_secret("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    nonce = secrets.token_urlsafe(12)
    state_payload = f"{athlete_key}:{nonce}"
    state_signature = hmac.new(
        str(client_secret).encode("utf-8"),
        state_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    oauth_state = f"{state_payload}:{state_signature}"

    parameters = {
        "client_id": client_id,
        "redirect_uri": STRAVA_REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": "read,activity:read_all",
        "state": oauth_state,
    }
    return f"{STRAVA_AUTHORIZE_URL}?{urlencode(parameters)}"


def athlete_key_from_oauth_state(oauth_state):
    """Validate Strava's returned state and recover the VEKDYN athlete key."""
    client_secret = strava_secret("STRAVA_CLIENT_SECRET")
    if not oauth_state or not client_secret:
        return None

    try:
        athlete_key, nonce, returned_signature = oauth_state.split(":", 2)
    except ValueError:
        return None

    state_payload = f"{athlete_key}:{nonce}"
    expected_signature = hmac.new(
        str(client_secret).encode("utf-8"),
        state_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(returned_signature, expected_signature):
        return None

    return athlete_key


def save_token_data(athlete_key, token_data):
    """Keep one athlete's newest Strava tokens isolated from every other athlete."""
    athlete_details = token_data.get("athlete", {})
    connection = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_at": token_data["expires_at"],
        "scope": token_data.get("scope", ""),
        "strava_athlete_id": athlete_details.get("id"),
        "strava_name": " ".join(
            part
            for part in [
                athlete_details.get("firstname", ""),
                athlete_details.get("lastname", ""),
            ]
            if part
        ).strip(),
    }
    existing_owner = saved_owner_of_strava_account(
        connection.get("strava_athlete_id")
    )
    if existing_owner and existing_owner != athlete_key:
        owner_name = athletes.get(existing_owner, {}).get("profile", {}).get(
            "name", existing_owner
        )
        raise RuntimeError(
            f"This Strava account is already connected to {owner_name}."
        )

    persist_strava_connection(athlete_key, connection)
    strava_connections()[athlete_key] = connection
    # Never leave results from an older/wrong account attached to this profile.
    st.session_state.pop(f"{athlete_key}_strava_weekly", None)
    st.session_state.pop(f"{athlete_key}_strava_heart_rate", None)
    st.session_state.pop(f"strava_error_{athlete_key}", None)
    return connection


def normalized_person_name(name):
    """Normalize names so capitalization and punctuation do not cause a mismatch."""
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def verify_strava_identity(athlete_key, token_data):
    """Prevent one athlete's Strava account from being assigned to another."""
    expected_name = athletes[athlete_key]["profile"].get("name", "")
    strava_athlete = token_data.get("athlete", {})
    returned_name = " ".join(
        part
        for part in [
            strava_athlete.get("firstname", ""),
            strava_athlete.get("lastname", ""),
        ]
        if part
    ).strip()

    if not returned_name:
        raise RuntimeError("Strava did not return the athlete's name.")

    expected_first_name = expected_name.split()[0] if expected_name.split() else ""
    returned_first_name = strava_athlete.get("firstname", "")
    full_name_matches = (
        normalized_person_name(returned_name)
        == normalized_person_name(expected_name)
    )
    first_name_matches = (
        bool(expected_first_name)
        and normalized_person_name(returned_first_name)
        == normalized_person_name(expected_first_name)
    )

    # Some VEKDYN profiles currently contain only a first name ("Diego"),
    # while Strava returns the full name ("Diego Moran").
    if not (full_name_matches or first_name_matches):
        raise RuntimeError(
            f"You selected {expected_name}, but Strava authorized {returned_name}. "
            f"Log out of {returned_name}'s Strava account, then connect {expected_name}."
        )


def exchange_authorization_code(code, athlete_key):
    response = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": strava_secret("STRAVA_CLIENT_ID"),
            "client_secret": strava_secret("STRAVA_CLIENT_SECRET"),
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    response.raise_for_status()
    token_data = response.json()
    verify_strava_identity(athlete_key, token_data)
    save_token_data(athlete_key, token_data)
    return token_data["access_token"]


def refresh_strava_token(athlete_key):
    connection = athlete_strava_connection(athlete_key)
    refresh_token = (
        connection.get("refresh_token")
        or configured_refresh_token(athlete_key)
    )

    if not refresh_token:
        raise RuntimeError(
            f"No Strava refresh token is available for {athlete_key}."
        )

    response = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": strava_secret("STRAVA_CLIENT_ID"),
            "client_secret": strava_secret("STRAVA_CLIENT_SECRET"),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    response.raise_for_status()
    token_data = response.json()
    # Refresh responses do not include the athlete profile, so preserve the
    # identity returned during the original authorization.
    token_data["athlete"] = {
        "id": connection.get("strava_athlete_id"),
        "firstname": connection.get("strava_name", ""),
        "lastname": "",
    }
    save_token_data(athlete_key, token_data)
    return token_data["access_token"]


def get_valid_strava_token(athlete_key):
    connection = athlete_strava_connection(athlete_key)
    access_token = connection.get("access_token")
    expires_at = connection.get("expires_at", 0)

    if access_token and expires_at > time.time() + 60:
        return access_token

    return refresh_strava_token(athlete_key)


def get_strava_training_data(access_token, number_of_weeks=8):
    start_date = datetime.now(timezone.utc) - timedelta(weeks=number_of_weeks)

    response = requests.get(
        STRAVA_ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "after": int(start_date.timestamp()),
            "page": 1,
            "per_page": 200,
        },
        timeout=15,
    )
    response.raise_for_status()

    runs = [
        activity
        for activity in response.json()
        if activity.get("sport_type") in {"Run", "TrailRun", "VirtualRun"}
    ]

    if not runs:
        return pd.DataFrame(columns=["Week", "Mileage"]), {}

    run_data = pd.DataFrame(
        {
            "Date": [run["start_date_local"] for run in runs],
            "Mileage": [run["distance"] / 1609.344 for run in runs],
            "MovingTime": [run.get("moving_time", 0) for run in runs],
            "AverageHR": [run.get("average_heartrate") for run in runs],
            "MaxHR": [run.get("max_heartrate") for run in runs],
            "HasHR": [run.get("has_heartrate", False) for run in runs],
        }
    )
    # Strava's local timestamps can include a timezone offset.  The chart's
    # generated Monday dates are timezone-free, so reduce every activity to
    # its local calendar date before grouping.  Otherwise reindex() sees no
    # matching weeks and silently fills every value with zero.
    run_data["Date"] = pd.to_datetime(
        run_data["Date"].astype(str).str[:10],
        format="%Y-%m-%d",
    )
    run_data["WeekStart"] = (
        run_data["Date"]
        - pd.to_timedelta(run_data["Date"].dt.weekday, unit="day")
    ).dt.normalize()

    current_monday = pd.Timestamp.now().normalize() - pd.to_timedelta(
        pd.Timestamp.now().weekday(), unit="day"
    )
    all_weeks = pd.date_range(
        end=current_monday,
        periods=number_of_weeks,
        freq="7D",
    )

    weekly = run_data.groupby("WeekStart")["Mileage"].sum().reindex(
        all_weeks,
        fill_value=0,
    )

    weekly_mileage = pd.DataFrame(
        {
            "Week": all_weeks.strftime("%b %d"),
            "Mileage": weekly.round(1).to_numpy(),
        }
    )

    # Use the seven most recent runs that actually contain HR. This prevents
    # the card from falling back to dictionary data when the current week has
    # mileage but no recorded heart rate yet. Weight average HR by moving time
    # so a short warm-up does not count as much as a long run.
    all_heart_runs = run_data[
        run_data["HasHR"].fillna(False).astype(bool)
        & run_data["AverageHR"].notna()
    ].copy()

    heart_runs = (
        all_heart_runs
        .sort_values("Date", ascending=False)
        .head(7)
        .copy()
    )

    heart_summary = {}
    if not heart_runs.empty:
        total_hr_time = heart_runs.loc[
            heart_runs["MovingTime"] > 0,
            "MovingTime",
        ].sum()

        if total_hr_time > 0:
            weighted_hr = (
                heart_runs["AverageHR"] * heart_runs["MovingTime"]
            ).sum() / heart_runs["MovingTime"].sum()
        else:
            weighted_hr = heart_runs["AverageHR"].mean()

        recorded_max_hr = all_heart_runs["MaxHR"].dropna()
        max_hr_date = "Not available"
        if not recorded_max_hr.empty:
            max_hr_index = recorded_max_hr.idxmax()
            max_hr_date = all_heart_runs.loc[max_hr_index, "Date"].strftime(
                "%b %d, %Y"
            )

        heart_summary = {
            "average_heart_rate": round(float(weighted_hr)),
            "max_heart_rate": (
                round(float(recorded_max_hr.max()))
                if not recorded_max_hr.empty
                else "--"
            ),
            "max_heart_rate_date": max_hr_date,
            "activities_with_hr": int(len(heart_runs)),
            "last_updated": heart_runs["Date"].max().strftime("%b %d, %Y"),
        }

    return weekly_mileage, heart_summary


def dictionary_weekly_mileage(training):
    return pd.DataFrame(
        {
            "Week": training.get("weeks", []),
            "Mileage": training.get("weekly_volume", []),
        }
    )


def get_xc_results_for_event(xc_results, event):
    """Return XC results as a predictable list of dictionaries."""
    event_results = xc_results.get(event, xc_results.get(event.upper(), []))

    if isinstance(event_results, str):
        return [{"time": event_results, "meet": "", "date": ""}]

    if isinstance(event_results, dict):
        return [event_results]

    if isinstance(event_results, list):
        return [
            result
            for result in event_results
            if isinstance(result, dict)
        ]

    return []


def primary_xc_result(xc_results, event):
    """Use the first entered result as the result shown on the XC card."""
    results = get_xc_results_for_event(xc_results, event)
    if not results:
        return {"time": "--", "meet": "No result entered", "date": ""}

    return {
        "time": results[0].get("time", "--"),
        "meet": results[0].get("meet", "Meet not entered"),
        "date": results[0].get("date", ""),
    }


def lactate_value(threshold_lactate, key):
    """Format a lactate reading while allowing blank dictionary values."""
    value = threshold_lactate.get(key)
    if value in (None, "", "--"):
        return "--"

    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def open_team_workspace(team_id):
    """Send a visitor to the selected team's protected VEKDYN workspace."""
    if team_id not in TEAM_CONFIG:
        st.error("That VEKDYN team workspace is not configured.")
        return

    # A session authenticated for one school must not silently open another.
    if (
        st.session_state.get("logged_in")
        and st.session_state.get("active_team") == team_id
    ):
        st.session_state["page"] = "dashboard"
    else:
        st.session_state["logged_in"] = False
        st.session_state.pop("logged_in_user", None)
        st.session_state["pending_team"] = team_id
        st.session_state["page"] = "login"

        if "session" in st.query_params:
            del st.query_params["session"]

    st.rerun()


def render_login_page():
    """Show the login page for whichever VEKDYN team was selected."""
    pending_team = (
        st.session_state.get("pending_team")
        or st.session_state.get("active_team")
        or "ollu_distance"
    )
    config = team_config(pending_team)

    st.markdown(
        """
        <style>
            .stApp { background: #f6f8f6; }
            [data-testid="stSidebar"] { display: none; }
            .block-container { max-width: 560px; padding-top: 5rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("# VEKDYN")
    st.subheader(f"{config['name']} — Coach Login")
    st.caption("Sign in to access this private team workspace.")

    username = st.text_input(
        "Username",
        key=f"coach_username_{pending_team}",
    )
    password = st.text_input(
        "Password",
        type="password",
        key=f"coach_password_{pending_team}",
    )

    login_col, back_col = st.columns(2)

    with login_col:
        if st.button(
            "Log In",
            type="primary",
            use_container_width=True,
            key=f"login_{pending_team}",
        ):
            if check_login(pending_team, username, password):
                st.session_state["logged_in"] = True
                st.session_state["logged_in_user"] = username
                st.session_state["active_team"] = pending_team
                st.session_state["pending_team"] = None
                st.session_state["page"] = "dashboard"

                st.query_params["session"] = create_login_token(
                    username,
                    pending_team,
                )
                st.rerun()
            else:
                st.error("Incorrect username or password for this team.")

    with back_col:
        if st.button(
            "← Back",
            use_container_width=True,
            key=f"back_from_{pending_team}_login",
        ):
            st.session_state["page"] = "home"
            st.session_state["pending_team"] = None
            st.rerun()


def render_starter_page():
    """Show the clean public team-search landing page."""

    st.markdown(
        """
        <style>
            .stApp {
                background: #f6f8f6;
            }

            /* PUBLIC LANDING PAGE ONLY:
               remove Streamlit's white top/share bar and keep the sidebar hidden.
               These rules disappear after st.stop() and the private dashboard
               renders, so the normal sidebar/collapse control remains inside VEKDYN. */
            [data-testid="stHeader"],
            [data-testid="stToolbar"],
            [data-testid="stDecoration"],
            [data-testid="stStatusWidget"],
            [data-testid="stSidebar"],
            [data-testid="collapsedControl"],
            [data-testid="stSidebarCollapsedControl"] {
                display: none !important;
            }

            [data-testid="stAppViewContainer"] > .main {
                margin-left: 0 !important;
                padding-top: 0 !important;
            }

            .block-container {
                max-width: 1120px;
                padding-top: 1.4rem;
                padding-bottom: 3rem;
            }

            .starter-brand {
                font-size: 28px;
                font-weight: 800;
                color: #111827;
                letter-spacing: -0.8px;
                margin-bottom: 2.4rem;
            }

            .starter-brand span {
                color: #2f9e44;
            }

            .starter-heading {
                text-align: center;
                font-size: 52px;
                line-height: 1.05;
                font-weight: 800;
                letter-spacing: -1.7px;
                color: #111827;
                margin-top: 0.6rem;
            }

            .starter-subheading {
                text-align: center;
                color: #6b7280;
                font-size: 18px;
                margin: 0.7rem 0 1.8rem;
            }

            .recent-heading {
                color: #111827;
                font-size: 24px;
                font-weight: 750;
                margin-top: 1.8rem;
                margin-bottom: 0.6rem;
            }

            .recent-team-name {
                color: #111827;
                font-size: 20px;
                font-weight: 750;
                margin-top: 0.2rem;
            }

            .recent-team-meta {
                color: #6b7280;
                font-size: 14px;
                margin-top: 0.2rem;
            }

            .landing-image-label {
                color: #111827;
                font-size: 20px;
                font-weight: 700;
                margin-top: 1.4rem;
                margin-bottom: 0.35rem;
            }

            .landing-image-help {
                color: #6b7280;
                font-size: 14px;
                margin-bottom: 0.65rem;
            }

            .landing-footer {
                text-align: center;
                color: #111827;
                font-size: 22px;
                font-weight: 750;
                margin-top: 1.1rem;
            }

            .landing-footer-subtext {
                text-align: center;
                color: #6b7280;
                font-size: 15px;
                margin-top: 0.15rem;
            }

            .landing-banner img {
                width: 100%;
                height: 330px;
                border-radius: 14px;
                object-fit: cover;
                border: 1px solid #e5e7eb;
                display: block;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # -----------------------------------------------------
    # BRAND
    # -----------------------------------------------------

    st.markdown(
        '<div class="starter-brand">VEK<span>DYN</span></div>',
        unsafe_allow_html=True,
    )

    # -----------------------------------------------------
    # FIND YOUR TEAM
    # -----------------------------------------------------

    st.markdown(
        '<div class="starter-heading">Find your team</div>'
        '<div class="starter-subheading">Search for your school to access '
        'your VEKDYN workspace.</div>',
        unsafe_allow_html=True,
    )

    left_pad, search_col, right_pad = st.columns([0.25, 5.5, 0.25])

    with search_col:
        school_search = st.text_input(
            "Search school or team name",
            placeholder="Search school or team name",
            label_visibility="collapsed",
            key="landing_school_search",
        )

        normalized_search = school_search.strip().casefold()

        if normalized_search:
            searchable_teams = (
                "our lady of the lake university ollu distance san antonio "
                "sam houston state university sam houston shsu distance huntsville"
            )

            team_matches = any(
                phrase in searchable_teams
                for phrase in normalized_search.split()
            )

            if not team_matches:
                st.info("No team account matches that search yet.")

        # -------------------------------------------------
        # RECENTLY ACCESSED — FULL WIDTH
        # -------------------------------------------------

        st.markdown(
            '<div class="recent-heading">Recently accessed</div>',
            unsafe_allow_html=True,
        )

        for team_id, meta_text in [
            ("ollu_distance", f"{len(athletes)} athletes connected"),
            ("sam_houston", "Workspace being built"),
        ]:
            config = team_config(team_id)

            with st.container(border=True):
                recent_icon, recent_text, recent_button = st.columns([0.45, 4.2, 1.25])

                with recent_icon:
                    st.markdown("## 🏃")

                with recent_text:
                    st.markdown(
                        f'<div class="recent-team-name">{config["name"]}</div>'
                        f'<div class="recent-team-meta">{meta_text}</div>',
                        unsafe_allow_html=True,
                    )

                with recent_button:
                    st.write("")
                    if st.button(
                        "Open Team →",
                        key=f"open_{team_id}",
                        type="primary",
                        use_container_width=True,
                    ):
                        open_team_workspace(team_id)

        # -------------------------------------------------
        # TEAM / LANDING IMAGE
        # -------------------------------------------------

        team_image = get_team_image("ollu_distance")

        if team_image:
            st.image(str(team_image), use_container_width=True)
        else:
            st.markdown(
                """
                <div style="
                    width:100%;
                    min-height:260px;
                    border:1px solid #e5e7eb;
                    border-radius:16px;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    background:#f9fafb;
                    margin-top:12px;
                ">
                    <div style="text-align:center; color:#9ca3af;">
                        <div style="font-size:28px; margin-bottom:8px;">🏃</div>
                        <div style="font-size:14px; font-weight:600;">OLLU Distance</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        # -------------------------------------------------
        # FOOTER
        # -------------------------------------------------

        st.divider()

        st.markdown(
            '<div class="landing-footer">🏃 Built for the next generation of runners</div>'
            '<div class="landing-footer-subtext">Data. Insight. Performance.</div>',
            unsafe_allow_html=True,
        )


# =========================================================
# PUBLIC HOME / PRIVATE LOGIN ROUTING
# =========================================================

# Handle a Strava OAuth return BEFORE the public landing page can stop execution.
# This is essential on the deployed Streamlit URL because the OAuth return starts
# a fresh app run with ?code=...&state=... in the URL.
# Complete an OAuth return before drawing the dashboard.
authorization_code = st.query_params.get("code")
authorization_error = st.query_params.get("error")
returned_oauth_state = st.query_params.get("state")

if authorization_error:
    st.error("Strava authorization was cancelled or denied.")
    st.query_params.clear()

if authorization_code:
    try:
        connected_athlete_key = athlete_key_from_oauth_state(
            returned_oauth_state
        )

        if connected_athlete_key not in athletes:
            raise RuntimeError(
                "The Strava connection could not be matched to an VEKDYN profile. "
                "Return to the dashboard and select Connect Strava again."
            )

        exchange_authorization_code(
            authorization_code,
            connected_athlete_key,
        )
        connected_name = athletes[connected_athlete_key]["profile"]["name"]
        st.session_state[
            f"strava_message_{connected_athlete_key}"
        ] = f"{connected_name}'s Strava connected successfully."
        st.session_state["selected_athlete_key"] = connected_athlete_key
        st.session_state["active_team"] = "ollu_distance"
        st.query_params.clear()
        st.rerun()
    except (
        requests.RequestException,
        sqlite3.DatabaseError,
        KeyError,
        RuntimeError,
    ) as error:
        # If this athlete already has a valid saved Strava connection,
        # an old/reused OAuth callback code should not create a red
        # dashboard-wide error banner. The existing Neon connection
        # remains the source of truth.
        oauth_athlete_key = athlete_key_from_oauth_state(returned_oauth_state)

        if (
            oauth_athlete_key in athletes
            and strava_is_connected(oauth_athlete_key)
        ):
            st.session_state.pop(
                f"strava_error_{oauth_athlete_key}",
                None,
            )

            # Remove the stale one-time OAuth parameters so Streamlit
            # cannot try to exchange the same authorization code again.
            st.query_params.clear()
            st.rerun()
        else:
            st.error(f"Strava authorization failed: {error}")



# The landing page stays public. Opening OLLU sends visitors to login.
if st.session_state.get("page") == "login" and not st.session_state.get("logged_in"):
    render_login_page()
    st.stop()

if st.session_state.get("page") == "home":
    render_starter_page()
    st.stop()

# Never expose any team dashboard without authentication for that workspace.
if (
    not st.session_state.get("logged_in")
    or st.session_state.get("active_team") not in TEAM_CONFIG
):
    st.session_state["page"] = "login"
    render_login_page()
    st.stop()

active_team = st.session_state["active_team"]
active_team_config = team_config(active_team)


# Sam Houston has its own authenticated workspace now. Until its roster/data
# source is added, do not fall through into the OLLU athlete dashboard.
if active_team == "sam_houston":
    st.markdown("# VEKDYN")
    st.subheader("Sam Houston Distance")
    st.success("Private Sam Houston workspace connected.")
    st.caption(
        "This workspace is isolated from OLLU. Add the Sam Houston roster "
        "and team data here as you build the second team."
    )

    if st.button("Log Out", key="sam_houston_build_logout"):
        log_out()

    st.stop()


# =========================================================
# APP STYLING
# =========================================================

st.markdown(
    """
    <style>


        .stApp { background-color: #f6f8f6; }
        [data-testid="stSidebar"] {
            background-color: #ffffff;
            border-right: 1px solid #e5e7eb;
        }
        [data-testid="stMetric"] { background-color: transparent; }
        [data-testid="stMetricValue"] {
            font-size: 30px;
            font-weight: 700;
            color: #111827;
        }
        [data-testid="stMetricLabel"] { color: #6b7280; }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background-color: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.03);
        }
        h1, h2, h3 { color: #111827; }
        .green-text { color: #2f9e44; font-weight: 600; }
        .small-text { color: #6b7280; font-size: 14px; }
        .athlete-name {
            font-size: 34px;
            font-weight: 750;
            color: #111827;
            margin-bottom: 4px;
        }
        .active-badge {
            background-color: #e8f7ea;
            color: #2f9e44;
            border-radius: 12px;
            padding: 4px 10px;
            font-size: 13px;
            font-weight: 600;
        }
                .athlete-photo-circle {
            width: 110px !important;
            height: 110px !important;
            border-radius: 50% !important;
            object-fit: cover !important;
            object-position: center !important;

            /* Clean circle — no gray/white frame */
            border: none !important;
            outline: none !important;
            box-shadow: none !important;
            background: transparent !important;

            display: block !important;
            padding: 0 !important;
            margin: 0 !important;
        }
        .pb-event { color: #6b7280; font-size: 14px; margin-bottom: 4px; }
        .pb-time { color: #111827; font-size: 27px; font-weight: 750; margin-bottom: 5px; }
        .status-dot { color: #2f9e44; font-size: 12px; }
        .notes-title {
            font-size: 28px;
            font-weight: 760;
            color: #111827;
            margin-bottom: 2px;
        }
        .notes-subtitle {
            color: #6b7280;
            font-size: 14px;
            margin-bottom: 12px;
        }
        .note-card {
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            padding: 15px 16px;
            margin-bottom: 12px;
        }
        .note-card-athlete {
            background: #f5f9ff;
            border-color: #dbeafe;
        }
        .note-card-coach {
            background: #f3fbf4;
            border-color: #d7efd9;
        }
        .note-
.note-author-wrap {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .note-author {
            color: #111827;
            font-size: 14px;
            font-weight: 700;
        }
        .note-role {
            display: inline-block;
            border-radius: 999px;
            padding: 2px 8px;
            font-size: 10px;
            font-weight: 800;
            letter-spacing: .25px;
        }
        .note-role-athlete {
            color: #2563eb;
            background: #e8f1ff;
        }
        .note-role-coach {
            color: #24833b;
            background: #e4f6e7;
        }
        .note-time {
            color: #6b7280;
            font-size: 12px;
            white-space: nowrap;
        }
        .note-body {
            color: #1f2937;
            font-size: 15px;
            line-height: 1.55;
            white-space: pre-wrap;
        }

        .team-workout-title {
            font-size: 28px;
            font-weight: 760;
            color: #111827;
            margin-top: 10px;
            margin-bottom: 2px;
        }

        .team-workout-subtitle {
            color: #6b7280;
            font-size: 14px;
            margin-bottom: 12px;
        }

        .team-workout-card {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            padding: 16px;
            min-height: 245px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.03);
        }

        .team-workout-date {
            color: #6b7280;
            font-size: 12px;
            font-weight: 650;
            margin-bottom: 6px;
        }

        .team-workout-type {
            color: #111827;
            font-size: 18px;
            font-weight: 760;
            margin-bottom: 13px;
        }

        .team-workout-detail {
            display: flex;
            flex-direction: column;
            gap: 2px;
            color: #374151;
            font-size: 13px;
            line-height: 1.4;
            margin-bottom: 10px;
        }

        .team-workout-label {
            color: #2f9e44;
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: .3px;
        }

        .team-workout-notes {
            border-top: 1px solid #eef0ee;
            margin-top: 12px;
            padding-top: 10px;
            color: #6b7280;
            font-size: 12px;
            line-height: 1.45;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# SIDEBAR AND ATHLETE SELECTION
# =========================================================

with st.sidebar:

    st.markdown(
        "## VEK<span style='color:#2f9e44'>DYN</span>",
        unsafe_allow_html=True
    )

    st.success("▣ Dashboard")

    st.divider()

    st.markdown("### Choose Athlete")

    athlete_key = st.selectbox(
        "",
        options=list(athletes.keys()),
        format_func=lambda key: athletes[key]["profile"]["name"],
    )

    st.divider()

    st.markdown("### Athlete Overview")

    st.write("♙ Profile")
    st.write("♨ Training")
    st.write("↗ Performance")
    st.write("♡ Recovery")
    st.write("📝 Notes")

    # -----------------------------------------------------
    # CONTACT / FEEDBACK
    # -----------------------------------------------------

    st.divider()

    st.markdown("### VEKDYN")

    if st.button(
        "✉ Contact & Feedback",
        key="contact_feedback_button",
        use_container_width=True,
    ):
        st.session_state["show_contact_form"] = (
            not st.session_state.get("show_contact_form", False)
        )

    if st.session_state.get("show_contact_form", False):
        st.caption(
            "Questions, feedback, or interested in bringing "
            "VEKDYN to your program?"
        )

        with st.form(
            "vek_dyn_contact_form",
            clear_on_submit=True,
        ):
            contact_name = st.text_input(
                "Name",
                placeholder="Your name",
            )

            contact_program = st.text_input(
                "School / Program",
                placeholder="School or running program",
            )

            contact_email = st.text_input(
                "Email",
                placeholder="name@email.com",
            )

            contact_message = st.text_area(
                "Message",
                placeholder=(
                    "Tell us what you're interested in, "
                    "share feedback, or report an issue."
                ),
                height=130,
            )

            contact_submit = st.form_submit_button(
                "Send Message",
                type="primary",
                use_container_width=True,
            )

        if contact_submit:
            if not contact_name.strip():
                st.warning("Please enter your name.")
            elif not contact_email.strip():
                st.warning("Please enter your email.")
            elif not contact_message.strip():
                st.warning("Please enter a message.")
            else:
                st.success("Thanks — your message is ready to send.")

    # -----------------------------------------------------
    # ACCOUNT
    # -----------------------------------------------------

    st.divider()

    st.caption("Coach")
    st.caption(active_team_config["name"])

    if st.button(
        "Log Out",
        key="logout_button",
        use_container_width=True,
    ):
        log_out()

athlete = athletes[athlete_key]
profile = athlete["profile"]
personal_bests = athlete.get("pbs", {})
xc_results = athlete.get("xc_results", {})
training = athlete.get("training", {})
recovery = athlete.get("recovery", training.get("recovery", {}))
threshold_lactate = athlete.get(
    "threshold_lactate",
    training.get("threshold_lactate", {}),
)


with st.sidebar:
    athlete_name_for_button = profile.get("name", "Athlete")
    weekly_session_key = f"{athlete_key}_strava_weekly"
    heart_session_key = f"{athlete_key}_strava_heart_rate"
    message_session_key = f"strava_message_{athlete_key}"
    error_session_key = f"strava_error_{athlete_key}"

    if strava_is_connected(athlete_key):
        if st.button(
            f"Sync {athlete_name_for_button.split()[0]}'s Strava",
            use_container_width=True,
        ):
            try:
                token = get_valid_strava_token(athlete_key)
                weekly, heart_rate = get_strava_training_data(
                    token,
                    number_of_weeks=8,
                )
                st.session_state[weekly_session_key] = weekly
                st.session_state[heart_session_key] = heart_rate
                st.session_state[message_session_key] = (
                    f"{athlete_name_for_button}'s Strava sync is complete."
                )
                st.session_state.pop(error_session_key, None)
            except (
                requests.RequestException,
                sqlite3.DatabaseError,
                RuntimeError,
                KeyError,
            ) as error:
                st.session_state[error_session_key] = str(error)

        connection = athlete_strava_connection(athlete_key)
        connected_strava_name = connection.get("strava_name")
        if connected_strava_name:
            st.caption(f"Connected Strava account: {connected_strava_name}")
        else:
            st.caption("Strava connection configured")

        # Always offer a fresh OAuth connection. This lets an athlete replace
        # an expired saved token or correct an account connected by mistake.
        reconnect_url = create_strava_login_url(athlete_key)
        if reconnect_url:
            st.link_button(
                f"Reconnect {athlete_name_for_button.split()[0]}'s Strava",
                reconnect_url,
                use_container_width=True,
            )
    else:
        login_url = create_strava_login_url(athlete_key)
        if login_url:
            st.link_button(
                f"Connect {athlete_name_for_button.split()[0]}'s Strava",
                login_url,
                use_container_width=True,
            )
        else:
            st.warning("Add the Strava Client ID to secrets.toml first.")

    if st.session_state.get(message_session_key):
        st.success(st.session_state[message_session_key])
    elif st.session_state.get(error_session_key):
        if strava_is_connected(athlete_key):
            st.warning(
                "Strava sync failed. No live Strava mileage is available right now. "
                f"Details: {st.session_state[error_session_key]}"
            )
        else:
            st.warning(st.session_state[error_session_key])
    elif not strava_is_connected(athlete_key):
        st.caption("This athlete has not connected Strava yet.")

    st.write("")
    st.caption("Coach")
    st.caption(active_team_config["name"])


# =========================================================
# CHOOSE THE TRAINING DATA SOURCE
# =========================================================

dictionary_volume = dictionary_weekly_mileage(training)
volume_data = dictionary_volume
volume_source = "No live data"

weekly_session_key = f"{athlete_key}_strava_weekly"
heart_session_key = f"{athlete_key}_strava_heart_rate"

if weekly_session_key in st.session_state:
    strava_volume = st.session_state[weekly_session_key]
    if not strava_volume.empty:
        volume_data = strava_volume
        volume_source = "Live Strava data"

live_heart_rate = st.session_state.get(heart_session_key, {})

# =========================================================
# ATHLETE PROFILE PHOTOS
# =========================================================

ATHLETE_PHOTO_DIR = Path(__file__).parent / "athlete_photos"


def get_athlete_photo(athlete_key):
    """
    Find an athlete's profile photo inside athlete_photos.

    Example:
        athlete_photos/diego.jpeg
        athlete_photos/isai.jpeg
    """

    clean_key = athlete_key.strip().lower()

    for extension in [".jpeg", ".jpg", ".png"]:
        photo_path = ATHLETE_PHOTO_DIR / f"{clean_key}{extension}"

        if photo_path.exists():
            return photo_path

    return None


def render_circular_athlete_photo(photo_path, alt_text="Athlete profile photo"):
    """Render a local athlete image as a true circle without changing the file."""
    mime = "image/png" if photo_path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(photo_path.read_bytes()).decode("utf-8")
    st.markdown(
        f'<img class="athlete-photo-circle" src="data:{mime};base64,{encoded}" alt="{alt_text}">',
        unsafe_allow_html=True,
    )


# =========================================================
# PROFILE DATA
# =========================================================

athlete_name = profile.get("name", "Unknown Athlete")
school = profile.get("school", "School not available")
athlete_class = profile.get("class", "Class not available")

initials = "".join(
    word[0] for word in athlete_name.split()[:2]
).upper()

athlete_photo = get_athlete_photo(athlete_key)


with st.container(border=True):

    photo_col, athlete_col, school_col = st.columns([1, 4, 1.5])

    # -----------------------------------------------------
    # ATHLETE PHOTO
    # -----------------------------------------------------

    with photo_col:

        if athlete_photo is not None:

            render_circular_athlete_photo(
                athlete_photo,
                alt_text=f"{athlete_name} profile photo",
            )

        else:

            st.markdown(
                f'<div class="profile-circle">{initials}</div>',
                unsafe_allow_html=True
            )

    # -----------------------------------------------------
    # ATHLETE INFORMATION
    # -----------------------------------------------------

    with athlete_col:

        st.markdown(
            f'<span class="athlete-name">{athlete_name}</span>&nbsp;&nbsp;'
            '<span class="active-badge">Active</span>',
            unsafe_allow_html=True,
        )

        st.write(
            f"**{athlete_class}**  •  🟢 Distance"
        )

        st.caption(
            "5'9\"  •  150 lbs  •  San Antonio, TX"
        )

    # -----------------------------------------------------
    # SCHOOL LOGO
    # -----------------------------------------------------

    with school_col:

        school_logo = get_team_image("ollu_distance")

        if school_logo is not None:

            # Make the school mark fill most of the athlete-header height.
            # For a truly background-free logo, use a transparent PNG
            # in team_images; CSS cannot remove white pixels baked into
            # the source image.
            st.markdown(
                """
                <style>
                    div[data-testid="stImage"] img {
                        max-height: 145px;
                        width: auto;
                        object-fit: contain;
                    }
                </style>
                """,
                unsafe_allow_html=True,
            )

            st.image(
                str(school_logo),
                use_container_width=True,
            )

        else:

            st.markdown(
                f"""
                <div style="
                    font-size:28px;
                    font-weight:800;
                    color:#111827;
                    text-align:center;
                ">
                    {school}
                </div>
                """,
                unsafe_allow_html=True,
            )

# =========================================================
# PERFORMANCE BESTS AND DATA SOURCE
# =========================================================

pb_card, source_card = st.columns([2, 1])

with pb_card:
    with st.container(border=True):
        st.subheader("Performance Bests")
        performance_view = st.radio(
            "Performance type",
            options=["Track", "Cross Country"],
            horizontal=True,
            key=f"performance_view_{athlete_key}",
            label_visibility="collapsed",
        )

        if performance_view == "Track":
            if personal_bests:
                pb_columns = st.columns(len(personal_bests))
                for column, event in zip(pb_columns, personal_bests):
                    with column:
                        st.markdown(
                            f"""
                            <div class="pb-event">{event.upper()}</div>
                            <div class="pb-time">{personal_bests[event]}</div>
                            <div class="green-text">Watch Race ↗</div>
                            """,
                            unsafe_allow_html=True,
                        )
            else:
                st.info("No track performance bests have been entered.")

        else:
            xc_columns = st.columns(2)
            for column, event in zip(xc_columns, ["8k", "10k"]):
                result = primary_xc_result(xc_results, event)
                result_details = result["meet"]
                if result["date"]:
                    result_details = f'{result_details} • {result["date"]}'

                with column:
                    st.markdown(
                        f"""
                        <div class="pb-event">{event.upper()} XC</div>
                        <div class="pb-time">{result["time"]}</div>
                        <div class="small-text">{result_details}</div>
                        """,
                        unsafe_allow_html=True,
                    )

            all_xc_rows = []
            for event in ["8k", "10k"]:
                for result in get_xc_results_for_event(xc_results, event):
                    all_xc_rows.append(
                        {
                            "Event": event.upper(),
                            "Time": result.get("time", "--"),
                            "Meet": result.get("meet", ""),
                            "Date": result.get("date", ""),
                        }
                    )

            if all_xc_rows:
                with st.expander("View all XC results"):
                    st.dataframe(
                        pd.DataFrame(all_xc_rows),
                        hide_index=True,
                        use_container_width=True,
                    )
            else:
                st.caption("No XC 8K or 10K PB is entered in ollu_roster_csv.")

with source_card:
    with st.container(border=True):
        st.subheader("Heart Rate Benchmark")
        max_observed_hr = live_heart_rate.get(
            "max_heart_rate",
            recovery.get("max_observed_heart_rate", "--"),
        )
        max_hr_date = live_heart_rate.get(
            "max_heart_rate_date",
            recovery.get("max_heart_rate_date", "Not available"),
        )
        st.metric("8-Week Max Observed HR", f"{max_observed_hr} bpm")
        st.caption(f"Recorded: {max_hr_date}")
        st.caption(f"Training source: {volume_source}")
        if live_heart_rate:
            st.markdown("<span class='green-text'>● Strava</span>", unsafe_allow_html=True)
        elif strava_is_connected(athlete_key):
            st.caption("Sync an HR-enabled Strava run to update this benchmark.")


# =========================================================
# WEEKLY TRAINING VOLUME + HEART RATE & RECOVERY
# =========================================================

volume_card, recovery_card = st.columns([2, 1])

with volume_card:
    with st.container(border=True):
        st.subheader("Weekly Training Volume")

        if volume_data.empty:
            st.info("No weekly training data is available.")
        else:
            current_volume = float(volume_data["Mileage"].iloc[-1])
            previous_volume = (
                float(volume_data["Mileage"].iloc[-2])
                if len(volume_data) > 1
                else current_volume
            )
            volume_change = current_volume - previous_volume

            number_col, graph_col = st.columns([1, 3])

            with number_col:
                st.metric(
                    label="This Week",
                    value=f"{current_volume:.1f} mi",
                    delta=f"{volume_change:+.1f} mi",
                )
                st.caption(f"Source: {volume_source}")

                if volume_source == "Live Strava data":
                    st.markdown(
                        "<span class='green-text'>● Strava</span>",
                        unsafe_allow_html=True,
                    )

            with graph_col:
                volume_chart = (
                    alt.Chart(volume_data)
                    .mark_area(
                        line={"color": "#35a33b", "strokeWidth": 3},
                        color=alt.Gradient(
                            gradient="linear",
                            stops=[
                                alt.GradientStop(color="#dff3e1", offset=0),
                                alt.GradientStop(color="#ffffff", offset=1),
                            ],
                            x1=1,
                            x2=1,
                            y1=1,
                            y2=0,
                        ),
                        point={"filled": True, "fill": "#35a33b", "size": 80},
                    )
                    .encode(
                        x=alt.X(
                            "Week:N",
                            sort=None,
                            axis=alt.Axis(title=None, labelAngle=0),
                        ),
                        y=alt.Y(
                            "Mileage:Q",
                            title="Miles",
                            scale=alt.Scale(zero=True),
                        ),
                        tooltip=[
                            alt.Tooltip("Week:N"),
                            alt.Tooltip("Mileage:Q", title="Miles", format=".1f"),
                        ],
                    )
                    .properties(height=240)
                )
                st.altair_chart(volume_chart, use_container_width=True)

with recovery_card:
    with st.container(border=True):
        st.subheader("Heart Rate & Recovery")

        heart_updated = live_heart_rate.get(
            "last_updated",
            recovery.get("last_updated", "Not available"),
        )
        st.caption(f"Last updated: {heart_updated}")

        sleep_left, sleep_right = st.columns(2)

        with sleep_left:
            sleeping_hr = recovery.get(
                "sleep_heart_rate",
                recovery.get("resting_hr", "--"),
            )
            st.metric("Sleeping HR", f"{sleeping_hr} bpm")
            st.caption("COROS connection pending")

        with sleep_right:
            st.metric(
                "Sleep Time",
                f"{recovery.get('sleep_hours', '--')}h {recovery.get('sleep_minutes', '--')}m",
            )
            st.markdown(
                "<span class='status-dot'>● Good</span>",
                unsafe_allow_html=True,
            )

        st.divider()

        recovery_left, recovery_right = st.columns(2)

        with recovery_left:
            st.metric(
                "Average HRV",
                f"{recovery.get('average_hrv', '--')} ms",
            )
            st.markdown(
                "<span class='status-dot'>● Optimal</span>",
                unsafe_allow_html=True,
            )

        with recovery_right:
            st.metric(
                "Recovery Score",
                f"{recovery.get('recovery_score', '--')}%",
            )

# =========================================================
# ATHLETE + COACH NOTES
# =========================================================

st.write("")

notes_feed_col, notes_compose_col = st.columns([1.75, 1], gap="large")

with notes_feed_col:
    st.markdown(
        '<div class="notes-title">Notes</div>'
        '<div class="notes-subtitle">Athlete feedback and coach responses in one timeline.</div>',
        unsafe_allow_html=True,
    )

    note_filter_label = st.selectbox(
        "Note filter",
        options=["All Notes", "Athlete", "Coach"],
        key=f"note_filter_{athlete_key}",
        label_visibility="collapsed",
    )
    role_filter = {
        "All Notes": None,
        "Athlete": "ATHLETE",
        "Coach": "COACH",
    }[note_filter_label]

    try:
        notes = load_athlete_notes(
            athlete_key,
            limit=40,
            role_filter=role_filter,
        )
    except psycopg2.Error as error:
        notes = []
        st.error(f"Notes could not be loaded from Neon: {error}")

    if notes:
        for note in notes:
            role = note.get("author_role", "ATHLETE").upper()
            role_class = "coach" if role == "COACH" else "athlete"
            safe_author = html.escape(str(note.get("author_name", "")))
            safe_role = html.escape(role)
            safe_time = html.escape(format_note_timestamp(note.get("created_at")))
            safe_text = html.escape(str(note.get("note_text", ""))).replace("\n", "<br>")

            note_html = (
                f'<div class="note-card note-card-{role_class}">'
                '<div class="note-header">'
                '<div class="note-author-wrap">'
                f'<span class="note-author">{safe_author}</span>'
                f'<span class="note-role note-role-{role_class}">{safe_role}</span>'
                '</div>'
                f'<span class="note-time">{safe_time}</span>'
                '</div>'
                f'<div class="note-body">{safe_text}</div>'
                '</div>'
            )
            st.markdown(note_html, unsafe_allow_html=True)
    else:
        st.info("No notes yet. Add the first training update for this athlete.")

with notes_compose_col:
    with st.container(border=True):
        st.markdown("### Add a note")
        st.caption("Share how training went or leave a coaching response.")

        with st.form(key=f"shared_note_form_{athlete_key}", clear_on_submit=True):
            posting_as = st.selectbox(
                "Posting as",
                options=["Athlete", "Coach"],
                key=f"note_posting_as_{athlete_key}",
            )

            if posting_as == "Athlete":
                note_author_name = athlete_name
                note_author_role = "ATHLETE"
                st.caption(f"Posting as {athlete_name}")
            else:
                note_author_name = "Coach Zarate"
                note_author_role = "COACH"
                st.caption("Posting as Coach Zarate")

            new_note_text = st.text_area(
                "Note",
                height=180,
                placeholder=(
                    "How did training go? Include effort, soreness, sleep, "
                    "lactate, mechanics, recovery, or anything the coach should know."
                ),
                label_visibility="collapsed",
            )

            save_shared_note = st.form_submit_button(
                "Save Note",
                type="primary",
                use_container_width=True,
            )

        if save_shared_note:
            try:
                save_athlete_note(
                    athlete_key=athlete_key,
                    author_name=note_author_name,
                    author_role=note_author_role,
                    note_text=new_note_text,
                )
                st.success("Note added.")
                st.rerun()
            except ValueError as error:
                st.warning(str(error))
            except psycopg2.Error as error:
                st.error(f"The note could not be saved to Neon: {error}")


# =========================================================
# PERFORMANCE PREDICTIONS
# =========================================================

prediction = athlete.get("prediction", {})

with st.container(border=True):

    st.subheader("Performance Predictions")

    st.caption(
        "Based on current fitness, training load, recovery, "
        "threshold profile, and historical performances"
    )

    prediction_col, confidence_col, factors_col = st.columns([2,1,2])


    with prediction_col:

        st.caption("Predicted 1500m Range")

        predicted_time = prediction.get(
            "1500m",
            {}
        ).get(
            "range",
            "--"
        )

        st.markdown(
            f"## {predicted_time}"
        )

        st.caption(
            "Prediction updates as training data changes."
        )


    with confidence_col:

        confidence = prediction.get(
            "1500m",
            {}
        ).get(
            "confidence",
            "--"
        )

        st.metric(
            "Confidence",
            f"{confidence}%"
        )

        st.caption(
            "High Confidence"
        )


    with factors_col:

        st.write("**Performance Factors**")

        factors = prediction.get(
            "factors",
            {}
        )

        st.progress(
            factors.get("aerobic_fitness",0)/100,
            text="Aerobic Fitness"
        )

        st.progress(
            factors.get("threshold_fitness",0)/100,
            text="Threshold Fitness"
        )

        st.progress(
            factors.get("speed_reserve", 0) / 100,
            text="Speed Reserve"
        )


# =========================================================
# TEAM WORKOUTS — EXCEL IMPORT
# =========================================================

TEAM_WORKOUT_COLUMNS = [
    "Date",
    "Type",
    "Warm Up",
    "Workout",
    "Cool Down",
    "Notes",
]


def load_team_training_plan(uploaded_file):
    """
    Read one team-wide Excel training plan.

    Expected columns:
        Date | Type | Warm Up | Workout | Cool Down | Notes

    The plan belongs to the whole team, so there is intentionally
    no athlete column.
    """
    try:
        plan = pd.read_excel(uploaded_file)
    except Exception as error:
        raise ValueError(f"VEKDYN could not read that Excel file: {error}") from error

    # Normalize accidental leading/trailing spaces in Excel headers.
    plan.columns = [str(column).strip() for column in plan.columns]

    missing_columns = [
        column
        for column in TEAM_WORKOUT_COLUMNS
        if column not in plan.columns
    ]

    if missing_columns:
        raise ValueError(
            "The training plan is missing these columns: "
            + ", ".join(missing_columns)
        )

    plan = plan[TEAM_WORKOUT_COLUMNS].copy()

    plan["Date"] = pd.to_datetime(
        plan["Date"],
        errors="coerce",
    )

    plan = plan.dropna(subset=["Date"])

    for column in ["Type", "Warm Up", "Workout", "Cool Down", "Notes"]:
        plan[column] = (
            plan[column]
            .fillna("")
            .astype(str)
            .str.strip()
        )

    # A workout needs at least the main Workout field.
    plan = plan[
        plan["Workout"].str.len() > 0
    ].copy()

    plan = plan.sort_values("Date").reset_index(drop=True)

    if plan.empty:
        raise ValueError(
            "No valid workouts were found. Make sure Date and Workout are filled in."
        )

    return plan


def workout_value(value, fallback="—"):
    """Return a clean display value for workout-card text."""
    value = str(value).strip()

    if not value or value.lower() == "nan":
        return fallback

    return value


def render_team_workout_card(workout):
    """Render one workout using the clean VEKDYN card style."""
    workout_date = pd.Timestamp(workout["Date"])
    date_label = workout_date.strftime("%a, %b %d")

    workout_type = html.escape(
        workout_value(workout.get("Type"), "Team Training")
    )
    warm_up = html.escape(
        workout_value(workout.get("Warm Up"))
    )
    main_workout = html.escape(
        workout_value(workout.get("Workout"))
    )
    cool_down = html.escape(
        workout_value(workout.get("Cool Down"))
    )
    notes = html.escape(
        workout_value(workout.get("Notes"), "")
    )

    details_html = (
        f'<div class="team-workout-detail">'
        f'<span class="team-workout-label">Warm-up</span>'
        f'<span>{warm_up}</span>'
        f'</div>'
        f'<div class="team-workout-detail">'
        f'<span class="team-workout-label">Workout</span>'
        f'<span>{main_workout}</span>'
        f'</div>'
        f'<div class="team-workout-detail">'
        f'<span class="team-workout-label">Cool-down</span>'
        f'<span>{cool_down}</span>'
        f'</div>'
    )

    notes_html = ""
    if notes:
        notes_html = (
            f'<div class="team-workout-notes">'
            f'<span class="team-workout-label">Coach notes</span><br>'
            f'{notes}'
            f'</div>'
        )

    st.markdown(
        f"""
        <div class="team-workout-card">
            <div class="team-workout-date">{date_label}</div>
            <div class="team-workout-type">{workout_type}</div>
            {details_html}
            {notes_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_team_workouts():
    """
    Import and display the shared OLLU team plan.

    This first version keeps the imported workbook in Streamlit session state.
    The next persistence step can save the same rows to Neon by team_id.
    """
    st.markdown(
        '<div class="team-workout-title">Upcoming Workouts</div>'
        '<div class="team-workout-subtitle">'
        'Team-wide training assigned by the coach.'
        '</div>',
        unsafe_allow_html=True,
    )

    # Coach import control stays compact so the workout cards remain the focus.
    with st.expander("Import team training plan (.xlsx)", expanded=False):
        st.caption(
            "Excel columns: Date, Type, Warm Up, Workout, Cool Down, Notes. "
            "The same plan is shown for the entire roster."
        )

        uploaded_plan = st.file_uploader(
            "Upload Excel training plan",
            type=["xlsx"],
            key="team_training_plan_upload",
            label_visibility="collapsed",
        )

        if uploaded_plan is not None:
            try:
                preview_plan = load_team_training_plan(uploaded_plan)

                st.dataframe(
                    preview_plan,
                    hide_index=True,
                    use_container_width=True,
                )

                if st.button(
                    "Import Workouts",
                    key="import_team_workouts_button",
                    type="primary",
                    use_container_width=True,
                ):
                    plan_key = f"team_training_plan_{active_team}"
                    plan_name_key = f"team_training_plan_name_{active_team}"
                    st.session_state[plan_key] = preview_plan
                    st.session_state[plan_name_key] = uploaded_plan.name
                    st.success(
                        f"{len(preview_plan)} team workouts imported into VEKDYN."
                    )
                    st.rerun()

            except ValueError as error:
                st.warning(str(error))

    plan_key = f"team_training_plan_{active_team}"
    training_plan = st.session_state.get(plan_key)

    if training_plan is None or training_plan.empty:
        st.info(
            "No team training plan has been imported yet. "
            "Upload the coach's Excel plan to populate this section."
        )
        return

    today = pd.Timestamp.now(tz=TEAM_TIMEZONE).tz_localize(None).normalize()

    upcoming = training_plan[
        training_plan["Date"].dt.normalize() >= today
    ].copy()

    # If the plan has no future dates, show the most recent workouts instead
    # so the section never looks broken during a demo.
    if upcoming.empty:
        upcoming = training_plan.tail(4).copy()
        st.caption("No future workouts are entered. Showing the most recent sessions.")
    else:
        upcoming = upcoming.head(4)

    source_name = st.session_state.get(
        f"team_training_plan_name_{active_team}"
    )
    if source_name:
        st.caption(f"Training plan source: {source_name}")

    workout_columns = st.columns(len(upcoming), gap="medium")

    for column, (_, workout) in zip(
        workout_columns,
        upcoming.iterrows(),
    ):
        with column:
            render_team_workout_card(workout)


render_team_workouts()


# =========================================================
# THRESHOLD LACTATE PROFILE
# =========================================================

st.subheader("Threshold Lactate Profile")

threshold = athlete.get("threshold", {})

with st.container(border=True):

    short_col, medium_col, long_col = st.columns(3)

    with short_col:
        st.metric(
            "Short Reps (1-3 min)",
            f"{threshold.get('short_reps', {}).get('lactate', '--')} mmol"
        )
        st.caption(
            threshold.get('short_reps', {}).get('pace', '--')
        )

    with medium_col:
        st.metric(
            "Medium Reps (5-10 min)",
            f"{threshold.get('medium_reps', {}).get('lactate', '--')} mmol"
        )
        st.caption(
            threshold.get('medium_reps', {}).get('pace', '--')
        )

    with long_col:
        st.metric(
            "Long Reps (10-15 min)",
            f"{threshold.get('long_reps', {}).get('lactate', '--')} mmol"
        )
        st.caption(
            threshold.get('long_reps', {}).get('pace', '--')
        )