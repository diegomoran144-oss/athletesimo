import hmac
import hashlib
import base64
import json
import time
import bcrypt
import psycopg2
import streamlit as st
from pathlib import Path

# =========================================================
# VEKDYN — UNIFIED ENTRY POINT
# One URL -> one login -> coach or athlete workspace
#
# Keep these two existing files beside this file in Git:
#   coach_hub.py
#   athlete_hub.py
#
# Rename your current files:
#   athelete_run(...).py -> coach_hub.py
#   main(...).py         -> athlete_hub.py
# =========================================================

COACH_APP = Path(__file__).with_name("coach_hub.py")
ATHLETE_APP = Path(__file__).with_name("athlete_hub.py")

TEAM_CONFIG = {
    "ollu_distance": "OLLU Distance",
    "sam_houston": "Sam Houston Distance",
    "dark_horse_endurance": "Dark Horse Endurance",
}


# =========================================================
# UNIFIED BROWSER SESSION
# Keeps the selected role/account after a browser refresh.
# =========================================================

UNIFIED_SESSION_DAYS = 30


def unified_session_secret():
    try:
        return str(st.secrets["UNIFIED_SESSION_SECRET"])
    except (KeyError, FileNotFoundError):
        # Reuse an existing private Streamlit secret if a dedicated one has
        # not been added yet. DATABASE_URL is never exposed to the browser;
        # it is only used server-side as HMAC key material.
        return str(st.secrets["DATABASE_URL"])


def encode_unified_session(user):
    payload = {
        "role": user["role"],
        "team_id": user.get("team_id"),
        "username": user.get("username"),
        "athlete_id": user.get("athlete_id"),
        "athlete_key": user.get("athlete_key"),
        "must_change_password": bool(user.get("must_change_password", False)),
        "exp": int(time.time()) + (UNIFIED_SESSION_DAYS * 86400),
    }

    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(
        unified_session_secret().encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    return f"{encoded}.{signature}"


def decode_unified_session(token):
    try:
        encoded, supplied_signature = str(token).split(".", 1)
        expected_signature = hmac.new(
            unified_session_secret().encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None

        padded = encoded + ("=" * (-len(encoded) % 4))
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )

        if int(payload.get("exp", 0)) < int(time.time()):
            return None

        if payload.get("role") not in {"coach", "athlete"}:
            return None

        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def clear_unified_login():
    keys_to_clear = [
        "vekdyn_authenticated_user",
        "vekdyn_role",
        "logged_in",
        "logged_in_user",
        "active_team",
        "pending_team",
        "page",
        "athlete_id",
        "password_change_required",
    ]

    for key in keys_to_clear:
        st.session_state.pop(key, None)

    st.query_params.clear()


def handle_global_logout():
    # Coach Hub's old logout can send the browser back here with ?logout=1.
    if st.query_params.get("logout") == "1":
        clear_unified_login()
        st.rerun()


# =========================================================
# DATABASE
# =========================================================

def get_database_connection():
    return psycopg2.connect(st.secrets["DATABASE_URL"])


# =========================================================
# ATHLETE AUTHENTICATION
# Uses the athlete_logins table you already have in Neon.
# =========================================================

def authenticate_athlete(username, password):
    clean_username = str(username).strip().lower()
    clean_password = str(password).strip()

    if not clean_username or not clean_password:
        return None

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    athlete_id,
                    athlete_key,
                    team_id,
                    password_hash,
                    active,
                    COALESCE(must_change_password, FALSE)
                FROM athlete_logins
                WHERE LOWER(TRIM(athlete_id)) = %s
                LIMIT 1
                """,
                (clean_username,),
            )
            row = cursor.fetchone()

    if not row:
        return None

    athlete_id, athlete_key, team_id, password_hash, active, must_change = row

    if not active:
        return None

    if isinstance(password_hash, str):
        password_hash = password_hash.encode("utf-8")

    try:
        matches = bcrypt.checkpw(
            clean_password.encode("utf-8"),
            password_hash,
        )
    except ValueError:
        return None

    if not matches:
        return None

    return {
        "role": "athlete",
        "athlete_id": athlete_id,
        "athlete_key": athlete_key or athlete_id,
        "team_id": team_id,
        "must_change_password": bool(must_change),
    }


# =========================================================
# COACH AUTHENTICATION
# Searches all configured team accounts automatically.
# The coach no longer has to choose a school first.
# =========================================================

def authenticate_coach(username, password):
    clean_username = str(username).strip()
    clean_password = str(password).strip()

    if not clean_username or not clean_password:
        return None

    try:
        team_logins = st.secrets["team_logins"]
    except (KeyError, FileNotFoundError):
        return None

    for team_id in TEAM_CONFIG:
        try:
            account = team_logins[team_id]
            correct_username = str(account["username"])
            correct_password = str(account["password"])
        except (KeyError, TypeError):
            continue

        username_matches = hmac.compare_digest(
            clean_username,
            correct_username,
        )
        password_matches = hmac.compare_digest(
            clean_password,
            correct_password,
        )

        if username_matches and password_matches:
            return {
                "role": "coach",
                "username": correct_username,
                "team_id": team_id,
            }

    return None


# =========================================================
# ONE AUTHENTICATION FUNCTION
#
# This is the idea you were learning:
# def packages the job,
# if makes the decision,
# dictionaries carry the result.
# =========================================================

def authenticate_user(username, password):
    coach = authenticate_coach(username, password)

    if coach:
        return coach

    athlete = authenticate_athlete(username, password)

    if athlete:
        return athlete

    return None


# =========================================================
# HAND OFF TO EXISTING APPS
# =========================================================

def run_existing_app(path):
    if not path.exists():
        st.error(f"VEKDYN could not find {path.name}.")
        st.stop()

    source = path.read_text(encoding="utf-8")

    # The selected hub executes as the Streamlit app after authentication.
    exec(
        compile(source, str(path), "exec"),
        {
            "__name__": "__main__",
            "__file__": str(path),
        },
    )


def open_coach_hub(user):
    # Match the session-state names already expected by your Coach Hub.
    st.session_state["logged_in"] = True
    st.session_state["logged_in_user"] = user["username"]
    st.session_state["active_team"] = user["team_id"]
    st.session_state["pending_team"] = None
    st.session_state["page"] = "dashboard"
    st.session_state["vekdyn_role"] = "coach"

    run_existing_app(COACH_APP)


def open_athlete_hub(user):
    # Match the session-state names already expected by your Athlete Hub.
    st.session_state["logged_in"] = True
    st.session_state["athlete_id"] = user["athlete_id"]
    st.session_state["password_change_required"] = user["must_change_password"]
    st.session_state["vekdyn_role"] = "athlete"

    run_existing_app(ATHLETE_APP)


# =========================================================
# SESSION
# =========================================================

if "vekdyn_authenticated_user" not in st.session_state:
    st.session_state["vekdyn_authenticated_user"] = None

handle_global_logout()

# A hard refresh creates a new Streamlit session. Restore the unified account
# from the signed browser/query token before deciding whether to show login.
if not st.session_state.get("vekdyn_authenticated_user"):
    saved_unified_token = st.query_params.get("vekdyn_session")
    if saved_unified_token:
        restored_user = decode_unified_session(saved_unified_token)
        if restored_user:
            st.session_state["vekdyn_authenticated_user"] = restored_user
        else:
            st.query_params.clear()


# =========================================================
# ALREADY LOGGED IN -> ROUTE IMMEDIATELY
# =========================================================

current_user = st.session_state.get("vekdyn_authenticated_user")

if current_user:
    if current_user["role"] == "coach":
        open_coach_hub(current_user)
        st.stop()

    if current_user["role"] == "athlete":
        open_athlete_hub(current_user)
        st.stop()


# =========================================================
# UNIFIED LOGIN SCREEN
# =========================================================

st.set_page_config(
    page_title="VEKDYN",
    page_icon="🏃",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        .stApp {
            background: #f6f8f6;
            color: #111827;
        }

        [data-testid="stSidebar"],
        [data-testid="collapsedControl"],
        [data-testid="stSidebarCollapsedControl"],
        #MainMenu,
        footer {
            display: none !important;
        }

        .block-container {
            max-width: 520px;
            padding-top: 5rem;
        }

        .vekdyn-brand {
            text-align: center;
            font-size: 42px;
            font-weight: 850;
            letter-spacing: 1px;
            margin-bottom: 0;
        }

        .vekdyn-brand span {
            color: #2f9e44;
        }

        .vekdyn-tagline {
            text-align: center;
            color: #6b7280;
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 2.2px;
            margin-top: 2px;
            margin-bottom: 44px;
        }

        .login-title {
            text-align: center;
            font-size: 30px;
            font-weight: 800;
            margin-bottom: 4px;
        }

        .login-copy {
            text-align: center;
            color: #6b7280;
            margin-bottom: 25px;
        }

        div[data-testid="stForm"] {
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 16px;
            padding: 26px;
            box-shadow: 0 3px 14px rgba(0,0,0,.04);
        }

        .stButton button,
        div[data-testid="stFormSubmitButton"] button {
            min-height: 46px;
            font-weight: 750;
        }

        /* Runner hero image already stored in team_images/landing_page.jpg.
           The image itself is injected below as a base64 background. */
        .stApp {
            background-size: cover !important;
            background-position: center center !important;
            background-attachment: fixed !important;
        }

        .stApp::before {
            content: "";
            position: fixed;
            inset: 0;
            background: rgba(8, 18, 13, .36);
            z-index: 0;
            pointer-events: none;
        }

        .block-container {
            position: relative;
            z-index: 1;
            padding-top: 7rem;
        }

        .vekdyn-brand,
        .vekdyn-tagline,
        .login-title {
            background: rgba(255,255,255,.96);
        }

        .vekdyn-brand {
            padding-top: 24px;
            border-radius: 18px 18px 0 0;
            margin-bottom: 0;
        }

        .vekdyn-tagline {
            margin: 0;
            padding: 4px 24px 25px 24px;
        }

        .login-title {
            padding: 0 24px 20px 24px;
            margin-bottom: 0;
        }

        div[data-testid="stForm"] {
            border-radius: 0 0 18px 18px;
            border-top: 0;
            box-shadow: 0 12px 38px rgba(0,0,0,.22);
        }

    </style>
    """,
    unsafe_allow_html=True,
)

# Use the existing VEKDYN runner/landing image as the login background.
landing_image = Path(__file__).with_name("team_images") / "landing_page.jpg"
if landing_image.exists():
    image_b64 = base64.b64encode(landing_image.read_bytes()).decode("ascii")
    st.markdown(
        f"""
        <style>
            .stApp {{
                background-image:
                    linear-gradient(rgba(8,18,13,.10), rgba(8,18,13,.10)),
                    url("data:image/jpeg;base64,{image_b64}") !important;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )

# =========================================================
# PUBLIC PROGRAMS / PRICING
# =========================================================
pricing_mode = st.query_params.get("pricing") == "1"

st.markdown(
    """
    <style>
        [data-testid="stToolbar"] { visibility: hidden !important; }
        .program-pricing-button, .back-login-button {
            position: fixed; top: 15px; right: 22px; z-index: 999999;
            display: inline-flex; align-items: center; justify-content: center;
            min-height: 36px; padding: 0 16px; background: #ffffff;
            color: #111827 !important; border: 1px solid #d1d5db;
            border-radius: 9px; font-size: 14px; font-weight: 750;
            text-decoration: none !important; box-shadow: 0 1px 4px rgba(0,0,0,.10);
        }
        .pricing-shell {
            background: rgba(255,255,255,.97); border-radius: 20px;
            padding: 34px 30px; box-shadow: 0 12px 38px rgba(0,0,0,.22);
        }
        .pricing-eyebrow { color:#2f9e44; font-size:13px; font-weight:850; letter-spacing:1.8px; text-transform:uppercase; }
        .pricing-title { color:#111827; font-size:34px; font-weight:850; line-height:1.12; margin:8px 0 10px; }
        .pricing-copy { color:#6b7280; font-size:16px; line-height:1.6; margin-bottom:24px; }
        .pricing-card { border:1px solid #e5e7eb; border-radius:14px; padding:22px; margin-top:14px; background:#fff; }
        .pricing-card h3 { color:#111827; margin:0 0 8px; font-size:20px; }
        .pricing-card p { color:#4b5563; margin:0; line-height:1.55; }
    </style>
    """,
    unsafe_allow_html=True,
)

if pricing_mode:
    st.markdown(
        """
        <a class="back-login-button" href="?" target="_self">Back to Sign In</a>
        <div class="pricing-shell">
            <div class="pricing-eyebrow">VEKDYN FOR PROGRAMS</div>
            <div class="pricing-title">Build a stronger distance program with better data.</div>
            <div class="pricing-copy">VEKDYN gives coaches one place to organize training,
            monitor athlete development, review performance and recovery information,
            and keep athletes connected to the plan.</div>
            <div class="pricing-card"><h3>Program access</h3><p>Coach dashboard, athlete accounts,
            training calendars, workout delivery, threshold tracking, performance tools,
            recovery data, and supported athlete-data integrations.</p></div>
            <div class="pricing-card"><h3>Pilot / pricing</h3><p>Program pricing is set up directly
            with VEKDYN so the package can match the size and needs of the team. Contact
            VEKDYN to discuss program access and pilot availability.</p></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()

st.markdown(
    """<a class="program-pricing-button" href="?pricing=1" target="_self">Programs / Pricing</a>""",
    unsafe_allow_html=True,
)

st.markdown(
    '<div class="vekdyn-brand">VEK<span>DYN</span></div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="vekdyn-tagline">DATA DRIVES DEVELOPMENT</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="login-title">Welcome back</div>',
    unsafe_allow_html=True,
)

with st.form("vekdyn_unified_login"):
    username = st.text_input(
        "Username / Athlete ID",
        placeholder="Enter your username or Athlete ID",
    )

    password = st.text_input(
        "Password",
        type="password",
        placeholder="Enter your password",
    )

    submitted = st.form_submit_button(
        "Sign In",
        type="primary",
        use_container_width=True,
    )

if submitted:
    clean_username = username.strip()
    clean_password = password.strip()

    if not clean_username:
        st.error("Enter your username or Athlete ID.")

    elif not clean_password:
        st.error("Enter your password.")

    else:
        try:
            user = authenticate_user(
                clean_username,
                clean_password,
            )
        except psycopg2.Error as error:
            st.error(f"VEKDYN could not reach the account database: {error}")
            st.stop()

        if user:
            st.session_state["vekdyn_authenticated_user"] = user
            st.query_params["vekdyn_session"] = encode_unified_session(user)

            # Clear old routing state before entering the correct hub.
            if user["role"] == "coach":
                st.session_state["logged_in"] = True
                st.session_state["logged_in_user"] = user["username"]
                st.session_state["active_team"] = user["team_id"]
                st.session_state["pending_team"] = None
                st.session_state["page"] = "dashboard"

            else:
                st.session_state["logged_in"] = True
                st.session_state["athlete_id"] = user["athlete_id"]
                st.session_state["password_change_required"] = user[
                    "must_change_password"
                ]

            st.rerun()

        else:
            st.error("Incorrect username / Athlete ID or password.")



# =========================================================
# COACH HUB LOGOUT INTEGRATION NOTE
# =========================================================
# In coach_hub.py, the logout button should clear the unified login too.
# Replace its old logout action with:
#
#     for key in [
#         "vekdyn_authenticated_user", "vekdyn_role", "logged_in",
#         "logged_in_user", "active_team", "pending_team", "page"
#     ]:
#         st.session_state.pop(key, None)
#     st.query_params.clear()
#     st.rerun()
#
# This entry point also understands ?logout=1 if you prefer to route the
# Coach Hub logout back through the unified controller.
