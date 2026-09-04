import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import string
import bcrypt

import altair as alt
import pandas as pd
import psycopg2
import requests
import streamlit as st

# =================================================
# TEAM ROSTERS — CSV FILES ARE THE SOURCE OF TRUTH
# =================================================

OLLU_ROSTER_PATH = Path(__file__).with_name("ollu_roster_csv")
SAM_HOUSTON_ROSTER_PATH = Path(__file__).with_name("sam_houston_csv")
DARK_HORSE_ROSTER_PATH = Path(__file__).with_name("dark_horse_endurance_csv")

TEAM_IMAGES_DIR = Path(__file__).with_name("team_images")
TEAM_LOGOS_DIR = Path(__file__).with_name("team_logos")


def get_team_logo(team_id):
    """Return the same team-specific school image used on the landing card."""
    return get_team_image(team_id)


def get_team_image(image_id):
    """Return a VEKDYN-controlled image from team_images by its unique ID."""
    for extension in (".png", ".jpg", ".jpeg", ".webp"):
        image_path = TEAM_IMAGES_DIR / f"{image_id}{extension}"
        if image_path.exists():
            return image_path
    return None


def clean_csv_value(value, default=""):
    """Turn blank/NaN CSV values into safe VEKDYN values."""
    if pd.isna(value):
        return default
    value = str(value).strip()
    return default if value.lower() == "nan" else value


def make_athlete_id(name):
    """Create a stable simple athlete ID when a CSV does not already provide one."""
    value = clean_csv_value(name).lower()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value


def load_team_roster(roster_path, default_school="", default_team="Distance"):
    """
    Load either roster format currently used by VEKDYN.

    OLLU format:
        athlete_id, first_name, last_name, school, team, class_year, ...

    Sam Houston format:
        name, sex, class_year, pb_800, pb_1500, pb_mile, pb_3k, pb_5k, pb_8k

    Both formats are converted into the same athlete dictionary so the
    dashboard can render each school separately with the same UI.
    """
    if not roster_path.exists():
        raise FileNotFoundError(f"Roster file not found: {roster_path}")

    roster = pd.read_csv(
        roster_path,
        dtype=str,
        keep_default_na=False,
    ).fillna("")

    # Normalize column names in case whitespace was accidentally added.
    roster.columns = [str(column).strip() for column in roster.columns]

    # -------------------------------------------------
    # NORMALIZE NAME / ID FIELDS
    # -------------------------------------------------
    if "athlete_id" not in roster.columns:
        if "name" not in roster.columns:
            raise RuntimeError(
                f"{roster_path.name} must contain either athlete_id/first_name/last_name "
                "or a name column."
            )

        roster["name"] = roster["name"].astype(str).str.strip()
        roster["athlete_id"] = roster["name"].apply(make_athlete_id)

        split_names = roster["name"].str.split()
        roster["first_name"] = split_names.str[0].fillna("")
        roster["last_name"] = split_names.apply(
            lambda parts: " ".join(parts[1:]) if isinstance(parts, list) and len(parts) > 1 else ""
        )
    else:
        if "first_name" not in roster.columns:
            roster["first_name"] = ""
        if "last_name" not in roster.columns:
            roster["last_name"] = ""

    # Supply team fields for the compact Sam Houston CSV.
    if "school" not in roster.columns:
        roster["school"] = default_school

    if "team" not in roster.columns:
        roster["team"] = default_team

    if "class_year" not in roster.columns:
        roster["class_year"] = "FR"

    roster["athlete_id"] = roster["athlete_id"].astype(str).str.strip()

    # Ignore blank IDs when checking duplicates.
    nonblank_ids = roster.loc[
        roster["athlete_id"] != "",
        "athlete_id",
    ]

    duplicate_ids = nonblank_ids[
        nonblank_ids.duplicated(keep=False)
    ].tolist()

    if duplicate_ids:
        raise RuntimeError(
            f"{roster_path.name} has duplicate athlete_id values: "
            + ", ".join(sorted(set(duplicate_ids)))
        )

    athletes = {}

    # -------------------------------------------------
    # BUILD ATHLETE DICTIONARY
    # -------------------------------------------------
    for _, row in roster.iterrows():
        athlete_id = clean_csv_value(row.get("athlete_id"))
        if not athlete_id:
            continue

        first_name = clean_csv_value(row.get("first_name"))
        last_name = clean_csv_value(row.get("last_name"))
        full_name = f"{first_name} {last_name}".strip()

        if not full_name:
            full_name = clean_csv_value(row.get("name"), athlete_id)

        # ---------------------------------------------
        # TRACK PERSONAL BESTS
        # ---------------------------------------------
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

        # ---------------------------------------------
        # CROSS COUNTRY RESULTS
        # ---------------------------------------------
        xc_results = {}

        # OLLU uses xc_8k_pb; the compact Sam Houston CSV uses pb_8k.
        xc_8k = clean_csv_value(
            row.get("xc_8k_pb"),
            clean_csv_value(row.get("pb_8k")),
        )

        xc_10k = clean_csv_value(row.get("xc_10k_pb"))

        # Future women's 6K support if pb_6k or xc_6k_pb is added.
        xc_6k = clean_csv_value(
            row.get("xc_6k_pb"),
            clean_csv_value(row.get("pb_6k")),
        )

        if xc_6k:
            xc_results["6k"] = [
                {"time": xc_6k, "meet": "XC Personal Best", "date": ""}
            ]

        if xc_8k:
            xc_results["8k"] = [
                {"time": xc_8k, "meet": "XC Personal Best", "date": ""}
            ]

        if xc_10k:
            xc_results["10k"] = [
                {"time": xc_10k, "meet": "XC Personal Best", "date": ""}
            ]

        # ---------------------------------------------
        # THRESHOLD DATA
        # ---------------------------------------------
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
                "school": clean_csv_value(row.get("school"), default_school),
                "team": clean_csv_value(row.get("team"), default_team),
                "class": clean_csv_value(row.get("class_year"), "FR"),
                "sex": clean_csv_value(row.get("sex")),
            },
            "pbs": pbs,
            "xc_results": xc_results,
            "threshold": threshold,

            # Live training/HR is intentionally not stored in the roster CSV.
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
        raise RuntimeError(
            f"No athletes were loaded from {roster_path.name}."
        )

    return roster, athletes


# =========================================================
# LOAD EACH TEAM SEPARATELY
# =========================================================

ollu_roster, ollu_athletes = load_team_roster(
    OLLU_ROSTER_PATH,
    default_school="OLLU",
    default_team="Distance",
)

sam_houston_roster, sam_houston_athletes = load_team_roster(
    SAM_HOUSTON_ROSTER_PATH,
    default_school="Sam Houston",
    default_team="Distance",
)

# Dark Horse can be deployed before the roster is populated. Replace the
# header-only template with the real CSV when the coach is ready.
try:
    dark_horse_roster, dark_horse_athletes = load_team_roster(
        DARK_HORSE_ROSTER_PATH,
        default_school="Dark Horse Endurance",
        default_team="Endurance",
    )
except (FileNotFoundError, RuntimeError):
    dark_horse_roster = pd.DataFrame()
    dark_horse_athletes = {}


# =========================================================
# TEAM ROUTING
# =========================================================

def get_team_athletes(team_id):
    """Return only the roster belonging to the selected VEKDYN team."""
    if team_id == "ollu_distance":
        return ollu_athletes

    if team_id == "sam_houston":
        return sam_houston_athletes

    if team_id == "dark_horse_endurance":
        return dark_horse_athletes

    return {}


# =========================================================
# GLOBAL ATHLETE LOOKUP — USED BY STRAVA OAUTH
# =========================================================
# The dashboard still swaps `athletes` to the active team's roster later.
# Strava OAuth, however, returns to a fresh Streamlit run before that team
# routing happens. Therefore OAuth needs a lookup containing BOTH schools.

all_athletes = {
    **ollu_athletes,
    **sam_houston_athletes,
    **dark_horse_athletes,
}

# Map every athlete key back to the team that owns that profile.
athlete_team_lookup = {
    **{athlete_key: "ollu_distance" for athlete_key in ollu_athletes},
    **{athlete_key: "sam_houston" for athlete_key in sam_houston_athletes},
    **{athlete_key: "dark_horse_endurance" for athlete_key in dark_horse_athletes},
}

# Fail early if the same athlete_id exists in both schools. Athlete keys are
# also the primary keys used by Neon for Strava connections, so they must be
# globally unique across VEKDYN.
team_roster_ids = {
    "OLLU": set(ollu_athletes),
    "Sam Houston": set(sam_houston_athletes),
    "Dark Horse Endurance": set(dark_horse_athletes),
}
duplicate_cross_team_ids = set()
team_id_sets = list(team_roster_ids.values())
for left_index in range(len(team_id_sets)):
    for right_index in range(left_index + 1, len(team_id_sets)):
        duplicate_cross_team_ids.update(
            team_id_sets[left_index].intersection(team_id_sets[right_index])
        )
if duplicate_cross_team_ids:
    raise RuntimeError(
        "These athlete_id values are duplicated across VEKDYN teams: "
        + ", ".join(sorted(duplicate_cross_team_ids))
        + ". Give each athlete a globally unique athlete_id before using Strava."
    )

# Used by the public landing page before a private workspace is selected.
# The private dashboard replaces this with get_team_athletes(active_team).
athletes = ollu_athletes

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
    "dark_horse_endurance": {
        "name": "Dark Horse Endurance",
        "short_name": "Dark Horse",
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
        token_parts = str(token).rsplit(":", 3)
        if len(token_parts) != 4:
            return None

        team_id, username, expires_at, returned_signature = token_parts
        expires_at = int(expires_at)
    except (ValueError, TypeError, AttributeError):
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
# ATHLETE LOGIN ACCOUNTS — COACH MANAGEMENT
# =========================================================

def initialize_athlete_login_database():
    """
    Create/upgrade the ONE shared athlete-login table used by both
    VEKDYN Coach and VEKDYN Athlete.

    Canonical columns:
        athlete_id
        athlete_key
        team_id
        display_name
        event_group
        password_hash
        active
        password_updated_at

    Older columns such as athlete_name / updated_at are preserved if they
    already exist, but their values are copied into the canonical columns.
    """

    with get_database_connection() as database:
        with database.cursor() as cursor:

            # ---------------------------------------------------------
            # BASE TABLE
            # ---------------------------------------------------------
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_logins (
                    athlete_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                )
                """
            )

            # ---------------------------------------------------------
            # CANONICAL SHARED COLUMNS
            # ---------------------------------------------------------
            cursor.execute(
                """
                ALTER TABLE athlete_logins
                ADD COLUMN IF NOT EXISTS athlete_key TEXT
                """
            )

            cursor.execute(
                """
                ALTER TABLE athlete_logins
                ADD COLUMN IF NOT EXISTS team_id TEXT
                """
            )

            cursor.execute(
                """
                ALTER TABLE athlete_logins
                ADD COLUMN IF NOT EXISTS display_name TEXT
                """
            )

            cursor.execute(
                """
                ALTER TABLE athlete_logins
                ADD COLUMN IF NOT EXISTS event_group TEXT
                DEFAULT 'Distance'
                """
            )

            cursor.execute(
                """
                ALTER TABLE athlete_logins
                ADD COLUMN IF NOT EXISTS password_updated_at TIMESTAMPTZ
                """
            )

            cursor.execute(
                """
                ALTER TABLE athlete_logins
                ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN
                DEFAULT FALSE
                """
            )

            # ---------------------------------------------------------
            # MIGRATE VALUES FROM OLDER ATHLETE-APP COLUMN NAMES
            # ---------------------------------------------------------
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'athlete_logins'
                """
            )
            existing_columns = {row[0] for row in cursor.fetchall()}

            if "athlete_name" in existing_columns:
                cursor.execute(
                    """
                    UPDATE athlete_logins
                    SET display_name = athlete_name
                    WHERE (display_name IS NULL OR BTRIM(display_name) = '')
                      AND athlete_name IS NOT NULL
                      AND BTRIM(athlete_name) <> ''
                    """
                )

            if "updated_at" in existing_columns:
                cursor.execute(
                    """
                    UPDATE athlete_logins
                    SET password_updated_at = updated_at
                    WHERE password_updated_at IS NULL
                      AND updated_at IS NOT NULL
                    """
                )

            # Every existing account needs a permanent athlete_key.
            cursor.execute(
                """
                UPDATE athlete_logins
                SET athlete_key = athlete_id
                WHERE athlete_key IS NULL
                   OR BTRIM(athlete_key) = ''
                """
            )

            cursor.execute(
                """
                UPDATE athlete_logins
                SET event_group = 'Distance'
                WHERE event_group IS NULL
                   OR BTRIM(event_group) = ''
                """
            )

        database.commit()


def get_athlete_login_account(athlete_key):
    """Return the current VEKDYN Athlete account for one athlete."""

    initialize_athlete_login_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    athlete_id,
                    athlete_key,
                    team_id,
                    display_name,
                    event_group,
                    active,
                    password_updated_at,
                    COALESCE(must_change_password, FALSE)
                FROM athlete_logins
                WHERE athlete_key = %s
                   OR athlete_id = %s
                LIMIT 1
                """,
                (athlete_key, athlete_key),
            )
            row = cursor.fetchone()

    if not row:
        return None

    return {
        "athlete_id": row[0],
        "athlete_key": row[1] or row[0],
        "team_id": row[2],
        "display_name": row[3],
        "event_group": row[4] or "Distance",
        "active": bool(row[5]),
        "password_updated_at": row[6],
        "must_change_password": bool(row[7]),
    }


def generate_temporary_password():
    """Generate a readable but strong one-time athlete password."""

    alphabet = string.ascii_letters + string.digits
    groups = [
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
        "".join(secrets.choice(alphabet) for _ in range(4)),
    ]
    return "Vkd-" + "-".join(groups)


def create_or_reset_athlete_login(
        athlete_key,
        team_id,
        display_name,
        event_group="Distance",
):
    """
    Create an athlete account or reset its password.

    Only the bcrypt hash is persisted to Neon. The readable
    temporary password is returned to the coach for delivery
    to the athlete.
    """

    initialize_athlete_login_database()

    athlete_id = str(athlete_key).strip().lower()
    clean_key = str(athlete_key).strip()
    clean_name = str(display_name).strip() or clean_key
    clean_team = str(team_id).strip()
    clean_event_group = str(event_group).strip() or "Distance"

    if not athlete_id or not clean_key or not clean_team:
        raise ValueError("Athlete ID, athlete key, and team are required.")

    temporary_password = generate_temporary_password()
    password_hash = bcrypt.hashpw(
        temporary_password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO athlete_logins (
                    athlete_id,
                    athlete_key,
                    team_id,
                    display_name,
                    event_group,
                    password_hash,
                    active,
                    password_updated_at,
                    must_change_password
                )
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW(), TRUE)

                ON CONFLICT (athlete_id)
                DO UPDATE SET
                    athlete_key = EXCLUDED.athlete_key,
                    team_id = EXCLUDED.team_id,
                    display_name = EXCLUDED.display_name,
                    event_group = EXCLUDED.event_group,
                    password_hash = EXCLUDED.password_hash,
                    active = TRUE,
                    password_updated_at = NOW(),
                    must_change_password = TRUE
                """,
                (
                    athlete_id,
                    clean_key,
                    clean_team,
                    clean_name,
                    clean_event_group,
                    password_hash,
                ),
            )

        database.commit()

    return athlete_id, temporary_password


def set_athlete_login_active(athlete_key, active):
    """Enable or disable an athlete login without deleting its history."""

    initialize_athlete_login_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE athlete_logins
                SET active = %s
                WHERE athlete_key = %s OR athlete_id = %s
                """,
                (bool(active), athlete_key, athlete_key),
            )
        database.commit()


def render_athlete_account_manager(athlete_key, profile, team_id):
    """Coach-facing controls for one athlete's VEKDYN Athlete login."""

    athlete_name = profile.get("name", athlete_key)
    event_group = profile.get("team", "Distance") or "Distance"

    try:
        account = get_athlete_login_account(athlete_key)
    except psycopg2.Error as error:
        st.error(f"VEKDYN could not load this athlete account: {error}")
        return

    st.subheader("Athlete Account")
    st.caption(
        "Create the private login this athlete will use in VEKDYN Athlete. "
        "The temporary password is never stored in readable form."
    )

    with st.container(border=True):
        status_col, id_col = st.columns([1, 2])

        with status_col:
            if account and account.get("active"):
                st.success("Login active")
            elif account:
                st.warning("Login disabled")
            else:
                st.info("No login created")

        with id_col:
            login_id = account.get("athlete_id") if account else str(athlete_key).lower()
            st.markdown(f"**Athlete ID:** `{login_id}`")
            st.caption(f"Linked to {athlete_name} · {team_config(team_id)['short_name']}")

        button_label = (
            "Reset Temporary Password"
            if account
            else "Create Athlete Login"
        )

        if st.button(
                button_label,
                type="primary",
                use_container_width=True,
                key=f"athlete_login_create_reset_{team_id}_{athlete_key}",
        ):
            try:
                new_athlete_id, temporary_password = create_or_reset_athlete_login(
                    athlete_key=athlete_key,
                    team_id=team_id,
                    display_name=athlete_name,
                    event_group=event_group,
                )

                st.session_state[f"temp_login_id_{athlete_key}"] = new_athlete_id
                st.session_state[f"temp_password_{athlete_key}"] = temporary_password
                st.session_state[f"temp_password_notice_{athlete_key}"] = True

                st.rerun()

            except (ValueError, psycopg2.Error) as error:
                st.error(f"VEKDYN could not create the athlete login: {error}")

        temp_password = st.session_state.get(f"temp_password_{athlete_key}")
        temp_login_id = st.session_state.get(f"temp_login_id_{athlete_key}")

        if temp_password and temp_login_id:
            st.success("Temporary athlete credentials generated.")
            st.code(
                f"Athlete ID: {temp_login_id}\n"
                f"Temporary password: {temp_password}",
                language=None,
            )
            st.caption(
                "Give these credentials directly to the athlete. "
                "The readable password is only being kept in this coach session."
            )

            if st.button(
                    "Clear Temporary Password",
                    use_container_width=True,
                    key=f"clear_temp_password_{team_id}_{athlete_key}",
            ):
                st.session_state.pop(f"temp_login_id_{athlete_key}", None)
                st.session_state.pop(f"temp_password_{athlete_key}", None)
                st.session_state.pop(f"temp_password_notice_{athlete_key}", None)
                st.rerun()

        if account:
            st.divider()
            active_now = bool(account.get("active"))
            toggle_label = "Disable Athlete Login" if active_now else "Enable Athlete Login"

            if st.button(
                    toggle_label,
                    use_container_width=True,
                    key=f"toggle_athlete_login_{team_id}_{athlete_key}_{active_now}",
            ):
                try:
                    set_athlete_login_active(athlete_key, not active_now)
                    st.rerun()
                except psycopg2.Error as error:
                    st.error(f"VEKDYN could not update login status: {error}")


# =========================================================
# COROS MCP — COACH-SIDE OAUTH + SLEEP / HRV
# =========================================================
# Each athlete authorizes their own COROS account. VEKDYN stores that OAuth
# connection against the athlete_key selected by the coach.

COROS_MCP_URL = "https://mcpus.coros.com/mcp"
COROS_REDIRECT_URI = "https://vekdyn.streamlit.app"
COROS_TIMEZONE = "America/Chicago"
COROS_PROTOCOL_VERSION = "2025-06-18"


def initialize_coros_database():
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS coros_oauth_client (
                id SMALLINT PRIMARY KEY DEFAULT 1, client_id TEXT NOT NULL,
                client_secret TEXT, registration_json JSONB, updated_at TIMESTAMPTZ DEFAULT NOW())""")
            c.execute("""CREATE TABLE IF NOT EXISTS coros_oauth_pending (
                state TEXT PRIMARY KEY, athlete_key TEXT NOT NULL, code_verifier TEXT NOT NULL,
                token_endpoint TEXT NOT NULL, client_id TEXT NOT NULL, client_secret TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW())""")
            c.execute("""CREATE TABLE IF NOT EXISTS coros_connections (
                athlete_key TEXT PRIMARY KEY, access_token TEXT NOT NULL, refresh_token TEXT,
                token_type TEXT, scope TEXT, expires_at BIGINT, updated_at TIMESTAMPTZ DEFAULT NOW())""")
            c.execute("""CREATE TABLE IF NOT EXISTS coros_recovery_daily (
                athlete_key TEXT NOT NULL, recovery_date DATE NOT NULL, sleep_minutes INTEGER,
                sleep_score INTEGER, hrv_avg INTEGER, hrv_baseline INTEGER,
                hrv_normal_low INTEGER, hrv_normal_high INTEGER, hrv_status TEXT,
                recovery_score INTEGER,
                updated_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (athlete_key, recovery_date))""")
            c.execute("""ALTER TABLE coros_recovery_daily
                ADD COLUMN IF NOT EXISTS recovery_score INTEGER""")
            c.execute("""ALTER TABLE coros_recovery_daily
                ADD COLUMN IF NOT EXISTS sleep_hr_avg INTEGER""")
            c.execute("""ALTER TABLE coros_recovery_daily
                ADD COLUMN IF NOT EXISTS sleep_hr_baseline INTEGER""")
        db.commit()


def _coros_auth_metadata():
    origin = COROS_MCP_URL.split('/mcp', 1)[0]
    resource = None
    for url in (f"{origin}/.well-known/oauth-protected-resource/mcp",
                f"{origin}/.well-known/oauth-protected-resource"):
        r = requests.get(url, timeout=15)
        if r.ok:
            resource = r.json(); break
    if not resource or not resource.get("authorization_servers"):
        raise RuntimeError("COROS OAuth discovery failed.")
    issuer = str(resource["authorization_servers"][0]).rstrip('/')
    for url in (f"{issuer}/.well-known/oauth-authorization-server",
                f"{issuer}/.well-known/openid-configuration"):
        r = requests.get(url, timeout=15)
        if r.ok:
            return r.json()
    raise RuntimeError("COROS authorization metadata could not be loaded.")


def _coros_client():
    initialize_coros_database()
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("SELECT client_id, client_secret FROM coros_oauth_client WHERE id=1")
            row = c.fetchone()
    if row:
        return {"client_id": row[0], "client_secret": row[1]}

    meta = _coros_auth_metadata()
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise RuntimeError("COROS did not advertise dynamic client registration for this server.")
    r = requests.post(endpoint, json={
        "client_name": "VEKDYN",
        "redirect_uris": [COROS_REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, timeout=15)
    r.raise_for_status(); data = r.json()
    if not data.get("client_id"):
        raise RuntimeError("COROS registration did not return a client ID.")
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("""INSERT INTO coros_oauth_client(id,client_id,client_secret,registration_json)
                VALUES(1,%s,%s,%s::jsonb) ON CONFLICT(id) DO UPDATE SET
                client_id=EXCLUDED.client_id, client_secret=EXCLUDED.client_secret,
                registration_json=EXCLUDED.registration_json, updated_at=NOW()""",
                (data["client_id"], data.get("client_secret"), r.text))
        db.commit()
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret")}


def create_coros_login_url(athlete_key):
    meta, client = _coros_auth_metadata(), _coros_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    payload = f"coros.{athlete_key}.{secrets.token_urlsafe(18)}"
    sig = hmac.new(get_login_signing_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    state = f"{payload}.{sig}"
    token_endpoint = meta.get("token_endpoint")
    if not meta.get("authorization_endpoint") or not token_endpoint:
        raise RuntimeError("COROS OAuth endpoints are incomplete.")
    initialize_coros_database()
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("DELETE FROM coros_oauth_pending WHERE created_at < NOW()-INTERVAL '30 minutes'")
            c.execute("""INSERT INTO coros_oauth_pending
                (state,athlete_key,code_verifier,token_endpoint,client_id,client_secret)
                VALUES(%s,%s,%s,%s,%s,%s)""",
                (state, athlete_key, verifier, token_endpoint, client["client_id"], client.get("client_secret")))
        db.commit()
    params = {"response_type":"code", "client_id":client["client_id"],
              "redirect_uri":COROS_REDIRECT_URI, "state":state,
              "code_challenge":challenge, "code_challenge_method":"S256", "resource":COROS_MCP_URL}
    scopes = meta.get("scopes_supported") or []
    if scopes: params["scope"] = " ".join(scopes)
    return f"{meta['authorization_endpoint']}?{urlencode(params)}"


def coros_state_athlete_key(state):
    if not state or not str(state).startswith("coros."): return None
    parts = str(state).split('.')
    if len(parts) < 4: return None
    payload, returned = '.'.join(parts[:-1]), parts[-1]
    expected = hmac.new(get_login_signing_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return parts[1] if hmac.compare_digest(returned, expected) else None


def exchange_coros_authorization_code(code, state):
    athlete_key = coros_state_athlete_key(state)
    if athlete_key not in all_athletes: raise RuntimeError("COROS callback did not match a VEKDYN athlete.")
    initialize_coros_database()
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("SELECT code_verifier,token_endpoint,client_id,client_secret FROM coros_oauth_pending WHERE state=%s",(state,))
            row = c.fetchone()
    if not row: raise RuntimeError("COROS authorization expired. Select Connect COROS again.")
    verifier, endpoint, client_id, secret = row
    form = {"grant_type":"authorization_code","code":code,"redirect_uri":COROS_REDIRECT_URI,
            "client_id":client_id,"code_verifier":verifier,"resource":COROS_MCP_URL}
    if secret: form["client_secret"] = secret
    r = requests.post(endpoint, data=form, timeout=20); r.raise_for_status(); token = r.json()
    if not token.get("access_token"): raise RuntimeError("COROS returned no access token.")
    expires_at = int(time.time()) + int(token.get("expires_in") or 0) if token.get("expires_in") else None
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("""INSERT INTO coros_connections(athlete_key,access_token,refresh_token,token_type,scope,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(athlete_key) DO UPDATE SET
                access_token=EXCLUDED.access_token,refresh_token=EXCLUDED.refresh_token,
                token_type=EXCLUDED.token_type,scope=EXCLUDED.scope,expires_at=EXCLUDED.expires_at,updated_at=NOW()""",
                (athlete_key,token["access_token"],token.get("refresh_token"),token.get("token_type"),token.get("scope"),expires_at))
            c.execute("DELETE FROM coros_oauth_pending WHERE state=%s",(state,))
        db.commit()
    return athlete_key


def load_coros_connection(athlete_key):
    initialize_coros_database()
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("SELECT access_token,refresh_token,expires_at FROM coros_connections WHERE athlete_key=%s",(athlete_key,)); row=c.fetchone()
    return {"access_token":row[0],"refresh_token":row[1],"expires_at":row[2]} if row else {}


def coros_is_connected(athlete_key): return bool(load_coros_connection(athlete_key).get("access_token"))


def get_valid_coros_token(athlete_key):
    con = load_coros_connection(athlete_key)
    if not con: raise RuntimeError("This athlete has not connected COROS.")
    if not con.get("expires_at") or int(con["expires_at"]) > int(time.time())+120: return con["access_token"]
    if not con.get("refresh_token"): raise RuntimeError("COROS session expired. Reconnect COROS.")
    meta, client = _coros_auth_metadata(), _coros_client()
    form={"grant_type":"refresh_token","refresh_token":con["refresh_token"],"client_id":client["client_id"],"resource":COROS_MCP_URL}
    if client.get("client_secret"): form["client_secret"]=client["client_secret"]
    r=requests.post(meta["token_endpoint"],data=form,timeout=20); r.raise_for_status(); t=r.json()
    expires=int(time.time())+int(t.get("expires_in") or 0) if t.get("expires_in") else None
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute("UPDATE coros_connections SET access_token=%s,refresh_token=COALESCE(%s,refresh_token),expires_at=%s,updated_at=NOW() WHERE athlete_key=%s",
                      (t["access_token"],t.get("refresh_token"),expires,athlete_key))
        db.commit()
    return t["access_token"]


def _mcp_json(r):
    r.raise_for_status()
    if "application/json" in r.headers.get("content-type", ""):
        return r.json()
    for line in r.text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                pass
    raise RuntimeError("COROS MCP returned an unreadable response.")


def _normalize_coros_text(value):
    """Turn MCP text content into real newlines before parsing COROS summaries."""
    if value is None:
        return ""
    text_value = str(value).strip()

    # COROS MCP can return the tool text as a JSON-encoded string, e.g.
    # "Sleep Data\\n...". Decode that wrapper first.
    if len(text_value) >= 2 and text_value[0] == '"' and text_value[-1] == '"':
        try:
            decoded = json.loads(text_value)
            if isinstance(decoded, str):
                text_value = decoded
        except Exception:
            pass

    # Defensive fallback for literal escape sequences left by an MCP transport.
    text_value = text_value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return text_value.strip()


def coros_mcp_tool_call(token, name, arguments):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": COROS_PROTOCOL_VERSION,
    }
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": COROS_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "VEKDYN", "version": "1.1"},
        },
    }
    r = requests.post(COROS_MCP_URL, headers=headers, json=init, timeout=25)
    data = _mcp_json(r)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))

    session_id = r.headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    # Complete the MCP handshake before tools/call.
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    ready = requests.post(COROS_MCP_URL, headers=headers, json=initialized, timeout=15)
    ready.raise_for_status()

    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = requests.post(COROS_MCP_URL, headers=headers, json=call, timeout=30)
    data = _mcp_json(response)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))

    text_parts = [
        _normalize_coros_text(item.get("text", ""))
        for item in data.get("result", {}).get("content", [])
        if isinstance(item, dict) and item.get("text") is not None
    ]
    return "\n".join(part for part in text_parts if part)


def _parse_coros_sleep(text):
    """Parse COROS sleep summaries keyed by wake-up date."""
    text = _normalize_coros_text(text)
    records = {}
    date_pattern = re.compile(r"(?m)^(\d{4}-\d{2}-\d{2}):?\s*$")
    matches = list(date_pattern.finditer(text))
    for i, match in enumerate(matches):
        day = match.group(1)
        block = text[match.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        score_match = re.search(r"Sleep Score:\s*(\d+)", block, re.I)
        sleep_match = re.search(r"Main Sleep:\s*(?:(\d+)h\s*)?(\d+)min", block, re.I)
        if score_match or sleep_match:
            records[day] = {
                "sleep_score": int(score_match.group(1)) if score_match else None,
                "sleep_minutes": (
                    int(sleep_match.group(1) or 0) * 60 + int(sleep_match.group(2))
                    if sleep_match else None
                ),
            }
    return records


def _parse_coros_hrv(text):
    """Parse the official COROS HRV assessment, not raw HRV samples."""
    text = _normalize_coros_text(text)
    assessment = text.split("Sleep HRV Time Series", 1)[0]
    pattern = re.compile(
        r"(?m)^(\d{4}-\d{2}-\d{2}):\s*\n"
        r"\s*HRV Avg:\s*(\d+)\s*ms\s*[—-]\s*([^\n]+)\n"
        r"\s*Normal Range:\s*(\d+)\s*-\s*(\d+)\s*ms\s*\n"
        r"\s*Baseline:\s*(\d+)\s*ms",
        re.I,
    )
    records = {}
    for m in pattern.finditer(assessment):
        records[m.group(1)] = {
            "hrv_avg": int(m.group(2)),
            "hrv_status": m.group(3).strip(),
            "hrv_normal_low": int(m.group(4)),
            "hrv_normal_high": int(m.group(5)),
            "hrv_baseline": int(m.group(6)),
        }
    return records


def _parse_coros_daily_health(text):
    """Parse COROS average sleeping heart rate from daily health summaries."""
    text = _normalize_coros_text(text)
    records = {}
    # Daily Health dates are usually rendered as --- 20260904 ---.
    date_pattern = re.compile(r"(?m)^---\s*(\d{8})\s*---\s*$")
    matches = list(date_pattern.finditer(text))
    for i, match in enumerate(matches):
        raw_day = match.group(1)
        day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
        block = text[match.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        sleep_hr = re.search(r"Sleep HR:\s*Avg\s*(\d+)\s*bpm", block, re.I)
        if sleep_hr:
            records[day] = {"sleep_hr_avg": int(sleep_hr.group(1))}
    return records


def _median_int(values):
    clean = sorted(int(v) for v in values if v is not None)
    if not clean:
        return None
    n = len(clean)
    if n % 2:
        return clean[n // 2]
    return int(round((clean[n // 2 - 1] + clean[n // 2]) / 2))


def vekdyn_recovery_score(
    sleep_score,
    hrv_avg,
    hrv_baseline,
    hrv_low=None,
    hrv_high=None,
    sleep_hr_avg=None,
    sleep_hr_baseline=None,
):
    """VEKDYN Recovery v2 using the athlete's own sleep, HRV and sleeping-HR norms.

    Target weights: sleep 45%, HRV 40%, average sleeping HR 15%.
    If one input is unavailable, available components are reweighted instead of
    inventing missing physiological data.
    """
    components = []

    if sleep_score is not None:
        components.append((0.45, max(0.0, min(100.0, float(sleep_score)))))

    if hrv_avg is not None and hrv_baseline not in (None, 0):
        if hrv_low is not None and hrv_high is not None and hrv_low <= hrv_avg <= hrv_high:
            hrv_component = 100.0
        elif hrv_low not in (None, 0) and hrv_avg < hrv_low:
            hrv_component = max(0.0, min(100.0, 100.0 * float(hrv_avg) / float(hrv_low)))
        else:
            reference = float(hrv_baseline)
            deviation = abs(float(hrv_avg) - reference) / max(reference, 1.0)
            hrv_component = max(60.0, 100.0 - deviation * 100.0)
        components.append((0.40, hrv_component))

    if sleep_hr_avg is not None and sleep_hr_baseline not in (None, 0):
        # A modestly lower sleeping HR is not penalized; elevations above personal
        # baseline progressively reduce the HR component.
        ratio = float(sleep_hr_avg) / float(sleep_hr_baseline)
        if ratio <= 1.02:
            hr_component = 100.0
        else:
            hr_component = max(40.0, 100.0 - (ratio - 1.02) * 250.0)
        components.append((0.15, hr_component))

    if not components:
        return None

    weight_total = sum(weight for weight, _ in components)
    return int(round(sum(weight * score for weight, score in components) / weight_total))


def sync_coros_recovery(athlete_key):
    """Sync seven recent COROS recovery days for one athlete."""
    token = get_valid_coros_token(athlete_key)
    args = {"startDate": "", "endDate": "", "days": 7, "timezone": COROS_TIMEZONE}

    sleep_text = coros_mcp_tool_call(token, "querySleepData", args)
    hrv_text = coros_mcp_tool_call(token, "querySleepHrv", args)
    health_text = coros_mcp_tool_call(
        token,
        "queryDailyHealthData",
        {"days": 7, "timezone": COROS_TIMEZONE},
    )

    sleep_days = _parse_coros_sleep(sleep_text)
    hrv_days = _parse_coros_hrv(hrv_text)
    health_days = _parse_coros_daily_health(health_text)
    all_days = sorted(set(sleep_days) | set(hrv_days) | set(health_days))

    if not all_days:
        raise RuntimeError(
            "COROS returned data, but VEKDYN could not parse the recent recovery records."
        )

    sleep_hr_baseline = _median_int(
        record.get("sleep_hr_avg") for record in health_days.values()
    )

    initialize_coros_database()
    with get_database_connection() as db:
        with db.cursor() as c:
            for day in all_days:
                sv = sleep_days.get(day, {})
                hv = hrv_days.get(day, {})
                dv = health_days.get(day, {})
                score = vekdyn_recovery_score(
                    sv.get("sleep_score"),
                    hv.get("hrv_avg"),
                    hv.get("hrv_baseline"),
                    hv.get("hrv_normal_low"),
                    hv.get("hrv_normal_high"),
                    dv.get("sleep_hr_avg"),
                    sleep_hr_baseline,
                )
                c.execute(
                    """INSERT INTO coros_recovery_daily(
                            athlete_key,recovery_date,sleep_minutes,sleep_score,
                            hrv_avg,hrv_baseline,hrv_normal_low,hrv_normal_high,
                            hrv_status,recovery_score,sleep_hr_avg,sleep_hr_baseline)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(athlete_key,recovery_date) DO UPDATE SET
                            sleep_minutes=COALESCE(EXCLUDED.sleep_minutes,coros_recovery_daily.sleep_minutes),
                            sleep_score=COALESCE(EXCLUDED.sleep_score,coros_recovery_daily.sleep_score),
                            hrv_avg=COALESCE(EXCLUDED.hrv_avg,coros_recovery_daily.hrv_avg),
                            hrv_baseline=COALESCE(EXCLUDED.hrv_baseline,coros_recovery_daily.hrv_baseline),
                            hrv_normal_low=COALESCE(EXCLUDED.hrv_normal_low,coros_recovery_daily.hrv_normal_low),
                            hrv_normal_high=COALESCE(EXCLUDED.hrv_normal_high,coros_recovery_daily.hrv_normal_high),
                            hrv_status=COALESCE(EXCLUDED.hrv_status,coros_recovery_daily.hrv_status),
                            recovery_score=COALESCE(EXCLUDED.recovery_score,coros_recovery_daily.recovery_score),
                            sleep_hr_avg=COALESCE(EXCLUDED.sleep_hr_avg,coros_recovery_daily.sleep_hr_avg),
                            sleep_hr_baseline=COALESCE(EXCLUDED.sleep_hr_baseline,coros_recovery_daily.sleep_hr_baseline),
                            updated_at=NOW()""",
                    (
                        athlete_key, day, sv.get("sleep_minutes"), sv.get("sleep_score"),
                        hv.get("hrv_avg"), hv.get("hrv_baseline"), hv.get("hrv_normal_low"),
                        hv.get("hrv_normal_high"), hv.get("hrv_status"), score,
                        dv.get("sleep_hr_avg"), sleep_hr_baseline,
                    ),
                )
        db.commit()
    return load_latest_coros_recovery(athlete_key)


def load_latest_coros_recovery(athlete_key):
    initialize_coros_database()
    with get_database_connection() as db:
        with db.cursor() as c:
            c.execute(
                """SELECT recovery_date,sleep_minutes,sleep_score,hrv_avg,hrv_baseline,
                          hrv_normal_low,hrv_normal_high,hrv_status,recovery_score,
                          sleep_hr_avg,sleep_hr_baseline
                   FROM coros_recovery_daily
                   WHERE athlete_key=%s
                   ORDER BY recovery_date DESC LIMIT 1""",
                (athlete_key,),
            )
            r = c.fetchone()
    if not r:
        return {}
    return {
        "date": r[0], "sleep_minutes": r[1], "sleep_score": r[2], "hrv_avg": r[3],
        "hrv_baseline": r[4], "hrv_normal_low": r[5], "hrv_normal_high": r[6],
        "hrv_status": r[7], "recovery_score": r[8], "sleep_hr_avg": r[9],
        "sleep_hr_baseline": r[10],
    }


# =========================================================
# STRAVA SETTINGS
# =========================================================

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"

STRAVA_REDIRECT_URI = "https://vekdyn.streamlit.app"


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
        owner_name = all_athletes.get(existing_owner, {}).get("profile", {}).get(
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
    expected_name = all_athletes[athlete_key]["profile"].get("name", "")
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


def remember_recent_team(team_id):
    """Keep the three most recently opened teams at the top of the landing page."""
    recent = list(st.session_state.get("recent_team_ids", []))
    recent = [saved_id for saved_id in recent if saved_id != team_id]
    recent.insert(0, team_id)
    st.session_state["recent_team_ids"] = recent[:3]


def open_team_workspace(team_id):
    """Send a visitor to the selected team's protected VEKDYN workspace."""
    if team_id not in TEAM_CONFIG:
        st.error("That VEKDYN team workspace is not configured.")
        return

    remember_recent_team(team_id)

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


def render_pricing_page():
    """Public purchasing page for schools to verify VEKDYN pricing."""
    st.markdown("""
    <style>
    .stApp{background:#f6f8f6}
    [data-testid="stHeader"],[data-testid="stToolbar"],[data-testid="stDecoration"],
    [data-testid="stStatusWidget"],[data-testid="stSidebar"],[data-testid="collapsedControl"],
    [data-testid="stSidebarCollapsedControl"]{display:none!important}
    .block-container{max-width:980px;padding-top:1.6rem;padding-bottom:3rem}
    .price-brand{font-size:28px;font-weight:800;color:#111827;margin-bottom:1.4rem}
    .price-brand span{color:#2f9e44}
    .price-title{text-align:center;font-size:44px;font-weight:800;color:#111827}
    .price-sub{text-align:center;color:#6b7280;font-size:17px;margin:.4rem 0 1.3rem}
    .plan-card{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:24px 26px;min-height:170px;margin-top:8px}
    .plan-name{font-size:20px;font-weight:800;color:#111827}
    .plan-price{font-size:42px;font-weight:800;color:#0b67c2;margin-top:8px}
    .plan-price span{font-size:17px;font-weight:500;color:#6b7280}
    .plan-copy{color:#6b7280;margin-top:7px;line-height:1.45}
    .purchase-box{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:22px 26px;margin-top:18px}
    .purchase-title{font-size:21px;font-weight:800;color:#111827;margin-bottom:14px}
    .purchase-row{display:flex;justify-content:space-between;gap:30px;border-bottom:1px solid #eef0ee;padding:10px 0;color:#374151}
    .purchase-row:last-child{border-bottom:none}.purchase-row strong{color:#111827}
    </style>""", unsafe_allow_html=True)

    st.markdown('<div class="price-brand">VEK<span>DYN</span></div>', unsafe_allow_html=True)

    if st.button("← Find your team"):
        st.session_state["page"] = "home"
        st.rerun()

    st.markdown(
        '<div class="price-title">VEKDYN Team Platform</div>'
        '<div class="price-sub">Performance and training intelligence built for distance running programs.</div>',
        unsafe_allow_html=True,
    )

    annual_col, monthly_col = st.columns(2)

    with annual_col:
        st.markdown(
            """
            <div class="plan-card">
                <div class="plan-name">Annual Team License</div>
                <div class="plan-price">$500 <span>/ year</span></div>
                <div class="plan-copy">Best value for programs purchasing on an annual budget.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with monthly_col:
        st.markdown(
            """
            <div class="plan-card">
                <div class="plan-name">Monthly Team License</div>
                <div class="plan-price">$50 <span>/ month</span></div>
                <div class="plan-copy">Flexible month-to-month access for programs that prefer monthly billing.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.caption("Annual billing saves $100 compared with 12 months of monthly billing.")

    left, right = st.columns(2)
    with left:
        st.markdown("#### Included")
        st.markdown(
            "✓ Team dashboard & analytics  \\n"
            "✓ Athlete performance profiles  \\n"
            "✓ Strava integration  \\n"
            "✓ Team workout planning  \\n"
            "✓ Coach & athlete notes"
        )
    with right:
        st.markdown("#### Performance tools")
        st.markdown(
            "✓ Threshold & training analytics  \\n"
            "✓ Race predictions  \\n"
            "✓ Recovery tracking  \\n"
            "✓ Secure team workspace  \\n"
            "✓ Continuous updates"
        )

    st.markdown(
        """
        <div class="purchase-box">
          <div class="purchase-title">Purchasing Information</div>
          <div class="purchase-row"><span>Vendor / Company Name</span><strong>VEKDYN</strong></div>
          <div class="purchase-row"><span>Product</span><strong>VEKDYN Team Platform</strong></div>
          <div class="purchase-row"><span>License Options</span><strong>Annual or Monthly Team License</strong></div>
          <div class="purchase-row"><span>Published Pricing</span><strong>$500/year or $50/month</strong></div>
          <div class="purchase-row"><span>Billing</span><strong>Annual invoice or monthly billing</strong></div>
          <div class="purchase-row"><span>Payment</span><strong>Invoice / ACH / Check</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.info(
        "Founding-program or pilot pricing may be provided by written quote. "
        "Published standard pricing is $500/year or $50/month per program."
    )

    st.markdown("### Request an Invoice")
    st.caption(
        "Coaches or university purchasing staff can prepare the information needed "
        "for a VEKDYN invoice. No payment or banking information is collected here."
    )

    with st.form("invoice_request_form"):
        invoice_school = st.text_input("School / Program")
        invoice_contact = st.text_input("Coach or Purchasing Contact")
        invoice_email = st.text_input("Contact Email")
        invoice_plan = st.selectbox(
            "License",
            [
                "Annual Team License — $500/year",
                "Monthly Team License — $50/month",
            ],
        )
        invoice_po = st.text_input("PO / Requisition Number (optional)")
        invoice_notes = st.text_area("Purchasing Notes (optional)")
        invoice_submit = st.form_submit_button(
            "Prepare Invoice Request",
            use_container_width=True,
        )

    if invoice_submit:
        if not invoice_school.strip() or not invoice_contact.strip() or not invoice_email.strip():
            st.error("Please enter the school/program, contact name, and contact email.")
        else:
            st.success("Invoice request prepared.")
            st.markdown(
                f"""**Vendor:** VEKDYN  
**Product:** VEKDYN Team Platform  
**School / Program:** {invoice_school}  
**Contact:** {invoice_contact}  
**Email:** {invoice_email}  
**License:** {invoice_plan}  
**PO / Requisition:** {invoice_po or "Not provided"}  
**Notes:** {invoice_notes or "None"}"""
            )


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

    brand_col, pricing_col = st.columns([5, 1.25])
    with brand_col:
        st.markdown('<div class="starter-brand">VEK<span>DYN</span></div>', unsafe_allow_html=True)
    with pricing_col:
        if st.button("For Programs / Pricing", use_container_width=True, key="public_pricing"):
            st.session_state["page"] = "pricing"
            st.rerun()

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

        # -------------------------------------------------
        # RECENT TEAMS + SEARCH RESULTS
        # -------------------------------------------------
        # The landing page shows at most three recent teams. Any other configured
        # school stays hidden until the coach searches for it.
        default_recent = [
            team_id for team_id in ("ollu_distance", "sam_houston", "dark_horse_endurance")
            if team_id in TEAM_CONFIG
        ][:3]
        recent_team_ids = [
            team_id for team_id in st.session_state.get("recent_team_ids", default_recent)
            if team_id in TEAM_CONFIG
        ][:3]

        def team_search_text(team_id):
            config = team_config(team_id)
            return " ".join([
                team_id.replace("_", " "),
                str(config.get("name", "")),
                str(config.get("short_name", "")),
            ]).casefold()

        def team_meta(team_id):
            roster_for_team = get_team_athletes(team_id)
            if roster_for_team:
                return f"{len(roster_for_team)} athletes connected"
            return "Team workspace"

        if normalized_search:
            display_team_ids = [
                team_id for team_id in TEAM_CONFIG
                if all(
                    word in team_search_text(team_id)
                    for word in normalized_search.split()
                )
            ]
            section_title = "Search results"
        else:
            display_team_ids = recent_team_ids
            section_title = "Recently accessed"

        st.markdown(
            f'<div class="recent-heading">{section_title}</div>',
            unsafe_allow_html=True,
        )

        if not display_team_ids:
            st.info("No team account matches that search yet.")

        for team_id in display_team_ids:
            config = team_config(team_id)
            team_image = get_team_image(team_id)
            meta_text = team_meta(team_id)

            with st.container(border=True):
                image_col, text_col, button_col = st.columns(
                    [1.35, 3.8, 1.25],
                    vertical_alignment="center",
                )

                with image_col:
                    if team_image:
                        st.image(str(team_image), use_container_width=True)
                    else:
                        st.markdown(
                            '<div style="height:90px;display:flex;align-items:center;'
                            'justify-content:center;font-size:32px;">🏃</div>',
                            unsafe_allow_html=True,
                        )

                with text_col:
                    st.markdown(
                        f'<div class="recent-team-name">{html.escape(config["name"])}</div>'
                        f'<div class="recent-team-meta">{html.escape(meta_text)}</div>',
                        unsafe_allow_html=True,
                    )

                with button_col:
                    if st.button(
                            "Open Team →",
                            key=f"open_{team_id}",
                            type="primary",
                            use_container_width=True,
                    ):
                        open_team_workspace(team_id)

        # -------------------------------------------------
        # PUBLIC LANDING-PAGE IMAGE
        # -------------------------------------------------

        # This is deliberately separate from every school's card image.
        # Save the general running photo as team_images/running_banner.jpg.
        landing_image = get_team_image("running_banner")
        if landing_image:
            st.markdown('<div class="landing-banner">', unsafe_allow_html=True)
            st.image(str(landing_image), use_container_width=True)
            st.markdown('</div>', unsafe_allow_html=True)

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
    provider_name = "COROS" if str(returned_oauth_state or "").startswith("coros.") else "Strava"
    st.error(f"{provider_name} authorization was cancelled or denied.")
    st.query_params.clear()

if authorization_code and str(returned_oauth_state or "").startswith("coros."):
    try:
        connected_athlete_key = exchange_coros_authorization_code(authorization_code, returned_oauth_state)
        connected_team = athlete_team_lookup.get(connected_athlete_key)
        connected_name = all_athletes[connected_athlete_key]["profile"]["name"]
        st.session_state[f"coros_message_{connected_athlete_key}"] = f"{connected_name}'s COROS connected successfully."
        st.session_state["selected_athlete_key"] = connected_athlete_key
        st.session_state["active_team"] = connected_team
        st.session_state["page"] = "dashboard"
        st.query_params.clear()
        st.rerun()
    except Exception as error:
        st.error(f"COROS authorization failed: {error}")
        st.stop()

if authorization_code and not str(returned_oauth_state or "").startswith("coros."):
    try:
        connected_athlete_key = athlete_key_from_oauth_state(
            returned_oauth_state
        )

        if connected_athlete_key not in all_athletes:
            raise RuntimeError(
                "The Strava connection could not be matched to a VEKDYN profile. "
                "Return to the dashboard and select Connect Strava again."
            )

        connected_team = athlete_team_lookup.get(connected_athlete_key)
        if not connected_team:
            raise RuntimeError(
                "VEKDYN found the athlete but could not determine which team owns "
                "the profile."
            )

        exchange_authorization_code(
            authorization_code,
            connected_athlete_key,
        )

        connected_name = all_athletes[connected_athlete_key]["profile"]["name"]

        st.session_state[
            f"strava_message_{connected_athlete_key}"
        ] = f"{connected_name}'s Strava connected successfully."

        # Return the coach to the same school/athlete that initiated OAuth.
        st.session_state["selected_athlete_key"] = connected_athlete_key
        st.session_state["active_team"] = connected_team
        st.session_state["page"] = "dashboard"

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
                oauth_athlete_key in all_athletes
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

if st.session_state.get("page") == "pricing":
    render_pricing_page()
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

# The existing dashboard remains shared. Only its data source changes.
athletes = get_team_athletes(active_team)

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
# DARK HORSE ENDURANCE — TEAM-SPECIFIC CONCEPT THEME
# =========================================================
# Keep OLLU and Sam Houston on the normal VEKDYN light theme.
# Only the authenticated Dark Horse workspace receives this skin.
if active_team == "dark_horse_endurance":
    st.markdown(
        """
        <style>
        :root {
            --dh-bg: #050506;
            --dh-surface: #09090b;
            --dh-card: #0d0d10;
            --dh-card-soft: #111116;
            --dh-border: #343038;
            --dh-border-soft: #252329;
            --dh-text: #f7f7f8;
            --dh-muted: #aaa6b0;
            --dh-purple: #9b4de4;
            --dh-purple-bright: #b45cff;
            --dh-purple-soft: #21112f;
        }


    /* Dark Horse top header */
    [data-testid="stHeader"],
    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    [data-testid="stStatusWidget"] {
        background: var(--dh-bg) !important;
        color: var(--dh-text) !important;
    }

    header[data-testid="stHeader"] {
        background: var(--dh-bg) !important;
    }



        /* App canvas */
        .stApp,
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        section.main {
            background: var(--dh-bg) !important;
            color: var(--dh-text) !important;
        }

        /* Sidebar from the approved concept */
        [data-testid="stSidebar"] {
            background: #050506 !important;
            border-right: 1px solid var(--dh-border) !important;
        }
        [data-testid="stSidebar"] * {
            color: var(--dh-text);
        }
        [data-testid="stSidebar"] hr {
            border-color: var(--dh-border) !important;
        }
        [data-testid="stSidebar"] .stCaptionContainer,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: var(--dh-muted) !important;
        }
        [data-testid="stSidebar"] div.stButton > button {
            background: transparent !important;
            color: var(--dh-text) !important;
            border: 1px solid #48434d !important;
            box-shadow: none !important;
        }
        [data-testid="stSidebar"] div.stButton > button:hover {
            border-color: var(--dh-purple) !important;
            color: #ffffff !important;
        }
        [data-testid="stSidebar"] div.stButton > button[kind="primary"],
        [data-testid="stSidebar"] div.stButton > button[data-testid="baseButton-primary"] {
            background: linear-gradient(135deg, #7131ad, #9b4de4) !important;
            border-color: #8e45cf !important;
            color: #ffffff !important;
        }
        /* Dark Horse athlete selector — robust across Streamlit/BaseWeb versions */
        [data-testid="stSidebar"] [data-testid="stSelectbox"] > div,
        [data-testid="stSidebar"] [data-testid="stSelectbox"] div[data-baseweb="select"],
        [data-testid="stSidebar"] [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        [data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"],
        [data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div {
            background: #111116 !important;
            background-color: #111116 !important;
            color: #f7f7f8 !important;
            border-color: #9b4de4 !important;
        }

        [data-testid="stSidebar"] [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        [data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] > div {
            border: 1px solid #9b4de4 !important;
            border-radius: 10px !important;
            box-shadow: none !important;
        }

        [data-testid="stSidebar"] [data-testid="stSelectbox"] span,
        [data-testid="stSidebar"] [data-testid="stSelectbox"] input,
        [data-testid="stSidebar"] .stSelectbox span,
        [data-testid="stSidebar"] .stSelectbox input {
            color: #f7f7f8 !important;
            -webkit-text-fill-color: #f7f7f8 !important;
        }

        [data-testid="stSidebar"] [data-testid="stSelectbox"] svg,
        [data-testid="stSidebar"] .stSelectbox svg {
            fill: #b45cff !important;
            color: #b45cff !important;
        }

        /* BaseWeb dropdown is mounted outside the sidebar DOM */
        div[data-baseweb="popover"] > div,
        div[data-baseweb="popover"] ul,
        div[data-baseweb="menu"],
        div[role="listbox"] {
            background: #111116 !important;
            background-color: #111116 !important;
            color: #f7f7f8 !important;
        }

        div[data-baseweb="popover"] li,
        div[data-baseweb="menu"] li,
        div[role="option"] {
            background-color: #111116 !important;
            color: #f7f7f8 !important;
        }

        div[data-baseweb="popover"] li:hover,
        div[data-baseweb="menu"] li:hover,
        div[role="option"]:hover {
            background-color: #21112f !important;
            color: #ffffff !important;
        }

        /* Strava link button under athlete selector */
        [data-testid="stSidebar"] [data-testid="stLinkButton"] a,
        [data-testid="stSidebar"] .stLinkButton a,
        [data-testid="stSidebar"] a[data-testid="stBaseButton-secondary"] {
            background: #111116 !important;
            background-color: #111116 !important;
            color: #f7f7f8 !important;
            border: 1px solid #9b4de4 !important;
            border-radius: 10px !important;
            box-shadow: none !important;
        }

        [data-testid="stSidebar"] [data-testid="stLinkButton"] a:hover,
        [data-testid="stSidebar"] .stLinkButton a:hover,
        [data-testid="stSidebar"] a[data-testid="stBaseButton-secondary"]:hover {
            background: #21112f !important;
            border-color: #b45cff !important;
            color: #ffffff !important;
        }

        /* Main content typography */
        .main h1, .main h2, .main h3, .main h4,
        [data-testid="stMain"] h1, [data-testid="stMain"] h2,
        [data-testid="stMain"] h3, [data-testid="stMain"] h4,
        .athlete-name, .notes-title, .team-workout-title,
        .pb-time, .team-workout-type, .note-author {
            color: var(--dh-text) !important;
        }
        [data-testid="stMain"] p,
        [data-testid="stMain"] label,
        [data-testid="stMain"] span,
        [data-testid="stMain"] div {
            border-color: inherit;
        }
        [data-testid="stMain"] .stCaptionContainer,
        [data-testid="stMain"] .small-text,
        [data-testid="stMain"] .pb-event,
        [data-testid="stMain"] .notes-subtitle,
        [data-testid="stMain"] .team-workout-subtitle,
        [data-testid="stMain"] .team-workout-date,
        [data-testid="stMain"] .note-time {
            color: var(--dh-muted) !important;
        }

        /* Distinct cards/panels — the key difference from a plain black mode */
        [data-testid="stMain"] div[data-testid="stVerticalBlockBorderWrapper"] {
            background: linear-gradient(180deg, var(--dh-card) 0%, #09090b 100%) !important;
            border: 1px solid var(--dh-border) !important;
            border-radius: 14px !important;
            box-shadow: 0 8px 26px rgba(0,0,0,.18) !important;
        }
        .team-workout-card, .note-card {
            background: var(--dh-card) !important;
            border-color: var(--dh-border) !important;
            box-shadow: none !important;
        }
        .note-card-athlete, .note-card-coach {
            background: var(--dh-card-soft) !important;
            border-color: var(--dh-border) !important;
        }
        .note-body, .team-workout-detail {
            color: #ddd9e1 !important;
        }
        .team-workout-notes {
            border-color: var(--dh-border-soft) !important;
            color: var(--dh-muted) !important;
        }

        /* Metrics */
        [data-testid="stMetricValue"] {
            color: var(--dh-text) !important;
        }
        [data-testid="stMetricLabel"] {
            color: var(--dh-muted) !important;
        }

        /* Dark Horse purple replaces green as the workspace accent,
           while athlete status dots/badges can stay green. */
        [data-testid="stMain"] a,
        [data-testid="stMain"] .green-text,
        .team-workout-label {
            color: var(--dh-purple-bright) !important;
        }
        [data-testid="stMain"] div[role="radiogroup"] label,
        [data-testid="stMain"] div[role="radiogroup"] label p,
        [data-testid="stMain"] div[role="radiogroup"] label span {
            color: #e8e5eb !important;
        }
        [data-testid="stMain"] div[role="radiogroup"] input:checked + div {
            border-color: var(--dh-purple) !important;
        }

        /* Inputs and forms */
        [data-testid="stMain"] input,
        [data-testid="stMain"] textarea,
        [data-testid="stMain"] [data-baseweb="select"] > div {
            background: #111116 !important;
            color: var(--dh-text) !important;
            border-color: #49434f !important;
        }
        [data-testid="stMain"] input::placeholder,
        [data-testid="stMain"] textarea::placeholder {
            color: #77727d !important;
        }

        /* Main-area buttons: subtle dark by default, purple for primary actions */
        [data-testid="stMain"] div.stButton > button,
        [data-testid="stMain"] div.stDownloadButton > button {
            background: #0f0f12 !important;
            color: var(--dh-text) !important;
            border: 1px solid #4a454f !important;
            box-shadow: none !important;
        }
        [data-testid="stMain"] div.stButton > button:hover,
        [data-testid="stMain"] div.stDownloadButton > button:hover {
            border-color: var(--dh-purple) !important;
        }
        [data-testid="stMain"] div.stButton > button[kind="primary"],
        [data-testid="stMain"] div.stButton > button[data-testid="baseButton-primary"] {
            background: linear-gradient(135deg, #7131ad, #9b4de4) !important;
            border-color: #9b4de4 !important;
            color: #ffffff !important;
        }

        /* Alerts retain readability while fitting the Dark Horse palette */
        [data-testid="stMain"] [data-testid="stAlert"] {
            background: #15101d !important;
            border: 1px solid #39274b !important;
            color: #eee9f2 !important;
        }
        [data-testid="stMain"] [data-testid="stAlert"] * {
            color: #eee9f2 !important;
        }

        /* Dividers and expanders */
        [data-testid="stMain"] hr {
            border-color: var(--dh-border-soft) !important;
        }
        [data-testid="stMain"] details {
            background: var(--dh-card) !important;
            border-color: var(--dh-border) !important;
        }

        /* Keep the active badge from the original VEKDYN athlete card. */
        .active-badge {
            background-color: #14381d !important;
            color: #7ee294 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# =========================================================
# EMPTY TEAM ROSTER
# =========================================================

if not athletes:
    with st.sidebar:
        st.markdown(
            "## VEK<span style='color:#2f9e44'>DYN</span>",
            unsafe_allow_html=True,
        )
        st.success("▣ Dashboard")
        st.divider()
        st.caption(active_team_config["name"])
        st.info("Roster pending")

        if st.button(
                "Log Out",
                key=f"empty_roster_logout_{active_team}",
                use_container_width=True,
        ):
            log_out()

    st.markdown(f"# {active_team_config['name']}")
    st.markdown("## Team Dashboard")
    st.info(
        "No athletes have been added to this roster yet. "
        "When the coach sends the roster, add it to the Sam Houston roster "
        "source and this same VEKDYN dashboard will populate automatically."
    )

    status_col1, status_col2, status_col3 = st.columns(3)

    with status_col1:
        st.metric("Athletes", "0")

    with status_col2:
        st.metric("Team", active_team_config["short_name"])

    with status_col3:
        st.metric("Workspace", "Ready")

    st.caption(
        "This team's login, roster, and workspace are isolated from OLLU."
    )
    st.stop()

# =========================================================
# SIDEBAR AND ATHLETE SELECTION
# =========================================================

if "dashboard_view" not in st.session_state:
    st.session_state["dashboard_view"] = "Dashboard"

with st.sidebar:
    # -----------------------------------------------------
    # VEKDYN / TEAM IDENTITY
    # -----------------------------------------------------

    st.markdown(
        "## VEK<span style='color:#2f9e44'>DYN</span>",
        unsafe_allow_html=True,
    )

    # Keep the coach and current school immediately under
    # the VEKDYN identity so the active workspace is obvious.
    st.caption("Coach")
    st.caption(active_team_config["name"])

    if st.button(
            "▣ Dashboard",
            key="nav_dashboard",
            use_container_width=True,
            type="primary" if st.session_state["dashboard_view"] == "Dashboard" else "secondary",
    ):
        st.session_state["dashboard_view"] = "Dashboard"

    st.divider()

    # -----------------------------------------------------
    # ATHLETE SELECTION
    # -----------------------------------------------------

    st.markdown("### Choose Athlete")

    athlete_key = st.selectbox(
        "",
        options=list(athletes.keys()),
        format_func=lambda key: athletes[key]["profile"]["name"],
        key="sidebar_athlete_selector",
    )

    # We can read the selected athlete immediately so the
    # Strava controls sit directly below the selector.
    selected_sidebar_athlete = athletes[athlete_key]
    selected_sidebar_profile = selected_sidebar_athlete["profile"]
    athlete_name_for_button = selected_sidebar_profile.get("name", "Athlete")
    athlete_first_name = athlete_name_for_button.split()[0]

    weekly_session_key = f"{athlete_key}_strava_weekly"
    heart_session_key = f"{athlete_key}_strava_heart_rate"
    message_session_key = f"strava_message_{athlete_key}"
    error_session_key = f"strava_error_{athlete_key}"

    # -----------------------------------------------------
    # STRAVA — DIRECTLY UNDER ATHLETE SELECTOR
    # -----------------------------------------------------

    if strava_is_connected(athlete_key):

        # Automatically sync only when the coach selects a different athlete.
        # This avoids a redundant Sync button and prevents extra Strava API calls
        # on every normal Streamlit rerun.
        auto_sync_key = f"{active_team}:{athlete_key}"

        if st.session_state.get("last_auto_synced_athlete") != auto_sync_key:
            try:
                token = get_valid_strava_token(athlete_key)

                weekly, heart_rate = get_strava_training_data(
                    token,
                    number_of_weeks=8,
                )

                st.session_state[weekly_session_key] = weekly
                st.session_state[heart_session_key] = heart_rate

                st.session_state[message_session_key] = (
                    f"{athlete_name_for_button}'s Strava synced automatically."
                )
                st.session_state.pop(error_session_key, None)

                # Mark this athlete as synced only after a successful request.
                st.session_state["last_auto_synced_athlete"] = auto_sync_key

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
            st.caption(
                f"Connected Strava account: {connected_strava_name}"
            )
        else:
            st.caption("Strava connected")

        reconnect_url = create_strava_login_url(athlete_key)

        if reconnect_url:
            st.link_button(
                f"Reconnect {athlete_first_name}'s Strava",
                reconnect_url,
                use_container_width=True,
            )

    else:

        login_url = create_strava_login_url(athlete_key)

        if login_url:
            st.link_button(
                f"Connect {athlete_first_name}'s Strava",
                login_url,
                use_container_width=True,
            )
        else:
            st.warning(
                "Add the Strava Client ID to secrets.toml first."
            )

    # -----------------------------------------------------
    # STRAVA STATUS
    # -----------------------------------------------------

    if st.session_state.get(message_session_key):
        st.success(
            st.session_state[message_session_key]
        )

    elif st.session_state.get(error_session_key):

        if strava_is_connected(athlete_key):
            st.warning(
                "Strava sync failed. No live Strava mileage is "
                "available right now. "
                f"Details: {st.session_state[error_session_key]}"
            )
        else:
            st.warning(
                st.session_state[error_session_key]
            )

    elif not strava_is_connected(athlete_key):
        st.caption(
            "This athlete has not connected Strava yet."
        )

    # COROS connection lives on the coach side, directly under the selected athlete.
    coros_message_key = f"coros_message_{athlete_key}"
    coros_error_key = f"coros_error_{athlete_key}"
    try:
        coros_connected = coros_is_connected(athlete_key)
        if coros_connected:
            st.caption("COROS connected")
            if st.button(f"Sync {athlete_first_name}'s COROS recovery", use_container_width=True, key=f"sync_coros_{active_team}_{athlete_key}"):
                try:
                    sync_coros_recovery(athlete_key)
                    st.session_state[coros_message_key] = f"{athlete_name_for_button}'s recovery data synced from COROS."
                    st.session_state.pop(coros_error_key, None)
                    st.rerun()
                except Exception as error:
                    st.session_state[coros_error_key] = str(error)
        else:
            coros_login_url = create_coros_login_url(athlete_key)
            st.link_button(f"Connect {athlete_first_name}'s COROS", coros_login_url, use_container_width=True)
            st.caption(f"{athlete_first_name} must authorize their own COROS account.")
    except Exception as error:
        st.session_state[coros_error_key] = str(error)

    if st.session_state.get(coros_message_key): st.success(st.session_state[coros_message_key])
    if st.session_state.get(coros_error_key): st.warning(f"COROS: {st.session_state[coros_error_key]}")

    st.divider()

    # -----------------------------------------------------
    # ATHLETE OVERVIEW
    # -----------------------------------------------------

    st.markdown("### Athlete Overview")

    nav_items = [
        ("♙ Profile", "Profile"),
        ("♨ Training", "Training"),
        ("↗ Performance", "Performance"),
        ("♡ Recovery", "Recovery"),
        ("📝 Notes", "Notes"),
    ]

    for label, view_name in nav_items:
        if st.button(
                label,
                key=f"nav_{view_name.lower()}",
                use_container_width=True,
                type="primary" if st.session_state.get("dashboard_view") == view_name else "secondary",
        ):
            st.session_state["dashboard_view"] = view_name

    dashboard_view = st.session_state.get("dashboard_view", "Dashboard")

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
            not st.session_state.get(
                "show_contact_form",
                False,
            )
        )

    if st.session_state.get(
            "show_contact_form",
            False,
    ):

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
                st.success(
                    "Thanks — your message is ready to send."
                )

    # -----------------------------------------------------
    # LOG OUT — NOTHING BELOW THIS
    # -----------------------------------------------------

    if st.button(
            "Log Out",
            key="logout_button",
            use_container_width=True,
    ):
        log_out()

# =========================================================
# SELECTED ATHLETE DATA
# =========================================================

athlete = athletes[athlete_key]
profile = athlete["profile"]
# Define the selected athlete name before any focused view is rendered.
# This keeps Notes (and other focused pages) from depending on the Profile view
# having run first.
athlete_name = profile.get("name", "Unknown Athlete")
personal_bests = athlete.get("pbs", {})
xc_results = athlete.get("xc_results", {})
training = athlete.get("training", {})
recovery = athlete.get(
    "recovery",
    training.get("recovery", {}),
)

try:
    coros_recovery = load_latest_coros_recovery(athlete_key) if coros_is_connected(athlete_key) else {}
except Exception:
    coros_recovery = {}
threshold_lactate = athlete.get(
    "threshold_lactate",
    training.get("threshold_lactate", {}),
)

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


if dashboard_view in {"Dashboard", "Profile"}:
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

            school_logo = get_team_logo(active_team)

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

if dashboard_view in {"Dashboard", "Profile", "Performance", "Recovery"}:
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

if dashboard_view in {"Dashboard", "Training", "Recovery"}:
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
                sleep_hr_value = coros_recovery.get("sleep_hr_avg")
                st.metric(
                    "Average Sleeping HR",
                    f"{sleep_hr_value} bpm" if sleep_hr_value is not None else "None",
                )
                if coros_recovery.get("sleep_hr_baseline") is not None:
                    st.caption(f"7-day sleeping-HR baseline: {coros_recovery['sleep_hr_baseline']} bpm")
                elif coros_is_connected(athlete_key):
                    st.caption("COROS connected")

            with sleep_right:
                coros_sleep_minutes = coros_recovery.get("sleep_minutes")
                sleep_display = (
                    f"{coros_sleep_minutes // 60}h {coros_sleep_minutes % 60}m"
                    if coros_sleep_minutes is not None else "None"
                )
                st.metric("Sleep Time", sleep_display)
                if coros_recovery.get("sleep_score") is not None:
                    st.caption(f"COROS sleep score: {coros_recovery['sleep_score']}")

            st.divider()

            recovery_left, recovery_right = st.columns(2)

            with recovery_left:
                hrv_value = coros_recovery.get("hrv_avg")
                st.metric(
                    "Average HRV",
                    f"{hrv_value} ms" if hrv_value is not None else "None",
                )
                if coros_recovery.get("hrv_status"):
                    detail = f"COROS: {coros_recovery['hrv_status']}"
                    if coros_recovery.get("hrv_baseline") is not None:
                        detail += f" · baseline {coros_recovery['hrv_baseline']} ms"
                    if (coros_recovery.get("hrv_normal_low") is not None
                            and coros_recovery.get("hrv_normal_high") is not None):
                        detail += f" · normal {coros_recovery['hrv_normal_low']}–{coros_recovery['hrv_normal_high']} ms"
                    st.caption(detail)

            with recovery_right:
                recovery_score = coros_recovery.get("recovery_score")
                recovery_display = f"{recovery_score}%" if recovery_score is not None else "None"
                st.metric("VEKDYN Recovery Score", recovery_display)
                if recovery_score is not None:
                    st.caption("Sleep + individualized HRV + average sleeping HR")

if dashboard_view in {"Dashboard", "Notes"}:
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
# VEKDYN PERFORMANCE PREDICTION ENGINE
# =========================================================

def vekdyn_time_to_seconds(value):
    """Convert race/pace strings such as 1:55.20, 14:55, or 5:05/mi to seconds."""
    if value in (None, "", "--"):
        return None

    clean = str(value).strip().lower()
    clean = clean.replace("/mile", "").replace("/mi", "").replace("per mile", "")
    clean = re.sub(r"[^0-9:.]", "", clean)

    if not clean:
        return None

    try:
        parts = clean.split(":")
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, TypeError):
        return None


def vekdyn_seconds_to_time(seconds, decimals=1):
    if seconds is None:
        return "--"

    minutes = int(seconds // 60)
    remaining = seconds - (minutes * 60)

    if decimals == 0:
        return f"{minutes}:{int(round(remaining)):02d}"

    width = 3 + decimals
    return f"{minutes}:{remaining:0{width}.{decimals}f}"


def vekdyn_linear_score(value, slow_value, fast_value, low_score, high_score):
    """Continuous score where a lower time/pace is better."""
    if value is None:
        return 0

    if value >= slow_value:
        return int(round(low_score))
    if value <= fast_value:
        return int(round(high_score))

    fraction = (slow_value - value) / (slow_value - fast_value)
    return int(round(low_score + fraction * (high_score - low_score)))


def vekdyn_male_speed_reserve_score(eight_seconds):
    """Male distance-runner speed-reserve scale discussed for VEKDYN."""
    if eight_seconds is None:
        return 0, "No 800m PB"

    if eight_seconds >= 125:
        return 12, "Low"
    if eight_seconds >= 120:
        return vekdyn_linear_score(eight_seconds, 125, 120, 15, 30), "Low"
    if eight_seconds >= 118:
        return vekdyn_linear_score(eight_seconds, 120, 118, 30, 45), "Developing"
    if eight_seconds >= 115:
        return vekdyn_linear_score(eight_seconds, 118, 115, 45, 65), "Good"
    if eight_seconds >= 110:
        return vekdyn_linear_score(eight_seconds, 115, 110, 65, 88), "Elite"
    return min(100, vekdyn_linear_score(eight_seconds, 110, 105, 88, 100)), "National"


def vekdyn_male_aerobic_score(five_k_seconds):
    """Continuous 5K ability score using the VEKDYN male distance framework."""
    if five_k_seconds is None:
        return 0, "No 5K PB"

    if five_k_seconds >= 1050:  # 17:30+
        return 12, "Low"
    if five_k_seconds >= 990:  # 17:30-16:30
        return vekdyn_linear_score(five_k_seconds, 1050, 990, 15, 28), "Low"
    if five_k_seconds >= 930:  # 16:30-15:30
        return vekdyn_linear_score(five_k_seconds, 990, 930, 28, 43), "Okay"
    if five_k_seconds >= 880:  # 15:30-14:40
        return vekdyn_linear_score(five_k_seconds, 930, 880, 43, 65), "Good"
    if five_k_seconds >= 840:  # 14:40-14:00
        return vekdyn_linear_score(five_k_seconds, 880, 840, 65, 82), "Competitive"
    if five_k_seconds >= 810:  # 14:00-13:30
        return vekdyn_linear_score(five_k_seconds, 840, 810, 82, 92), "Elite"
    return min(100, vekdyn_linear_score(five_k_seconds, 810, 780, 92, 100)), "National Competitive"


def vekdyn_parse_lactate(value):
    if value in (None, "", "--"):
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def vekdyn_threshold_profile(threshold):
    """
    Use the fastest entered threshold pace that remains controlled at 2.0-3.5 mmol.
    If no controlled sample exists, fall back to the best usable entered sample
    but lower the score/confidence.
    """
    samples = []

    for rep_name in ("short_reps", "medium_reps", "long_reps"):
        rep = threshold.get(rep_name, {}) or {}
        pace_seconds = vekdyn_time_to_seconds(rep.get("pace"))
        lactate = vekdyn_parse_lactate(rep.get("lactate"))

        if pace_seconds is not None and lactate is not None:
            samples.append({
                "rep": rep_name,
                "pace": pace_seconds,
                "lactate": lactate,
            })

    if not samples:
        return 0, "No LT data", None, False

    controlled = [
        sample for sample in samples
        if 2.0 <= sample["lactate"] <= 3.5
    ]

    if controlled:
        best = min(controlled, key=lambda sample: sample["pace"])
        pace = best["pace"]

        if pace >= 345:
            score, label = 20, "Low"
        elif pace >= 325:
            score, label = vekdyn_linear_score(pace, 345, 325, 25, 40), "Developing"
        elif pace >= 305:
            score, label = vekdyn_linear_score(pace, 325, 305, 40, 60), "Good"
        elif pace >= 290:
            score, label = vekdyn_linear_score(pace, 305, 290, 60, 76), "Very Good"
        elif pace >= 275:
            score, label = vekdyn_linear_score(pace, 290, 275, 76, 90), "Elite"
        else:
            score, label = min(100, vekdyn_linear_score(pace, 275, 260, 90, 100)), "Exceptional"

        # Reward a stable controlled profile across multiple entered rep lengths.
        if len(controlled) >= 2:
            score = min(100, score + 3)
        if len(controlled) >= 3:
            score = min(100, score + 2)

        return score, label, best, True

    best = min(samples, key=lambda sample: abs(sample["lactate"] - 2.75))
    return 30, "LT uncertain", best, False


def vekdyn_recent_mileage(volume_data):
    """Average up to the four most recent non-zero weekly mileage values."""
    if volume_data is None or getattr(volume_data, "empty", True):
        return None

    try:
        miles = pd.to_numeric(volume_data["Mileage"], errors="coerce").dropna()
        miles = miles[miles > 0].tail(4)
        if miles.empty:
            return None
        return float(miles.mean())
    except (KeyError, TypeError, ValueError):
        return None


def vekdyn_male_volume_compatibility(eight_seconds, weekly_miles):
    """
    VEKDYN volume/speed-reserve compatibility heuristic.
    This is a performance-model assumption, not a safety cutoff.
    """
    if eight_seconds is None or weekly_miles is None:
        return 50, "Unknown"

    if eight_seconds >= 120:
        target_low, target_high = 45, 60
    elif eight_seconds >= 118:
        target_low, target_high = 50, 65
    elif eight_seconds >= 115:
        target_low, target_high = 60, 75
    elif eight_seconds >= 110:
        target_low, target_high = 65, 85
    else:
        target_low, target_high = 70, 95

    if target_low <= weekly_miles <= target_high:
        return 92, "Matched"

    if weekly_miles < target_low:
        gap = target_low - weekly_miles
        return max(35, int(round(88 - gap * 3))), "Below model range"

    gap = weekly_miles - target_high
    return max(35, int(round(85 - gap * 2.5))), "Check elasticity"


# =========================================================
# WOMEN'S PERFORMANCE MODEL — V1
# =========================================================
# The women's model is intentionally more aerobic-strength weighted than the
# men's model. Threshold values are displayed as coach data but are not yet
# used to move the women's prediction until VEKDYN has enough testing data to
# calibrate that relationship.

def vekdyn_is_female(profile):
    sex = str((profile or {}).get("sex", "")).strip().casefold()
    return sex in {"f", "female", "woman", "women", "w"}


def vekdyn_women_speed_reserve_score(eight_seconds):
    if eight_seconds is None:
        return 0, "No 800m PB"
    if eight_seconds >= 152:  # 2:32+
        return 15, "Low"
    if eight_seconds >= 145:  # 2:25-2:32
        return vekdyn_linear_score(eight_seconds, 152, 145, 15, 28), "Low"
    if eight_seconds >= 140:  # 2:20-2:24
        return vekdyn_linear_score(eight_seconds, 145, 140, 28, 42), "Adequate"
    if eight_seconds >= 135:  # 2:15-2:19
        return vekdyn_linear_score(eight_seconds, 140, 135, 42, 58), "Good"
    if eight_seconds >= 130:  # 2:10-2:14
        return vekdyn_linear_score(eight_seconds, 135, 130, 58, 74), "High"
    if eight_seconds >= 125:  # 2:05-2:09
        return vekdyn_linear_score(eight_seconds, 130, 125, 74, 90), "Very High"
    return min(100, vekdyn_linear_score(eight_seconds, 125, 120, 90, 100)), "Elite"


def vekdyn_women_aerobic_score(five_k_seconds):
    if five_k_seconds is None:
        return 0, "No 5K PB"
    if five_k_seconds >= 1140:  # 19:00+
        return 18, "Developing"
    if five_k_seconds >= 1100:  # 19:00-18:20
        return vekdyn_linear_score(five_k_seconds, 1140, 1100, 20, 35), "Okay"
    if five_k_seconds >= 1030:  # 18:20-17:10
        return vekdyn_linear_score(five_k_seconds, 1100, 1030, 35, 58), "Good"
    if five_k_seconds >= 970:  # 17:10-16:10
        return vekdyn_linear_score(five_k_seconds, 1030, 970, 58, 78), "Competitive"
    if five_k_seconds >= 920:  # 16:10-15:20
        return vekdyn_linear_score(five_k_seconds, 970, 920, 78, 92), "Elite"
    return min(100, vekdyn_linear_score(five_k_seconds, 920, 870, 92, 100)), "Pro-level"


def vekdyn_women_volume_compatibility(eight_seconds, weekly_miles):
    if weekly_miles is None:
        return 50, "Unknown"
    if weekly_miles < 30:
        return max(35, int(round(65 - (30 - weekly_miles) * 2))), "Below model range"
    if weekly_miles < 45:
        # Low for a 5K profile, but reasonable for an 800/1500 athlete.
        score = 82 if eight_seconds is not None and eight_seconds <= 135 else 72
        return score, "Low / middle-distance appropriate"
    if weekly_miles < 60:
        return 94, "Balanced"
    if weekly_miles <= 80:
        return 86, "High"
    return max(45, int(round(78 - (weekly_miles - 80) * 1.5))), "Very high / specialized"


VEKDYN_WOMEN_800_GRID = [152, 145, 140, 135, 130, 125, 120]
VEKDYN_WOMEN_5K_GRID = [1140, 1100, 1030, 970, 920, 880]

# Women's 1500 capability surface (seconds). 5K strength carries more weight
# than in the men's surface, while 800 speed remains an important modifier.
VEKDYN_WOMEN_1500_MATRIX = [
    [325, 314, 296, 281, 267, 259],  # 2:32
    [317, 307, 290, 276, 263, 255],  # 2:25
    [311, 301, 284, 271, 260, 252],  # 2:20
    [304, 295, 279, 267, 257, 249],  # 2:15
    [298, 289, 274, 263, 253, 246],  # 2:10
    [292, 283, 269, 259, 249, 242],  # 2:05
    [287, 278, 264, 254, 245, 238],  # 2:00
]


def vekdyn_women_base_1500_prediction(eight_seconds, five_k_seconds):
    if eight_seconds is None or five_k_seconds is None:
        return None

    eight = max(min(eight_seconds, max(VEKDYN_WOMEN_800_GRID)), min(VEKDYN_WOMEN_800_GRID))
    five_k = max(min(five_k_seconds, max(VEKDYN_WOMEN_5K_GRID)), min(VEKDYN_WOMEN_5K_GRID))

    e_fast, e_slow = vekdyn_bracket(eight, VEKDYN_WOMEN_800_GRID, descending=False)
    f_fast, f_slow = vekdyn_bracket(five_k, VEKDYN_WOMEN_5K_GRID, descending=False)

    def matrix_value(e_value, f_value):
        row = VEKDYN_WOMEN_800_GRID.index(e_value)
        col = VEKDYN_WOMEN_5K_GRID.index(f_value)
        return VEKDYN_WOMEN_1500_MATRIX[row][col]

    q11 = matrix_value(e_fast, f_fast)
    q12 = matrix_value(e_fast, f_slow)
    q21 = matrix_value(e_slow, f_fast)
    q22 = matrix_value(e_slow, f_slow)

    if f_fast == f_slow:
        top, bottom = q11, q21
    else:
        top = vekdyn_interpolate_1d(five_k, f_fast, f_slow, q11, q12)
        bottom = vekdyn_interpolate_1d(five_k, f_fast, f_slow, q21, q22)

    if e_fast == e_slow:
        return top
    return vekdyn_interpolate_1d(eight, e_fast, e_slow, top, bottom)


VEKDYN_800_GRID = [122, 119, 117, 115, 112, 110, 108]
VEKDYN_5K_GRID = [990, 930, 900, 880, 860, 840, 820, 800]

# Midpoints (seconds) of the mile ranges we built together.
VEKDYN_MILE_MATRIX = [
    [280, 269, 263.5, 260, 257, 254, 251, 248],  # 2:02
    [274, 265.5, 260, 257, 254, 251, 248, 245],  # 1:59
    [271, 262.5, 258, 255, 252, 249, 246, 243],  # 1:57
    [268, 260.5, 256, 253, 250, 247, 244, 241],  # 1:55
    [264, 256.5, 252, 249, 246, 243, 240, 237],  # 1:52
    [261, 253.5, 249, 246, 243, 240, 237, 234],  # 1:50
    [258, 250.5, 246, 243, 240.5, 237, 234, 231],  # 1:48
]


def vekdyn_interpolate_1d(x, x1, x2, y1, y2):
    if x1 == x2:
        return y1
    fraction = (x - x1) / (x2 - x1)
    return y1 + fraction * (y2 - y1)


def vekdyn_bracket(value, grid, descending=False):
    ordered = sorted(grid, reverse=descending)

    if descending:
        if value >= ordered[0]:
            return ordered[0], ordered[0]
        if value <= ordered[-1]:
            return ordered[-1], ordered[-1]
        for a, b in zip(ordered, ordered[1:]):
            if a >= value >= b:
                return a, b
    else:
        if value <= ordered[0]:
            return ordered[0], ordered[0]
        if value >= ordered[-1]:
            return ordered[-1], ordered[-1]
        for a, b in zip(ordered, ordered[1:]):
            if a <= value <= b:
                return a, b

    return ordered[-1], ordered[-1]


def vekdyn_male_base_mile_prediction(eight_seconds, five_k_seconds):
    """
    Bilinear interpolation across the VEKDYN 800 x 5K matrix.
    This avoids snapping a 1:53.4 / 14:32 athlete to a single box.
    """
    if eight_seconds is None or five_k_seconds is None:
        return None

    # Clamp only outside the current calibration surface.
    eight = max(min(eight_seconds, max(VEKDYN_800_GRID)), min(VEKDYN_800_GRID))
    five_k = max(min(five_k_seconds, max(VEKDYN_5K_GRID)), min(VEKDYN_5K_GRID))

    e_fast, e_slow = vekdyn_bracket(eight, VEKDYN_800_GRID, descending=False)
    f_fast, f_slow = vekdyn_bracket(five_k, VEKDYN_5K_GRID, descending=False)

    def matrix_value(e_value, f_value):
        row = VEKDYN_800_GRID.index(e_value)
        col = VEKDYN_5K_GRID.index(f_value)
        return VEKDYN_MILE_MATRIX[row][col]

    q11 = matrix_value(e_fast, f_fast)
    q12 = matrix_value(e_fast, f_slow)
    q21 = matrix_value(e_slow, f_fast)
    q22 = matrix_value(e_slow, f_slow)

    if f_fast == f_slow:
        top = q11
        bottom = q21
    else:
        top = vekdyn_interpolate_1d(five_k, f_fast, f_slow, q11, q12)
        bottom = vekdyn_interpolate_1d(five_k, f_fast, f_slow, q21, q22)

    if e_fast == e_slow:
        return top

    return vekdyn_interpolate_1d(eight, e_fast, e_slow, top, bottom)


def vekdyn_elasticity_modifier(status="Preserved"):
    """
    The current VEKDYN model assumes elasticity/speed expression is preserved
    unless the athlete/coach marks evidence that it is not.
    """
    modifiers = {
        "Preserved": 0.0,
        "Moderately Preserved": 2.0,
        "Uncertain": 4.0,
        "Suppressed": 7.0,
    }
    return modifiers.get(status, 0.0)


def vekdyn_predict_1500(
        personal_bests,
        threshold,
        volume_data,
        elasticity_status="Preserved",
        profile=None,
):
    eight_seconds = vekdyn_time_to_seconds(personal_bests.get("800"))
    five_k_seconds = vekdyn_time_to_seconds(personal_bests.get("5k"))
    is_female = vekdyn_is_female(profile)

    weekly_miles = vekdyn_recent_mileage(volume_data)

    if is_female:
        speed_score, speed_label = vekdyn_women_speed_reserve_score(eight_seconds)
        aerobic_score, aerobic_label = vekdyn_women_aerobic_score(five_k_seconds)
        volume_score, volume_label = vekdyn_women_volume_compatibility(eight_seconds, weekly_miles)
        predicted_1500 = vekdyn_women_base_1500_prediction(eight_seconds, five_k_seconds)
        threshold_score, threshold_label, lt_sample, lt_controlled = (
            50, "Coach calibration pending", None, False
        )
        model_name = "Women's V1"
    else:
        speed_score, speed_label = vekdyn_male_speed_reserve_score(eight_seconds)
        aerobic_score, aerobic_label = vekdyn_male_aerobic_score(five_k_seconds)
        threshold_score, threshold_label, lt_sample, lt_controlled = vekdyn_threshold_profile(threshold)
        volume_score, volume_label = vekdyn_male_volume_compatibility(eight_seconds, weekly_miles)
        base_mile = vekdyn_male_base_mile_prediction(eight_seconds, five_k_seconds)
        predicted_1500 = None if base_mile is None else (
                (base_mile + vekdyn_elasticity_modifier(elasticity_status)) * (1500.0 / 1609.344)
        )
        model_name = "Men's model"

    if predicted_1500 is None:
        return {
            "available": False,
            "reason": "An 800m PB and 5K PB are needed for the VEKDYN prediction.",
            "speed_score": speed_score,
            "aerobic_score": aerobic_score,
            "threshold_score": threshold_score,
            "volume_score": volume_score,
            "elasticity_score": 90 if elasticity_status == "Preserved" else 65,
            "model_name": model_name,
            "is_female": is_female,
        }

    # Women's V1 uses 5K strength as the primary anchor and 800 speed as a
    # modifier. Elasticity is retained as a display factor but does not move
    # the women's prediction until the model is calibrated with more data.
    if is_female:
        adjusted_1500 = predicted_1500
        adjusted_mile = adjusted_1500 * (1609.344 / 1500.0)
    else:
        adjusted_1500 = predicted_1500
        adjusted_mile = adjusted_1500 * (1609.344 / 1500.0)

    confidence = 52
    confidence += 12 if eight_seconds is not None else 0
    confidence += 15 if five_k_seconds is not None else 0
    confidence += 8 if weekly_miles is not None else 0
    if not is_female:
        confidence += 8 if lt_controlled else (3 if lt_sample is not None else 0)
    confidence += 4 if volume_label in {"Matched", "Balanced"} else 0
    confidence += 3 if elasticity_status == "Preserved" else 0
    confidence = min(95 if not is_female else 88, confidence)

    half_range = 3.0 if not is_female else 5.0
    if weekly_miles is None:
        half_range += 1.0
    if not is_female and lt_sample is None:
        half_range += 1.5
    if elasticity_status in {"Uncertain", "Suppressed"}:
        half_range += 1.5

    return {
        "available": True,
        "mile_seconds": adjusted_mile,
        "mile_display": vekdyn_seconds_to_time(adjusted_mile, 1),
        "1500_seconds": adjusted_1500,
        "1500_low": vekdyn_seconds_to_time(adjusted_1500 - half_range, 1),
        "1500_mid": vekdyn_seconds_to_time(adjusted_1500, 1),
        "1500_high": vekdyn_seconds_to_time(adjusted_1500 + half_range, 1),
        "1500_range": (
            f"{vekdyn_seconds_to_time(adjusted_1500 - half_range, 1)}"
            f"–{vekdyn_seconds_to_time(adjusted_1500 + half_range, 1)}"
        ),
        "confidence": confidence,
        "speed_score": speed_score,
        "speed_label": speed_label,
        "aerobic_score": aerobic_score,
        "aerobic_label": aerobic_label,
        "threshold_score": threshold_score,
        "threshold_label": threshold_label,
        "volume_score": volume_score,
        "volume_label": volume_label,
        "weekly_miles": weekly_miles,
        "elasticity_score": (
            92 if elasticity_status == "Preserved"
            else 75 if elasticity_status == "Moderately Preserved"
            else 58 if elasticity_status == "Uncertain"
            else 40
        ),
        "elasticity_status": elasticity_status,
        "lt_sample": lt_sample,
        "model_name": model_name,
        "is_female": is_female,
    }


if dashboard_view in {"Dashboard", "Performance"}:
    # =========================================================
    # PERFORMANCE PREDICTIONS — LIVE VEKDYN MODEL
    # =========================================================

    threshold_for_prediction = athlete.get("threshold", {})

    # The current model assumes the athlete's elasticity/speed qualities are being
    # preserved unless future athlete/coach data says otherwise.
    elasticity_status = athlete.get("elasticity_status", "Preserved")

    vekdyn_prediction = vekdyn_predict_1500(
        personal_bests=personal_bests,
        threshold=threshold_for_prediction,
        volume_data=volume_data,
        elasticity_status=elasticity_status,
        profile=profile,
    )

    with st.container(border=True):

        st.subheader("Performance Predictions")

        if vekdyn_prediction.get("is_female"):
            st.caption(
                "VEKDYN Women's V1: 5K aerobic strength is the primary anchor, "
                "with 800m speed reserve and volume as modifiers. Threshold calibration is pending coach testing data."
            )
        else:
            st.caption(
                "VEKDYN profile model: 800m speed reserve + 5K aerobic ability, "
                "supported by threshold, volume compatibility, and preserved elasticity."
            )

        prediction_col, confidence_col, factors_col = st.columns([2, 1, 2])

        with prediction_col:
            st.caption("Predicted 1500m Range")

            if vekdyn_prediction["available"]:
                st.markdown(f"## {vekdyn_prediction['1500_range']}")
                st.caption(
                    f"Estimated mile capability: {vekdyn_prediction['mile_display']}"
                )
            else:
                st.markdown("## --")
                st.caption(vekdyn_prediction["reason"])

            st.caption("Prediction updates as athlete and training data change.")

        with confidence_col:
            confidence = vekdyn_prediction.get("confidence", 0)

            st.metric(
                "Confidence",
                f"{confidence}%"
            )

            if confidence >= 85:
                confidence_label = "High Confidence"
            elif confidence >= 70:
                confidence_label = "Moderate Confidence"
            else:
                confidence_label = "Developing Confidence"

            st.caption(confidence_label)

            recent_miles = vekdyn_prediction.get("weekly_miles")
            if recent_miles is not None:
                st.caption(f"Recent volume: {recent_miles:.1f} mi/wk")

        with factors_col:
            st.write("**Performance Factors**")

            st.progress(
                vekdyn_prediction.get("aerobic_score", 0) / 100,
                text="Aerobic Fitness"
            )

            if vekdyn_prediction.get("is_female"):
                st.progress(
                    vekdyn_prediction.get("threshold_score", 50) / 100,
                    text="Threshold — calibration pending"
                )
            else:
                st.progress(
                    vekdyn_prediction.get("threshold_score", 0) / 100,
                    text="Threshold Fitness"
                )

            st.progress(
                vekdyn_prediction.get("speed_score", 0) / 100,
                text="Speed Reserve"
            )

            st.progress(
                vekdyn_prediction.get("elasticity_score", 0) / 100,
                text="Elasticity Preservation"
            )

            st.caption(
                f"Volume compatibility: {vekdyn_prediction.get('volume_label', 'Unknown')}"
            )

# =========================================================
# TEAM WORKOUTS — WEEKLY COACH PLANNER + NEON
# =========================================================
# The coach planner uses a Sunday-Saturday grid like a traditional training
# spreadsheet while keeping every session in Neon. Existing VEKDYN Athlete
# reads remain compatible because the original workout columns are preserved.

WORKOUT_TYPES = [
    "Easy Run", "Recovery", "Long Run", "Threshold", "Intervals",
    "Hills", "Race / Time Trial", "Strength", "Rest", "Other",
]

SESSION_SLOTS = ["AM", "PM"]


def initialize_workouts_database():
    """Create/upgrade persistent team/athlete workout storage in Neon."""
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS team_workouts (
                    id BIGSERIAL PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    athlete_key TEXT,
                    workout_date DATE NOT NULL,
                    workout_type TEXT NOT NULL,
                    warm_up TEXT,
                    workout TEXT NOT NULL,
                    cool_down TEXT,
                    notes TEXT,
                    created_by TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS team_workouts_lookup_idx
                ON team_workouts (team_id, workout_date, athlete_key)
                """
            )
            cursor.execute(
                """
                ALTER TABLE team_workouts
                ADD COLUMN IF NOT EXISTS video_url TEXT
                """
            )
            cursor.execute(
                """
                ALTER TABLE team_workouts
                ADD COLUMN IF NOT EXISTS session_slot TEXT DEFAULT 'AM'
                """
            )
            cursor.execute(
                """
                ALTER TABLE team_workouts
                ADD COLUMN IF NOT EXISTS effort_level TEXT
                """
            )
            cursor.execute(
                """
                ALTER TABLE team_workouts
                ADD COLUMN IF NOT EXISTS planned_miles REAL
                """
            )
            cursor.execute(
                """
                UPDATE team_workouts
                SET session_slot = 'AM'
                WHERE session_slot IS NULL OR BTRIM(session_slot) = ''
                """
            )
        database.commit()


def save_team_workout(
        team_id,
        athlete_key,
        workout_date,
        workout_type,
        warm_up,
        workout,
        cool_down,
        notes,
        video_url="",
        session_slot="AM",
        effort_level="",
        planned_miles=None,
):
    """Save one coach-written session. athlete_key=None means entire team."""
    main_workout = str(workout).strip()
    clean_video_url = str(video_url or "").strip()
    clean_session = str(session_slot or "AM").strip().upper()
    clean_effort = str(effort_level or "").strip()

    if team_id != "dark_horse_endurance":
        clean_video_url = ""

    if not main_workout:
        raise ValueError("Add the main workout before saving.")

    if clean_session not in SESSION_SLOTS:
        clean_session = "AM"

    if planned_miles in (None, ""):
        clean_miles = None
    else:
        clean_miles = float(planned_miles)
        if clean_miles < 0:
            raise ValueError("Planned mileage cannot be negative.")

    initialize_workouts_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO team_workouts (
                    team_id, athlete_key, workout_date, workout_type,
                    warm_up, workout, cool_down, notes, video_url, created_by,
                    session_slot, effort_level, planned_miles
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    team_id,
                    athlete_key,
                    workout_date,
                    str(workout_type).strip(),
                    str(warm_up).strip(),
                    main_workout,
                    str(cool_down).strip(),
                    str(notes).strip(),
                    clean_video_url,
                    st.session_state.get("logged_in_user", "Coach"),
                    clean_session,
                    clean_effort,
                    clean_miles,
                ),
            )
        database.commit()


def _workout_rows_to_dicts(rows):
    return [
        {
            "id": row[0],
            "athlete_key": row[1],
            "Date": row[2],
            "Type": row[3],
            "Warm Up": row[4] or "",
            "Workout": row[5] or "",
            "Cool Down": row[6] or "",
            "Notes": row[7] or "",
            "Video URL": row[8] or "",
            "Session": (row[9] or "AM").upper(),
            "Effort": row[10] or "",
            "Planned Miles": row[11],
        }
        for row in rows
    ]


def load_team_workouts(team_id, selected_athlete_key=None, limit=12):
    """Load upcoming team sessions plus sessions assigned to one athlete."""
    initialize_workouts_database()
    today = datetime.now(TEAM_TIMEZONE).date()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            if selected_athlete_key:
                cursor.execute(
                    """
                    SELECT id, athlete_key, workout_date, workout_type, warm_up,
                           workout, cool_down, notes, video_url,
                           COALESCE(session_slot, 'AM'), effort_level, planned_miles
                    FROM team_workouts
                    WHERE team_id = %s
                      AND workout_date >= %s
                      AND (athlete_key IS NULL OR athlete_key = %s)
                    ORDER BY workout_date ASC,
                             CASE WHEN COALESCE(session_slot, 'AM') = 'AM' THEN 0 ELSE 1 END,
                             id ASC
                    LIMIT %s
                    """,
                    (team_id, today, selected_athlete_key, int(limit)),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, athlete_key, workout_date, workout_type, warm_up,
                           workout, cool_down, notes, video_url,
                           COALESCE(session_slot, 'AM'), effort_level, planned_miles
                    FROM team_workouts
                    WHERE team_id = %s
                      AND workout_date >= %s
                      AND athlete_key IS NULL
                    ORDER BY workout_date ASC,
                             CASE WHEN COALESCE(session_slot, 'AM') = 'AM' THEN 0 ELSE 1 END,
                             id ASC
                    LIMIT %s
                    """,
                    (team_id, today, int(limit)),
                )
            rows = cursor.fetchall()

    return _workout_rows_to_dicts(rows)


def load_team_workouts_range(team_id, start_date, end_date, selected_athlete_key=None):
    """Load a complete Sunday-Saturday planning window for the selected athlete."""
    initialize_workouts_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            if selected_athlete_key:
                cursor.execute(
                    """
                    SELECT id, athlete_key, workout_date, workout_type, warm_up,
                           workout, cool_down, notes, video_url,
                           COALESCE(session_slot, 'AM'), effort_level, planned_miles
                    FROM team_workouts
                    WHERE team_id = %s
                      AND workout_date BETWEEN %s AND %s
                      AND (athlete_key IS NULL OR athlete_key = %s)
                    ORDER BY workout_date ASC,
                             CASE WHEN COALESCE(session_slot, 'AM') = 'AM' THEN 0 ELSE 1 END,
                             id ASC
                    """,
                    (team_id, start_date, end_date, selected_athlete_key),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, athlete_key, workout_date, workout_type, warm_up,
                           workout, cool_down, notes, video_url,
                           COALESCE(session_slot, 'AM'), effort_level, planned_miles
                    FROM team_workouts
                    WHERE team_id = %s
                      AND workout_date BETWEEN %s AND %s
                      AND athlete_key IS NULL
                    ORDER BY workout_date ASC,
                             CASE WHEN COALESCE(session_slot, 'AM') = 'AM' THEN 0 ELSE 1 END,
                             id ASC
                    """,
                    (team_id, start_date, end_date),
                )
            rows = cursor.fetchall()

    return _workout_rows_to_dicts(rows)


def delete_team_workout(workout_id, team_id):
    """Delete one workout while keeping school data isolated."""
    initialize_workouts_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                "DELETE FROM team_workouts WHERE id = %s AND team_id = %s",
                (int(workout_id), team_id),
            )
        database.commit()


def workout_value(value, fallback="—"):
    value = "" if value is None else str(value).strip()
    return fallback if not value or value.lower() == "nan" else value


def _session_description(workout):
    """Compact description for one spreadsheet-style weekly cell."""
    pieces = []
    warmup = workout_value(workout.get("Warm Up"), "")
    main = workout_value(workout.get("Workout"), "")
    cooldown = workout_value(workout.get("Cool Down"), "")
    notes = workout_value(workout.get("Notes"), "")

    if warmup:
        pieces.append(f"WU: {warmup}")
    if main:
        pieces.append(main)
    if cooldown:
        pieces.append(f"CD: {cooldown}")
    if notes:
        pieces.append(f"Note: {notes}")

    return " | ".join(pieces) if pieces else "—"


def _weekly_workout_matrix(workouts, week_start):
    """Build the Sunday-Saturday matrix shown to the coach."""
    dates = [week_start + timedelta(days=i) for i in range(7)]
    by_day_slot = {(day, slot): [] for day in dates for slot in SESSION_SLOTS}

    for workout in workouts:
        day = workout.get("Date")
        if hasattr(day, "date"):
            day = day.date()
        slot = str(workout.get("Session") or "AM").upper()
        if slot not in SESSION_SLOTS:
            slot = "AM"
        if (day, slot) in by_day_slot:
            by_day_slot[(day, slot)].append(workout)

    def join_for(day, slot, field):
        sessions = by_day_slot[(day, slot)]
        if not sessions:
            return "—"
        if field == "title":
            return " / ".join(workout_value(item.get("Type"), "Training") for item in sessions)
        if field == "description":
            return " || ".join(_session_description(item) for item in sessions)
        if field == "effort":
            values = [workout_value(item.get("Effort"), "") for item in sessions]
            values = [value for value in values if value]
            return " / ".join(values) if values else "—"
        return "—"

    columns = [day.strftime("%a\n%b %-d") for day in dates]
    rows = {
        "AM Session": [join_for(day, "AM", "title") for day in dates],
        "PM Session": [join_for(day, "PM", "title") for day in dates],
        "AM Description": [join_for(day, "AM", "description") for day in dates],
        "PM Description": [join_for(day, "PM", "description") for day in dates],
        "AM Effort": [join_for(day, "AM", "effort") for day in dates],
        "PM Effort": [join_for(day, "PM", "effort") for day in dates],
    }

    daily_miles = []
    for day in dates:
        miles = [
            float(item["Planned Miles"])
            for slot in SESSION_SLOTS
            for item in by_day_slot[(day, slot)]
            if item.get("Planned Miles") is not None
        ]
        daily_miles.append(round(sum(miles), 1) if miles else None)

    rows["Total Day Mileage"] = [
        f"{value:g}" if value is not None else "—"
        for value in daily_miles
    ]

    matrix = pd.DataFrame(rows, index=columns).T
    weekly_total = round(sum(value for value in daily_miles if value is not None), 1)
    has_mileage = any(value is not None for value in daily_miles)
    return matrix, (weekly_total if has_mileage else None)


def render_team_workout_card(workout, athlete_lookup):
    """Detailed saved-session card used inside the management expander."""
    workout_date = pd.Timestamp(workout["Date"])
    date_label = workout_date.strftime("%a, %b %d")
    assigned_key = workout.get("athlete_key")
    assigned_name = (
        athlete_lookup.get(assigned_key, {}).get("profile", {}).get("name", assigned_key)
        if assigned_key else "Entire Team"
    )

    with st.container(border=True):
        st.caption(f"{date_label} · {workout.get('Session', 'AM')}")
        st.markdown(f"### {workout_value(workout.get('Type'), 'Team Training')}")
        st.caption(f"Assigned to: {assigned_name}")
        if workout.get("Planned Miles") is not None:
            st.caption(f"Planned mileage: {float(workout['Planned Miles']):g} mi")
        if workout_value(workout.get("Effort"), ""):
            st.caption(f"Effort: {workout_value(workout.get('Effort'), '')}")
        st.markdown(f"**Warm-up:** {workout_value(workout.get('Warm Up'))}")
        st.markdown(f"**Workout:** {workout_value(workout.get('Workout'))}")
        st.markdown(f"**Cool-down:** {workout_value(workout.get('Cool Down'))}")
        if workout_value(workout.get("Notes"), ""):
            st.caption(f"Coach notes: {workout_value(workout.get('Notes'), '')}")
        if active_team == "dark_horse_endurance" and workout_value(workout.get("Video URL"), ""):
            st.markdown("**Coach video:**")
            st.video(workout_value(workout.get("Video URL"), ""))
        if st.button("Delete", key=f"delete_workout_{workout['id']}"):
            delete_team_workout(workout["id"], active_team)
            st.rerun()


def render_team_workouts():
    """Sunday-Saturday coach planner with AM/PM sessions and weekly mileage."""
    st.markdown(
        '<div class="team-workout-title">Weekly Training Plan</div>'
        '<div class="team-workout-subtitle">A full week at a glance — AM/PM sessions, effort and planned mileage.</div>',
        unsafe_allow_html=True,
    )

    team_athletes = get_team_athletes(active_team)
    athlete_names = {
        key: value.get("profile", {}).get("name", key)
        for key, value in team_athletes.items()
    }
    name_to_key = {name: key for key, name in athlete_names.items()}

    today = datetime.now(TEAM_TIMEZONE).date()
    current_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    week_state_key = f"coach_workout_week_offset_{active_team}_{athlete_key}"
    if week_state_key not in st.session_state:
        st.session_state[week_state_key] = 0

    nav_left, nav_mid, nav_right = st.columns([1, 1, 1])
    with nav_left:
        if st.button("← Previous week", key=f"prev_workout_week_{active_team}_{athlete_key}", use_container_width=True):
            st.session_state[week_state_key] -= 1
            st.rerun()
    with nav_mid:
        if st.button("This week", key=f"this_workout_week_{active_team}_{athlete_key}", use_container_width=True):
            st.session_state[week_state_key] = 0
            st.rerun()
    with nav_right:
        if st.button("Next week →", key=f"next_workout_week_{active_team}_{athlete_key}", use_container_width=True):
            st.session_state[week_state_key] += 1
            st.rerun()

    week_start = current_sunday + timedelta(weeks=int(st.session_state[week_state_key]))
    week_end = week_start + timedelta(days=6)

    title_left, title_right = st.columns([3, 1])
    with title_left:
        st.markdown(
            f"### {week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
        )
        st.caption(
            "Showing team-wide sessions plus individual sessions for "
            f"{athlete_names.get(athlete_key, 'the selected athlete')}."
        )

    with st.expander("+ Write a workout", expanded=False):
        assignment_options = ["Entire Team"] + sorted(name_to_key.keys())
        with st.form(f"workout_form_{active_team}_{athlete_key}", clear_on_submit=True):
            top_left, top_mid, top_right, top_slot = st.columns([1.4, 1, 1.1, .7])
            with top_left:
                assignment = st.selectbox("Assign to", assignment_options)
            with top_mid:
                workout_date = st.date_input("Date", value=week_start)
            with top_right:
                workout_type = st.selectbox("Workout type", WORKOUT_TYPES)
            with top_slot:
                session_slot = st.selectbox("Session", SESSION_SLOTS)

            effort_col, miles_col = st.columns(2)
            with effort_col:
                effort_level = st.selectbox(
                    "Effort (1–10)",
                    ["—"] + [str(value) for value in range(1, 11)],
                    help="Optional coach target. Use 1 for very easy and 10 for maximal.",
                )
            with miles_col:
                planned_miles = st.number_input(
                    "Planned mileage",
                    min_value=0.0,
                    max_value=50.0,
                    value=0.0,
                    step=0.5,
                    help="Use 0 if you do not want mileage included for this session.",
                )

            warm_up = st.text_area("Warm Up", placeholder="Example: 2 miles easy + drills")
            main_workout = st.text_area(
                "Main Workout",
                placeholder="Example: 5 × 6 min LT, 1 min recovery",
            )
            cool_down = st.text_area("Cool Down", placeholder="Example: 2 miles easy")
            notes = st.text_area("Coach Notes", placeholder="Optional cues, targets, or instructions")

            if active_team == "dark_horse_endurance":
                video_url = st.text_input(
                    "Coach Video URL",
                    placeholder="Paste a YouTube or supported video link (optional)",
                    help="Video publishing is enabled only for the Dark Horse Endurance coach workspace.",
                )
            else:
                video_url = ""

            submitted = st.form_submit_button(
                "Save Workout", type="primary", use_container_width=True
            )

        if submitted:
            assigned_key = None if assignment == "Entire Team" else name_to_key[assignment]
            try:
                save_team_workout(
                    active_team,
                    assigned_key,
                    workout_date,
                    workout_type,
                    warm_up,
                    main_workout,
                    cool_down,
                    notes,
                    video_url,
                    session_slot=session_slot,
                    effort_level="" if effort_level == "—" else effort_level,
                    planned_miles=None if planned_miles == 0 else planned_miles,
                )
                st.success("Workout saved to VEKDYN.")
                st.rerun()
            except (ValueError, psycopg2.Error) as error:
                st.warning(str(error))

    try:
        workouts = load_team_workouts_range(
            active_team,
            week_start,
            week_end,
            athlete_key,
        )
    except Exception as error:
        st.warning(f"VEKDYN could not load workouts: {error}")
        return

    matrix, weekly_miles = _weekly_workout_matrix(workouts, week_start)

    with title_right:
        if weekly_miles is None:
            st.metric("Planned Week", "— mi")
        else:
            st.metric("Planned Week", f"{weekly_miles:g} mi")

    # Spreadsheet-style overview. Long descriptions get extra row height instead
    # of forcing the coach to open seven separate cards.
    st.dataframe(
        matrix,
        use_container_width=True,
        height=500,
        row_height=62,
    )

    if not workouts:
        st.info("No sessions are saved for this week yet.")
        return

    with st.expander("Manage saved sessions", expanded=False):
        for start_index in range(0, len(workouts), 3):
            row = workouts[start_index:start_index + 3]
            columns = st.columns(3, gap="medium")
            for column, workout in zip(columns, row):
                with column:
                    render_team_workout_card(workout, team_athletes)


if dashboard_view in {"Dashboard", "Training"}:
    render_team_workouts()


# =========================================================
# THRESHOLD LACTATE PROFILE — NEON
# =========================================================

def initialize_threshold_database():
    """Create persistent athlete threshold profile storage in Neon."""
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_threshold_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    athlete_key TEXT NOT NULL,
                    short_lactate REAL,
                    short_pace TEXT,
                    medium_lactate REAL,
                    medium_pace TEXT,
                    long_lactate REAL,
                    long_pace TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(team_id, athlete_key)
                )
                """
            )
        database.commit()


def save_threshold_profile(
        team_id,
        athlete_key,
        short_lactate,
        short_pace,
        medium_lactate,
        medium_pace,
        long_lactate,
        long_pace,
):
    """Save or update one athlete's threshold profile in Neon."""
    initialize_threshold_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO athlete_threshold_profiles (
                    team_id, athlete_key,
                    short_lactate, short_pace,
                    medium_lactate, medium_pace,
                    long_lactate, long_pace
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (team_id, athlete_key)
                DO UPDATE SET
                    short_lactate = EXCLUDED.short_lactate,
                    short_pace = EXCLUDED.short_pace,
                    medium_lactate = EXCLUDED.medium_lactate,
                    medium_pace = EXCLUDED.medium_pace,
                    long_lactate = EXCLUDED.long_lactate,
                    long_pace = EXCLUDED.long_pace,
                    updated_at = NOW()
                """,
                (
                    team_id, athlete_key,
                    short_lactate, short_pace,
                    medium_lactate, medium_pace,
                    long_lactate, long_pace,
                ),
            )
        database.commit()


def load_threshold_profile(team_id, athlete_key):
    """Load one athlete's saved threshold profile from Neon."""
    initialize_threshold_database()

    if not athlete_key:
        return {}

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    short_lactate, short_pace,
                    medium_lactate, medium_pace,
                    long_lactate, long_pace
                FROM athlete_threshold_profiles
                WHERE team_id = %s AND athlete_key = %s
                LIMIT 1
                """,
                (team_id, athlete_key),
            )
            row = cursor.fetchone()

    if not row:
        return {}

    return {
        "short_reps": {"lactate": row[0], "pace": row[1] or "--"},
        "medium_reps": {"lactate": row[2], "pace": row[3] or "--"},
        "long_reps": {"lactate": row[4], "pace": row[5] or "--"},
    }


# =========================================================
# THRESHOLD LACTATE PROFILE — DISPLAY + COACH EDITOR
# =========================================================

if dashboard_view in {"Dashboard", "Performance"}:
    st.subheader("Threshold Lactate Profile")

    # Neon is the current source after a coach saves a profile.
    # Until then, keep showing the original CSV threshold values.
    try:
        saved_threshold = load_threshold_profile(active_team, athlete_key)
    except Exception as error:
        saved_threshold = {}
        st.warning(f"VEKDYN could not load saved threshold data: {error}")

    threshold = saved_threshold or athlete.get("threshold", {})

    short_data = threshold.get("short_reps", {}) or {}
    medium_data = threshold.get("medium_reps", {}) or {}
    long_data = threshold.get("long_reps", {}) or {}


    def threshold_display_lactate(rep_data):
        value = rep_data.get("lactate")
        if value in (None, "", "--"):
            return "--"
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return str(value)


    def threshold_editor_lactate(rep_data):
        value = rep_data.get("lactate")
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


    def threshold_editor_pace(rep_data):
        value = rep_data.get("pace", "")
        return "" if value in (None, "--") else str(value)


    # -----------------------------------------------------
    # ORIGINAL THREE-CARD THRESHOLD DISPLAY
    # -----------------------------------------------------
    with st.container(border=True):
        short_col, medium_col, long_col = st.columns(3)

        with short_col:
            st.metric(
                "Short Reps (1–3 min)",
                f"{threshold_display_lactate(short_data)} mmol",
            )
            st.caption(short_data.get("pace") or "--")

        with medium_col:
            st.metric(
                "Medium Reps (5–10 min)",
                f"{threshold_display_lactate(medium_data)} mmol",
            )
            st.caption(medium_data.get("pace") or "--")

        with long_col:
            st.metric(
                "Long Reps (10–15 min)",
                f"{threshold_display_lactate(long_data)} mmol",
            )
            st.caption(long_data.get("pace") or "--")

    # -----------------------------------------------------
    # COACH EDITOR — COLLAPSED UNTIL NEEDED
    # -----------------------------------------------------
    with st.expander("Edit Threshold Profile", expanded=False):
        st.caption(
            "Update this athlete's threshold values. Saved values persist in Neon."
        )

        with st.form(
                f"threshold_profile_form_{active_team}_{athlete_key}",
                clear_on_submit=False,
        ):
            short_col, medium_col, long_col = st.columns(3)

            with short_col:
                st.markdown("**Short Reps (1–3 min)**")
                short_lactate = st.number_input(
                    "Lactate (mmol)",
                    min_value=0.0,
                    max_value=20.0,
                    value=threshold_editor_lactate(short_data),
                    step=0.1,
                    format="%.1f",
                    key=f"short_lactate_{active_team}_{athlete_key}",
                )
                short_pace = st.text_input(
                    "Pace",
                    value=threshold_editor_pace(short_data),
                    placeholder="Example: 5:00/mi",
                    key=f"short_pace_{active_team}_{athlete_key}",
                )

            with medium_col:
                st.markdown("**Medium Reps (5–10 min)**")
                medium_lactate = st.number_input(
                    "Lactate (mmol)",
                    min_value=0.0,
                    max_value=20.0,
                    value=threshold_editor_lactate(medium_data),
                    step=0.1,
                    format="%.1f",
                    key=f"medium_lactate_{active_team}_{athlete_key}",
                )
                medium_pace = st.text_input(
                    "Pace",
                    value=threshold_editor_pace(medium_data),
                    placeholder="Example: 5:10/mi",
                    key=f"medium_pace_{active_team}_{athlete_key}",
                )

            with long_col:
                st.markdown("**Long Reps (10–15 min)**")
                long_lactate = st.number_input(
                    "Lactate (mmol)",
                    min_value=0.0,
                    max_value=20.0,
                    value=threshold_editor_lactate(long_data),
                    step=0.1,
                    format="%.1f",
                    key=f"long_lactate_{active_team}_{athlete_key}",
                )
                long_pace = st.text_input(
                    "Pace",
                    value=threshold_editor_pace(long_data),
                    placeholder="Example: 5:20/mi",
                    key=f"long_pace_{active_team}_{athlete_key}",
                )

            save_threshold = st.form_submit_button(
                "Save Threshold Profile",
                type="primary",
                use_container_width=True,
            )

        if save_threshold:
            try:
                save_threshold_profile(
                    active_team,
                    athlete_key,
                    short_lactate,
                    short_pace.strip(),
                    medium_lactate,
                    medium_pace.strip(),
                    long_lactate,
                    long_pace.strip(),
                )
                st.success("Threshold profile saved.")
                st.rerun()
            except Exception as error:
                st.error(f"VEKDYN could not save the threshold profile: {error}")

# =========================================================
# ATHLETE ACCESS & CONNECTIONS — COLLAPSED ADMIN CONTROLS
# =========================================================

if dashboard_view in {"Dashboard", "Performance"}:
    st.divider()
    with st.expander("Athlete Access & Connections", expanded=False):
        st.caption(
            "Manage this athlete's VEKDYN login and review the connected training account. "
            "These controls stay collapsed during normal coaching use."
        )

        # Keep the existing secure account workflow. Readable temporary passwords
        # only appear immediately after Create/Reset and disappear when cleared.
        render_athlete_account_manager(
            athlete_key=athlete_key,
            profile=profile,
            team_id=active_team,
        )

        st.divider()
        st.markdown("#### Training Connection")

        try:
            coach_strava_connection = athlete_strava_connection(athlete_key)
        except Exception as error:
            coach_strava_connection = {}
            st.warning(f"VEKDYN could not load this athlete's Strava connection: {error}")

        if coach_strava_connection:
            connected_name = coach_strava_connection.get("strava_name") or athlete_name
            st.success(f"Strava connected — {connected_name}")
            st.caption(
                "Training data is stored through the athlete's VEKDYN connection and is available to the coach dashboard."
            )
        else:
            st.info("Strava not connected")
            st.caption(
                "The athlete can authorize Strava from VEKDYN Athlete. Coach-side connection is only needed as an administrative fallback."
            )