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
        /* Full-bleed VEKDYN background: remove Streamlit's white top strip. */
        html, body, #root, [data-testid="stAppViewContainer"], .stApp {
            margin: 0 !important;
            padding: 0 !important;
            min-height: 100vh !important;
        }

        [data-testid="stHeader"],
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] {
            background: transparent !important;
            box-shadow: none !important;
            border: 0 !important;
        }

        [data-testid="stAppViewContainer"] {
            background: transparent !important;
        }

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
            min-height: 100vh !important;
            background-size: cover !important;
            background-position: center center !important;
            background-repeat: no-repeat !important;
            background-attachment: fixed !important;
        }

        .main,
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"] {
            background: transparent !important;
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
# Uses native Streamlit buttons so navigation is reliable in Community Cloud.
# =========================================================

pricing_mode = st.query_params.get("pricing") == "1"

st.markdown(
    """
    <style>
        /* Hide Streamlit's public toolbar so VEKDYN owns the top-right action area. */
        [data-testid="stToolbar"] {
            display: none !important;
        }

        .pricing-page {
            background: rgba(255,255,255,.97);
            border: 1px solid rgba(229,231,235,.95);
            border-radius: 20px;
            padding: 34px 30px 30px 30px;
            box-shadow: 0 12px 38px rgba(0,0,0,.22);
            color: #111827;
        }

        .pricing-brand {
            text-align: center;
            font-size: 38px;
            font-weight: 900;
            letter-spacing: 1px;
            margin-bottom: 2px;
        }

        .pricing-brand span {
            color: #2f9e44;
        }

        .pricing-kicker {
            text-align: center;
            color: #6b7280;
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 2px;
            margin-bottom: 24px;
        }

        .pricing-heading {
            text-align: center;
            font-size: 30px;
            font-weight: 850;
            line-height: 1.2;
            margin-bottom: 8px;
        }

        .pricing-subheading {
            text-align: center;
            color: #6b7280;
            font-size: 15px;
            line-height: 1.6;
            margin: 0 auto 26px auto;
            max-width: 680px;
        }

        .plan-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin: 14px 0 10px 0;
        }

        .plan-card {
            border: 1px solid #dfe3e8;
            border-radius: 16px;
            background: #ffffff;
            padding: 24px 22px;
            min-height: 210px;
        }

        .plan-card.featured {
            border: 2px solid #2f9e44;
            box-shadow: 0 8px 24px rgba(47,158,68,.10);
        }

        .plan-label {
            color: #6b7280;
            font-size: 12px;
            font-weight: 850;
            letter-spacing: 1.3px;
            text-transform: uppercase;
            margin-bottom: 8px;
        }

        .plan-price {
            font-size: 35px;
            font-weight: 900;
            color: #111827;
            line-height: 1;
            margin-bottom: 8px;
        }

        .plan-price small {
            font-size: 15px;
            font-weight: 700;
            color: #6b7280;
        }

        .plan-copy {
            color: #4b5563;
            font-size: 14px;
            line-height: 1.5;
            margin-top: 12px;
        }

        .save-pill {
            display: inline-block;
            background: #eaf7ed;
            color: #237a35;
            border-radius: 999px;
            padding: 5px 9px;
            font-size: 12px;
            font-weight: 800;
            margin-top: 8px;
        }

        .included-box {
            margin-top: 20px;
            border-top: 1px solid #e5e7eb;
            padding-top: 20px;
        }

        .included-title {
            font-size: 18px;
            font-weight: 850;
            margin-bottom: 10px;
        }

        .included-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px 24px;
            color: #374151;
            font-size: 14px;
            line-height: 1.55;
        }

        .purchase-box {
            margin-top: 22px;
            background: #f7f9f7;
            border: 1px solid #e5e7eb;
            border-radius: 14px;
            padding: 18px 20px;
        }

        .purchase-title {
            font-size: 17px;
            font-weight: 850;
            margin-bottom: 10px;
        }

        .purchase-row {
            display: flex;
            justify-content: space-between;
            gap: 18px;
            padding: 7px 0;
            border-bottom: 1px solid #e5e7eb;
            font-size: 14px;
        }

        .purchase-row:last-child {
            border-bottom: 0;
        }

        .pricing-cta {
            text-align: center;
            margin: 28px 0 8px 0;
        }

        .pricing-cta-title {
            font-size: 22px;
            font-weight: 900;
            margin-bottom: 6px;
        }

        .pricing-cta-copy {
            color: #6b7280;
            font-size: 14px;
            margin-bottom: 4px;
        }

        @media (max-width: 720px) {
            .plan-grid,
            .included-grid {
                grid-template-columns: 1fr;
            }

            .pricing-page {
                padding: 26px 20px;
            }

            .pricing-heading {
                font-size: 26px;
            }
        }
    </style>
    """,
    unsafe_allow_html=True,
)

if pricing_mode:
    back_left, back_right = st.columns([3.2, 1])
    with back_right:
        if st.button(
            "← Back to Sign In",
            key="back_to_login",
            use_container_width=True,
        ):
            st.query_params.clear()
            st.rerun()

    pricing_html = '<div class="pricing-page"> <div class="pricing-brand">VEK<span>DYN</span></div> <div class="pricing-kicker">DATA DRIVES DEVELOPMENT</div> <div class="pricing-heading">Simple team pricing.</div> <div class="pricing-subheading"> One VEKDYN team license gives a program access to the coach platform and athlete experience. Choose annual or monthly billing. </div> <div class="plan-grid"> <div class="plan-card featured"> <div class="plan-label">Annual Team License</div> <div class="plan-price">$500 <small>/ year</small></div> <div class="save-pill">Save $100 annually</div> <div class="plan-copy"> Best value for programs using VEKDYN throughout the full season and academic year. </div> </div> <div class="plan-card"> <div class="plan-label">Monthly Team License</div> <div class="plan-price">$50 <small>/ month</small></div> <div class="plan-copy"> Flexible month-to-month access for programs that want to start with a shorter commitment. </div> </div> </div> <div class="included-box"> <div class="included-title">Included with either plan</div> <div class="included-grid"> <div>✓ Team dashboard & analytics</div> <div>✓ Athlete performance profiles</div> <div>✓ Training calendar & workout planning</div> <div>✓ Threshold & training analytics</div> <div>✓ Race predictions</div> <div>✓ Recovery tracking</div> <div>✓ Athlete data integrations</div> <div>✓ Secure team workspace</div> </div> </div> <div class="purchase-box"> <div class="purchase-title">Purchasing Information</div> <div class="purchase-row"><span>Vendor / Company</span><strong>VEKDYN</strong></div> <div class="purchase-row"><span>Product</span><strong>VEKDYN Team Platform</strong></div> <div class="purchase-row"><span>License</span><strong>Annual or Monthly Team License</strong></div> <div class="purchase-row"><span>Published Pricing</span><strong>$500/year or $50/month</strong></div> <div class="purchase-row"><span>Billing</span><strong>Annual invoice or monthly billing</strong></div> <div class="purchase-row"><span>Payment</span><strong>Invoice / ACH / Check</strong></div> </div> <div class="pricing-cta"> <div class="pricing-cta-title">Ready to bring VEKDYN to your program?</div> <div class="pricing-cta-copy"> Request program access or prepare the information needed for an invoice. </div> </div> </div>'
    st.markdown(pricing_html, unsafe_allow_html=True)

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
            type="primary",
            use_container_width=True,
        )

    if invoice_submit:
        if (
            not invoice_school.strip()
            or not invoice_contact.strip()
            or not invoice_email.strip()
        ):
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

    st.stop()

# Native button on the login screen. This is intentionally not an HTML link:
# Streamlit Cloud will render it reliably after every deployment.
nav_spacer, nav_action = st.columns([3.1, 1.35])
with nav_action:
    if st.button(
        "Programs / Pricing",
        key="open_program_pricing",
        use_container_width=True,
    ):
        st.query_params["pricing"] = "1"
        st.rerun()


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