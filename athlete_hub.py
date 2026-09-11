import base64
import calendar
import hashlib
import json
import re
import hmac
import html
import secrets
import time
from datetime import date, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import bcrypt
import psycopg2
import requests
import streamlit as st
from pathlib import Path


# =========================================================
# VEKDYN ATHLETE
# =========================================================

st.set_page_config(
    page_title="VEKDYN Athlete",
    page_icon="🏃",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# =========================================================
# DATABASE
# =========================================================

def get_database_connection():
    """
    Connect to the same Neon database used by
    the VEKDYN Coach platform.
    """

    return psycopg2.connect(
        st.secrets["DATABASE_URL"]
    )


# =========================================================
# STRAVA — ATHLETE SELF-CONNECTION
# =========================================================

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_REDIRECT_URI = st.secrets.get("ATHLETE_STRAVA_REDIRECT_URI", "http://localhost:8501")

def strava_secret(name, default=None):
    try:
        return st.secrets[name]
    except (KeyError, FileNotFoundError):
        return default

def initialize_strava_database():
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strava_connections (
                    athlete_key TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at BIGINT NOT NULL,
                    scope TEXT,
                    strava_athlete_id BIGINT UNIQUE,
                    strava_name TEXT
                )
            """)

def load_saved_strava_connection(athlete_key):
    initialize_strava_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute("""
                SELECT athlete_key, access_token, refresh_token, expires_at,
                       scope, strava_athlete_id, strava_name
                FROM strava_connections
                WHERE athlete_key = %s
            """, (athlete_key,))
            row = cursor.fetchone()
    if not row:
        return {}
    return {
        "athlete_key": row[0], "access_token": row[1], "refresh_token": row[2],
        "expires_at": row[3], "scope": row[4], "strava_athlete_id": row[5],
        "strava_name": row[6],
    }

def saved_owner_of_strava_account(strava_athlete_id):
    if not strava_athlete_id:
        return None
    initialize_strava_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT athlete_key FROM strava_connections WHERE strava_athlete_id = %s",
                (strava_athlete_id,),
            )
            row = cursor.fetchone()
    return row[0] if row else None

def persist_strava_connection(athlete_key, connection):
    initialize_strava_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute("""
                INSERT INTO strava_connections (
                    athlete_key, access_token, refresh_token, expires_at,
                    scope, strava_athlete_id, strava_name
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (athlete_key) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_at = EXCLUDED.expires_at,
                    scope = EXCLUDED.scope,
                    strava_athlete_id = EXCLUDED.strava_athlete_id,
                    strava_name = EXCLUDED.strava_name
            """, (
                athlete_key, connection["access_token"], connection["refresh_token"],
                int(connection["expires_at"]), connection.get("scope", ""),
                connection.get("strava_athlete_id"), connection.get("strava_name", ""),
            ))

def create_strava_login_url(athlete_key):
    client_id = strava_secret("STRAVA_CLIENT_ID")
    client_secret = strava_secret("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    nonce = secrets.token_urlsafe(12)
    payload = f"{athlete_key}:{nonce}"
    signature = hmac.new(str(client_secret).encode(), payload.encode(), hashlib.sha256).hexdigest()
    state = f"{payload}:{signature}"
    params = {
        "client_id": client_id, "redirect_uri": STRAVA_REDIRECT_URI,
        "response_type": "code", "approval_prompt": "force",
        "scope": "read,activity:read_all", "state": state,
    }
    return f"{STRAVA_AUTHORIZE_URL}?{urlencode(params)}"

def athlete_key_from_oauth_state(oauth_state):
    client_secret = strava_secret("STRAVA_CLIENT_SECRET")
    if not oauth_state or not client_secret:
        return None
    try:
        athlete_key, nonce, returned_signature = oauth_state.split(":", 2)
    except ValueError:
        return None
    payload = f"{athlete_key}:{nonce}"
    expected = hmac.new(str(client_secret).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return athlete_key if hmac.compare_digest(returned_signature, expected) else None

def exchange_authorization_code(code, athlete_key):
    response = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": strava_secret("STRAVA_CLIENT_ID"),
        "client_secret": strava_secret("STRAVA_CLIENT_SECRET"),
        "code": code, "grant_type": "authorization_code",
    }, timeout=15)
    response.raise_for_status()
    token_data = response.json()
    person = token_data.get("athlete", {})
    strava_id = person.get("id")
    strava_name = " ".join(x for x in [person.get("firstname", ""), person.get("lastname", "")] if x).strip()
    owner = saved_owner_of_strava_account(strava_id)
    if owner and owner != athlete_key:
        raise RuntimeError("This Strava account is already connected to another VEKDYN athlete.")
    connection = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_at": token_data["expires_at"],
        "scope": token_data.get("scope", ""),
        "strava_athlete_id": strava_id,
        "strava_name": strava_name,
    }
    persist_strava_connection(athlete_key, connection)
    return connection

def refresh_strava_token(athlete_key):
    connection = load_saved_strava_connection(athlete_key)
    refresh_token = connection.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No Strava refresh token exists.")
    response = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": strava_secret("STRAVA_CLIENT_ID"),
        "client_secret": strava_secret("STRAVA_CLIENT_SECRET"),
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, timeout=15)
    response.raise_for_status()
    token_data = response.json()
    updated = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "expires_at": token_data["expires_at"],
        "scope": token_data.get("scope", connection.get("scope", "")),
        "strava_athlete_id": connection.get("strava_athlete_id"),
        "strava_name": connection.get("strava_name", ""),
    }
    persist_strava_connection(athlete_key, updated)
    return updated["access_token"]

def get_valid_strava_token(athlete_key):
    connection = load_saved_strava_connection(athlete_key)
    if not connection:
        return None
    if connection.get("access_token") and connection.get("expires_at", 0) > time.time() + 60:
        return connection["access_token"]
    return refresh_strava_token(athlete_key)

def handle_strava_callback():
    code = st.query_params.get("code")
    returned_state = st.query_params.get("state")
    oauth_error = st.query_params.get("error")
    if not code and not oauth_error:
        return
    if oauth_error:
        st.error("Strava authorization was cancelled.")
        clear_oauth_params_keep_session()
        return
    oauth_athlete_key = athlete_key_from_oauth_state(returned_state)
    if not oauth_athlete_key:
        st.error("The Strava connection could not be verified.")
        clear_oauth_params_keep_session()
        return
    logged_in_key = st.session_state.get("athlete_id")
    if not logged_in_key or oauth_athlete_key != logged_in_key:
        st.error("This Strava authorization does not belong to the logged-in VEKDYN athlete.")
        clear_oauth_params_keep_session()
        return
    try:
        connection = exchange_authorization_code(code, oauth_athlete_key)
        st.session_state["strava_success"] = f"Connected to {connection.get('strava_name') or 'Strava'}."
        st.query_params.clear()
        st.rerun()
    except (requests.RequestException, RuntimeError) as error:
        st.error(f"Strava connection failed: {error}")


# =========================================================
# COROS MCP — ATHLETE SELF-CONNECTION + SLEEP / HRV
# =========================================================
# COROS authorization happens on the athlete side.  The resulting connection
# and recovery rows are stored in the same Neon database the Coach Hub reads.
# This means the athlete owns the authorization, while the coach can see the
# connected status and recovery data without ever receiving COROS credentials.

# Universal COROS MCP endpoint; COROS routes the athlete to the correct region.
COROS_MCP_URL = "https://mcp.coros.com/mcp"
COROS_TIMEZONE = "America/Chicago"
COROS_PROTOCOL_VERSION = "2025-06-18"

# COROS must use its own deployed callback. Never fall back to Strava/localhost.
COROS_REDIRECT_URI = str(st.secrets.get("ATHLETE_COROS_REDIRECT_URI", "")).strip()


def initialize_coros_database():
    """Create/upgrade the shared COROS tables used by Athlete and Coach."""
    with get_database_connection() as database:
        with database.cursor() as cursor:
            # One dynamically registered COROS OAuth client per redirect URI.
            # Keeping the redirect URI in the primary key avoids colliding with
            # the coach app if both apps are deployed at different URLs.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS coros_athlete_oauth_clients (
                    redirect_uri TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_secret TEXT,
                    token_endpoint TEXT NOT NULL,
                    authorization_endpoint TEXT NOT NULL,
                    token_endpoint_auth_method TEXT,
                    registration_json JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS coros_athlete_oauth_pending (
                    state TEXT PRIMARY KEY,
                    athlete_key TEXT NOT NULL,
                    athlete_id TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    client_secret TEXT,
                    token_endpoint TEXT NOT NULL,
                    token_endpoint_auth_method TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS coros_connections (
                    athlete_key TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    token_type TEXT,
                    scope TEXT,
                    expires_at BIGINT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # Upgrade an older COROS table without destroying existing tokens.
            cursor.execute(
                "ALTER TABLE coros_connections ADD COLUMN IF NOT EXISTS client_id TEXT"
            )
            cursor.execute(
                "ALTER TABLE coros_connections ADD COLUMN IF NOT EXISTS client_secret TEXT"
            )
            cursor.execute(
                "ALTER TABLE coros_connections ADD COLUMN IF NOT EXISTS token_endpoint TEXT"
            )
            cursor.execute(
                "ALTER TABLE coros_connections ADD COLUMN IF NOT EXISTS token_endpoint_auth_method TEXT"
            )
            cursor.execute(
                "ALTER TABLE coros_connections ADD COLUMN IF NOT EXISTS redirect_uri TEXT"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS coros_recovery_daily (
                    athlete_key TEXT NOT NULL,
                    recovery_date DATE NOT NULL,
                    sleep_minutes INTEGER,
                    sleep_score INTEGER,
                    hrv_avg INTEGER,
                    hrv_baseline INTEGER,
                    hrv_normal_low INTEGER,
                    hrv_normal_high INTEGER,
                    hrv_status TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (athlete_key, recovery_date)
                )
                """
            )

            cursor.execute(
                "ALTER TABLE coros_recovery_daily ADD COLUMN IF NOT EXISTS resting_hr INTEGER"
            )
            cursor.execute(
                "ALTER TABLE coros_recovery_daily ADD COLUMN IF NOT EXISTS vekdyn_recovery_score INTEGER"
            )

        database.commit()


def _safe_json(response):
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError(
            f"COROS returned an unreadable response ({response.status_code})."
        ) from error


def _coros_auth_metadata():
    """Discover COROS OAuth endpoints from the protected MCP resource."""
    origin = COROS_MCP_URL.rsplit("/mcp", 1)[0]
    resource_metadata = None

    # RFC 9728 path-aware location first, root fallback second.
    for metadata_url in (
        f"{origin}/.well-known/oauth-protected-resource/mcp",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        try:
            response = requests.get(metadata_url, timeout=15, allow_redirects=True)
        except requests.RequestException:
            continue

        if response.ok:
            candidate = _safe_json(response)
            if candidate.get("authorization_servers"):
                resource_metadata = candidate
                break

    if not resource_metadata:
        # Some MCP servers advertise the metadata URL only on a 401 challenge.
        try:
            challenge = requests.get(
                COROS_MCP_URL,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=15,
                allow_redirects=True,
            )
            www_authenticate = challenge.headers.get("WWW-Authenticate", "")
            match = re.search(r'resource_metadata="([^"]+)"', www_authenticate)
            if match:
                response = requests.get(match.group(1), timeout=15, allow_redirects=True)
                if response.ok:
                    resource_metadata = _safe_json(response)
        except requests.RequestException:
            pass

    if not resource_metadata or not resource_metadata.get("authorization_servers"):
        raise RuntimeError(
            "COROS OAuth discovery failed. The COROS MCP authorization server "
            "could not be located."
        )

    issuer = str(resource_metadata["authorization_servers"][0]).rstrip("/")

    for metadata_url in (
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ):
        try:
            response = requests.get(metadata_url, timeout=15, allow_redirects=True)
        except requests.RequestException:
            continue

        if response.ok:
            metadata = _safe_json(response)
            if metadata.get("authorization_endpoint") and metadata.get("token_endpoint"):
                return metadata

    raise RuntimeError("COROS authorization metadata could not be loaded.")


def _load_coros_oauth_client():
    """Return or dynamically register the OAuth client for this Athlete app."""
    initialize_coros_database()
    redirect_uri = str(COROS_REDIRECT_URI).strip()

    if not redirect_uri:
        raise RuntimeError(
            "ATHLETE_COROS_REDIRECT_URI is missing from Streamlit Secrets. "
            "Set it to the public VEKDYN Athlete app URL."
        )

    if not redirect_uri.startswith("https://"):
        raise RuntimeError(
            "ATHLETE_COROS_REDIRECT_URI must be the public HTTPS URL of the "
            "VEKDYN Athlete app; localhost is not valid for production."
        )

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT client_id, client_secret, token_endpoint,
                       authorization_endpoint, token_endpoint_auth_method
                FROM coros_athlete_oauth_clients
                WHERE redirect_uri = %s
                """,
                (redirect_uri,),
            )
            row = cursor.fetchone()

    if row:
        return {
            "client_id": row[0],
            "client_secret": row[1],
            "token_endpoint": row[2],
            "authorization_endpoint": row[3],
            "token_endpoint_auth_method": row[4] or "none",
        }

    metadata = _coros_auth_metadata()
    registration_endpoint = metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise RuntimeError(
            "COROS did not advertise dynamic OAuth client registration."
        )

    registration_payload = {
        "client_name": "VEKDYN Athlete",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    response = requests.post(
        registration_endpoint,
        json=registration_payload,
        timeout=20,
        allow_redirects=True,
    )
    response.raise_for_status()
    registration = _safe_json(response)

    client_id = registration.get("client_id")
    if not client_id:
        raise RuntimeError("COROS registration did not return a client ID.")

    client = {
        "client_id": str(client_id),
        "client_secret": registration.get("client_secret"),
        "token_endpoint": str(metadata["token_endpoint"]),
        "authorization_endpoint": str(metadata["authorization_endpoint"]),
        "token_endpoint_auth_method": registration.get(
            "token_endpoint_auth_method", "none"
        ),
    }

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO coros_athlete_oauth_clients (
                    redirect_uri, client_id, client_secret, token_endpoint,
                    authorization_endpoint, token_endpoint_auth_method,
                    registration_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (redirect_uri)
                DO UPDATE SET
                    client_id = EXCLUDED.client_id,
                    client_secret = EXCLUDED.client_secret,
                    token_endpoint = EXCLUDED.token_endpoint,
                    authorization_endpoint = EXCLUDED.authorization_endpoint,
                    token_endpoint_auth_method = EXCLUDED.token_endpoint_auth_method,
                    registration_json = EXCLUDED.registration_json,
                    updated_at = NOW()
                """,
                (
                    redirect_uri,
                    client["client_id"],
                    client.get("client_secret"),
                    client["token_endpoint"],
                    client["authorization_endpoint"],
                    client["token_endpoint_auth_method"],
                    json.dumps(registration),
                ),
            )
        database.commit()

    return client


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def create_coros_login_url(athlete_key, athlete_id):
    """Create a COROS OAuth URL tied to the logged-in VEKDYN athlete."""
    client = _load_coros_oauth_client()
    metadata = _coros_auth_metadata()
    verifier, challenge = _pkce_pair()
    state = "coros_" + secrets.token_urlsafe(32)
    redirect_uri = str(COROS_REDIRECT_URI).strip()

    initialize_coros_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM coros_athlete_oauth_pending
                WHERE created_at < NOW() - INTERVAL '30 minutes'
                """
            )
            cursor.execute(
                """
                INSERT INTO coros_athlete_oauth_pending (
                    state, athlete_key, athlete_id, code_verifier, redirect_uri,
                    client_id, client_secret, token_endpoint,
                    token_endpoint_auth_method
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    state,
                    str(athlete_key).strip(),
                    str(athlete_id).strip(),
                    verifier,
                    redirect_uri,
                    client["client_id"],
                    client.get("client_secret"),
                    client["token_endpoint"],
                    client.get("token_endpoint_auth_method", "none"),
                ),
            )
        database.commit()

    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": COROS_MCP_URL,
    }

    supported_scopes = metadata.get("scopes_supported") or []
    if supported_scopes:
        params["scope"] = " ".join(str(scope) for scope in supported_scopes)

    return str(client["authorization_endpoint"]) + "?" + urlencode(params)


def _coros_pending_oauth(state):
    initialize_coros_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT athlete_key, athlete_id, code_verifier, redirect_uri,
                       client_id, client_secret, token_endpoint,
                       token_endpoint_auth_method
                FROM coros_athlete_oauth_pending
                WHERE state = %s
                  AND created_at > NOW() - INTERVAL '30 minutes'
                LIMIT 1
                """,
                (state,),
            )
            row = cursor.fetchone()

    if not row:
        return None

    return {
        "athlete_key": row[0],
        "athlete_id": row[1],
        "code_verifier": row[2],
        "redirect_uri": row[3],
        "client_id": row[4],
        "client_secret": row[5],
        "token_endpoint": row[6],
        "token_endpoint_auth_method": row[7] or "none",
    }


def _oauth_token_request(endpoint, form, client_id, client_secret, auth_method):
    """POST to an OAuth token endpoint using the registered client auth method."""
    clean_form = dict(form)
    auth = None

    if auth_method == "client_secret_basic" and client_secret:
        auth = (client_id, client_secret)
    else:
        clean_form["client_id"] = client_id
        if auth_method == "client_secret_post" and client_secret:
            clean_form["client_secret"] = client_secret

    response = requests.post(
        endpoint,
        data=clean_form,
        auth=auth,
        timeout=20,
        allow_redirects=True,
    )
    response.raise_for_status()
    return _safe_json(response)


def exchange_coros_authorization_code(code, state):
    pending = _coros_pending_oauth(state)
    if not pending:
        raise RuntimeError(
            "This COROS authorization expired or could not be verified. "
            "Sign in to VEKDYN Athlete and select Connect COROS again."
        )

    token = _oauth_token_request(
        endpoint=pending["token_endpoint"],
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending["redirect_uri"],
            "code_verifier": pending["code_verifier"],
            "resource": COROS_MCP_URL,
        },
        client_id=pending["client_id"],
        client_secret=pending.get("client_secret"),
        auth_method=pending.get("token_endpoint_auth_method", "none"),
    )

    access_token = token.get("access_token")
    if not access_token:
        raise RuntimeError("COROS returned no access token.")

    expires_at = None
    if token.get("expires_in") is not None:
        expires_at = int(time.time()) + int(token["expires_in"])

    initialize_coros_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO coros_connections (
                    athlete_key, access_token, refresh_token, token_type,
                    scope, expires_at, client_id, client_secret,
                    token_endpoint, token_endpoint_auth_method, redirect_uri
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (athlete_key)
                DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    token_type = EXCLUDED.token_type,
                    scope = EXCLUDED.scope,
                    expires_at = EXCLUDED.expires_at,
                    client_id = EXCLUDED.client_id,
                    client_secret = EXCLUDED.client_secret,
                    token_endpoint = EXCLUDED.token_endpoint,
                    token_endpoint_auth_method = EXCLUDED.token_endpoint_auth_method,
                    redirect_uri = EXCLUDED.redirect_uri,
                    updated_at = NOW()
                """,
                (
                    pending["athlete_key"],
                    access_token,
                    token.get("refresh_token"),
                    token.get("token_type", "Bearer"),
                    token.get("scope", ""),
                    expires_at,
                    pending["client_id"],
                    pending.get("client_secret"),
                    pending["token_endpoint"],
                    pending.get("token_endpoint_auth_method", "none"),
                    pending["redirect_uri"],
                ),
            )
            cursor.execute(
                "DELETE FROM coros_athlete_oauth_pending WHERE state = %s",
                (state,),
            )
        database.commit()

    return pending["athlete_key"], pending["athlete_id"]


def load_saved_coros_connection(athlete_key):
    initialize_coros_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT access_token, refresh_token, expires_at, client_id,
                       client_secret, token_endpoint,
                       token_endpoint_auth_method, redirect_uri
                FROM coros_connections
                WHERE athlete_key = %s
                """,
                (athlete_key,),
            )
            row = cursor.fetchone()

    if not row:
        return {}

    return {
        "access_token": row[0],
        "refresh_token": row[1],
        "expires_at": row[2],
        "client_id": row[3],
        "client_secret": row[4],
        "token_endpoint": row[5],
        "token_endpoint_auth_method": row[6] or "none",
        "redirect_uri": row[7],
    }


def coros_is_connected(athlete_key):
    return bool(load_saved_coros_connection(athlete_key).get("access_token"))


def get_valid_coros_token(athlete_key):
    connection = load_saved_coros_connection(athlete_key)
    if not connection:
        raise RuntimeError("COROS has not been connected yet.")

    expires_at = connection.get("expires_at")
    if not expires_at or int(expires_at) > int(time.time()) + 120:
        return connection["access_token"]

    refresh_token = connection.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("The COROS session expired. Reconnect COROS.")

    if not connection.get("client_id") or not connection.get("token_endpoint"):
        raise RuntimeError(
            "This older COROS connection is missing refresh information. Reconnect COROS once."
        )

    token = _oauth_token_request(
        endpoint=connection["token_endpoint"],
        form={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "resource": COROS_MCP_URL,
        },
        client_id=connection["client_id"],
        client_secret=connection.get("client_secret"),
        auth_method=connection.get("token_endpoint_auth_method", "none"),
    )

    if not token.get("access_token"):
        raise RuntimeError("COROS did not return a refreshed access token.")

    new_expires_at = None
    if token.get("expires_in") is not None:
        new_expires_at = int(time.time()) + int(token["expires_in"])

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                UPDATE coros_connections
                SET access_token = %s,
                    refresh_token = COALESCE(%s, refresh_token),
                    expires_at = %s,
                    updated_at = NOW()
                WHERE athlete_key = %s
                """,
                (
                    token["access_token"],
                    token.get("refresh_token"),
                    new_expires_at,
                    athlete_key,
                ),
            )
        database.commit()

    return token["access_token"]


def _mcp_response_json(response):
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")

    if "application/json" in content_type:
        return response.json()

    # Streamable HTTP may reply as SSE. Read the first JSON data event.
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue

    raise RuntimeError("COROS MCP returned an unreadable response.")


def coros_mcp_tool_call(access_token, tool_name, arguments):
    """Initialize a short MCP session and call one COROS tool."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": COROS_PROTOCOL_VERSION,
    }

    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": COROS_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "VEKDYN Athlete", "version": "1.0"},
        },
    }

    initialize_response = requests.post(
        COROS_MCP_URL,
        headers=headers,
        json=initialize_payload,
        timeout=25,
        allow_redirects=True,
    )
    initialize_data = _mcp_response_json(initialize_response)
    if initialize_data.get("error"):
        raise RuntimeError(str(initialize_data["error"]))

    session_id = initialize_response.headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    # Complete the MCP initialization handshake before calling a tool.
    initialized_payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    initialized_response = requests.post(
        COROS_MCP_URL,
        headers=headers,
        json=initialized_payload,
        timeout=15,
        allow_redirects=True,
    )
    if initialized_response.status_code >= 400:
        initialized_response.raise_for_status()

    call_payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    call_response = requests.post(
        COROS_MCP_URL,
        headers=headers,
        json=call_payload,
        timeout=30,
        allow_redirects=True,
    )
    call_data = _mcp_response_json(call_response)
    if call_data.get("error"):
        raise RuntimeError(str(call_data["error"]))

    text_parts = []
    for item in call_data.get("result", {}).get("content", []):
        if isinstance(item, dict) and item.get("text"):
            text_parts.append(str(item["text"]))

    return "\n".join(text_parts)


def _parse_coros_sleep(text):
    records = {}
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2})[\s\S]*?"
        r"Sleep Score:\s*(\d+)[\s\S]*?"
        r"Main Sleep:\s*(?:(\d+)h\s*)?(\d+)min"
    )
    for match in pattern.finditer(text or ""):
        records[match.group(1)] = {
            "sleep_score": int(match.group(2)),
            "sleep_minutes": int(match.group(3) or 0) * 60 + int(match.group(4)),
        }
    return records


def _parse_coros_hrv(text):
    records = {}
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}):\s*\n\s*"
        r"HRV Avg:\s*(\d+)\s*ms\s*(?:—|-)\s*([^\n]+)\s*\n\s*"
        r"Normal Range:\s*(\d+)\s*-\s*(\d+)\s*ms\s*\n\s*"
        r"Baseline:\s*(\d+)\s*ms"
    )
    for match in pattern.finditer(text or ""):
        records[match.group(1)] = {
            "hrv_avg": int(match.group(2)),
            "hrv_status": match.group(3).strip(),
            "hrv_normal_low": int(match.group(4)),
            "hrv_normal_high": int(match.group(5)),
            "hrv_baseline": int(match.group(6)),
        }
    return records


def _parse_coros_resting_hr(text):
    records = {}
    pattern = re.compile(r"(\d{4}-\d{2}-\d{2}):\s*(\d+)\s*bpm", re.IGNORECASE)
    for match in pattern.finditer(text or ""):
        records[match.group(1)] = int(match.group(2))
    return records


def _recovery_score(sleep_score, hrv_avg, hrv_baseline, resting_hr, resting_baseline):
    """VEKDYN training-readiness score; missing inputs are reweighted, never fabricated."""
    components = []
    if sleep_score is not None:
        components.append((max(0.0, min(100.0, float(sleep_score))), 0.45))
    if hrv_avg is not None and hrv_baseline not in (None, 0):
        value = 100.0 * float(hrv_avg) / float(hrv_baseline)
        components.append((max(0.0, min(100.0, value)), 0.40))
    if resting_hr is not None and resting_baseline not in (None, 0):
        value = 100.0 * float(resting_baseline) / float(resting_hr)
        components.append((max(0.0, min(100.0, value)), 0.15))
    if not components:
        return None
    total_weight = sum(weight for _, weight in components)
    return int(round(sum(value * weight for value, weight in components) / total_weight))


def _resting_hr_baseline(athlete_key, recovery_date):
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT AVG(resting_hr)::float
                FROM (
                    SELECT resting_hr
                    FROM coros_recovery_daily
                    WHERE athlete_key = %s
                      AND recovery_date < %s
                      AND resting_hr IS NOT NULL
                    ORDER BY recovery_date DESC
                    LIMIT 7
                ) recent
                """,
                (athlete_key, recovery_date),
            )
            row = cursor.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def sync_coros_recovery(athlete_key, days=7):
    """Pull recent COROS sleep, sleep HRV and resting HR into the shared Neon table."""
    access_token = get_valid_coros_token(athlete_key)
    days = max(1, min(int(days), 7))
    arguments = {"startDate": "", "endDate": "", "days": days, "timezone": COROS_TIMEZONE}

    sleep_text = coros_mcp_tool_call(access_token, "querySleepData", arguments)
    hrv_text = coros_mcp_tool_call(access_token, "querySleepHrv", arguments)
    resting_text = coros_mcp_tool_call(
        access_token,
        "queryRestingHeartRate",
        {"days": days, "timezone": COROS_TIMEZONE},
    )

    sleep_records = _parse_coros_sleep(sleep_text)
    hrv_records = _parse_coros_hrv(hrv_text)
    resting_records = _parse_coros_resting_hr(resting_text)
    all_dates = sorted(set(sleep_records) | set(hrv_records) | set(resting_records))

    if not all_dates:
        raise RuntimeError("COROS connected, but no recent recovery record was returned.")

    initialize_coros_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            for recovery_date in all_dates:
                sleep = sleep_records.get(recovery_date, {})
                hrv = hrv_records.get(recovery_date, {})
                resting_hr = resting_records.get(recovery_date)
                baseline = _resting_hr_baseline(athlete_key, recovery_date)
                score = _recovery_score(
                    sleep.get("sleep_score"),
                    hrv.get("hrv_avg"),
                    hrv.get("hrv_baseline"),
                    resting_hr,
                    baseline,
                )
                cursor.execute(
                    """
                    INSERT INTO coros_recovery_daily (
                        athlete_key, recovery_date, sleep_minutes, sleep_score,
                        hrv_avg, hrv_baseline, hrv_normal_low, hrv_normal_high,
                        hrv_status, resting_hr, vekdyn_recovery_score
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (athlete_key, recovery_date)
                    DO UPDATE SET
                        sleep_minutes = COALESCE(EXCLUDED.sleep_minutes, coros_recovery_daily.sleep_minutes),
                        sleep_score = COALESCE(EXCLUDED.sleep_score, coros_recovery_daily.sleep_score),
                        hrv_avg = COALESCE(EXCLUDED.hrv_avg, coros_recovery_daily.hrv_avg),
                        hrv_baseline = COALESCE(EXCLUDED.hrv_baseline, coros_recovery_daily.hrv_baseline),
                        hrv_normal_low = COALESCE(EXCLUDED.hrv_normal_low, coros_recovery_daily.hrv_normal_low),
                        hrv_normal_high = COALESCE(EXCLUDED.hrv_normal_high, coros_recovery_daily.hrv_normal_high),
                        hrv_status = COALESCE(EXCLUDED.hrv_status, coros_recovery_daily.hrv_status),
                        resting_hr = COALESCE(EXCLUDED.resting_hr, coros_recovery_daily.resting_hr),
                        vekdyn_recovery_score = COALESCE(EXCLUDED.vekdyn_recovery_score, coros_recovery_daily.vekdyn_recovery_score),
                        updated_at = NOW()
                    """,
                    (
                        athlete_key, recovery_date, sleep.get("sleep_minutes"), sleep.get("sleep_score"),
                        hrv.get("hrv_avg"), hrv.get("hrv_baseline"), hrv.get("hrv_normal_low"),
                        hrv.get("hrv_normal_high"), hrv.get("hrv_status"), resting_hr, score,
                    ),
                )
        database.commit()

    return load_latest_coros_recovery(athlete_key)


def load_latest_coros_recovery(athlete_key):
    initialize_coros_database()
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT recovery_date, sleep_minutes, sleep_score, hrv_avg,
                       hrv_baseline, hrv_normal_low, hrv_normal_high, hrv_status,
                       resting_hr, vekdyn_recovery_score
                FROM coros_recovery_daily
                WHERE athlete_key = %s
                ORDER BY recovery_date DESC
                LIMIT 1
                """,
                (athlete_key,),
            )
            row = cursor.fetchone()

    if not row:
        return {}

    return {
        "date": row[0],
        "sleep_minutes": row[1],
        "sleep_score": row[2],
        "hrv_avg": row[3],
        "hrv_baseline": row[4],
        "hrv_normal_low": row[5],
        "hrv_normal_high": row[6],
        "hrv_status": row[7],
        "resting_hr": row[8],
        "vekdyn_recovery_score": row[9],
    }


# =========================================================
# ATHLETE LOGIN TABLE
# =========================================================

def create_login_table():
    """
    Create/upgrade the ONE shared athlete-login table used by
    VEKDYN Coach and VEKDYN Athlete.
    """

    conn = get_database_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_logins (
                    athlete_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                );
                """
            )

            cursor.execute(
                "ALTER TABLE athlete_logins ADD COLUMN IF NOT EXISTS athlete_key TEXT;"
            )
            cursor.execute(
                "ALTER TABLE athlete_logins ADD COLUMN IF NOT EXISTS team_id TEXT;"
            )
            cursor.execute(
                "ALTER TABLE athlete_logins ADD COLUMN IF NOT EXISTS display_name TEXT;"
            )
            cursor.execute(
                "ALTER TABLE athlete_logins ADD COLUMN IF NOT EXISTS event_group TEXT DEFAULT 'Distance';"
            )
            cursor.execute(
                "ALTER TABLE athlete_logins ADD COLUMN IF NOT EXISTS password_updated_at TIMESTAMPTZ;"
            )
            # Existing accounts remain normal accounts. The Coach Hub should set this
            # to TRUE whenever it generates/resets a temporary athlete password.
            cursor.execute(
                "ALTER TABLE athlete_logins ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;"
            )


            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'athlete_logins';
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
                      AND BTRIM(athlete_name) <> '';
                    """
                )

            if "updated_at" in existing_columns:
                cursor.execute(
                    """
                    UPDATE athlete_logins
                    SET password_updated_at = updated_at
                    WHERE password_updated_at IS NULL
                      AND updated_at IS NOT NULL;
                    """
                )

            cursor.execute(
                """
                UPDATE athlete_logins
                SET athlete_key = athlete_id
                WHERE athlete_key IS NULL
                   OR BTRIM(athlete_key) = '';
                """
            )

            cursor.execute(
                """
                UPDATE athlete_logins
                SET event_group = 'Distance'
                WHERE event_group IS NULL
                   OR BTRIM(event_group) = '';
                """
            )

        conn.commit()

    finally:
        conn.close()

try:

    create_login_table()

except Exception as error:

    st.error(
        f"Could not initialize athlete login system: {error}"
    )


# =========================================================
# PERSISTENT ATHLETE SESSIONS
# =========================================================

ATHLETE_SESSION_DAYS = 30


def create_athlete_session_table():
    """
    Persistent login tokens live in Neon so a browser refresh does not
    force the athlete to sign in again.

    Only a SHA-256 hash of the random token is stored in Neon.
    """
    conn = get_database_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_sessions (
                    token_hash TEXT PRIMARY KEY,
                    athlete_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    revoked BOOLEAN NOT NULL DEFAULT FALSE
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_athlete_sessions_athlete_id
                ON athlete_sessions (athlete_id);
                """
            )
            cursor.execute(
                """
                DELETE FROM athlete_sessions
                WHERE expires_at <= NOW()
                   OR revoked = TRUE;
                """
            )
        conn.commit()
    finally:
        conn.close()


def _session_token_hash(raw_token):
    return hashlib.sha256(
        str(raw_token).encode("utf-8")
    ).hexdigest()


def issue_persistent_athlete_session(athlete_id):
    """
    Create a high-entropy bearer token and store only its hash in Neon.
    The raw token is returned to the browser through the app URL.
    """
    raw_token = secrets.token_urlsafe(48)
    token_hash = _session_token_hash(raw_token)

    conn = get_database_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO athlete_sessions (
                    token_hash,
                    athlete_id,
                    expires_at,
                    revoked
                )
                VALUES (
                    %s,
                    %s,
                    NOW() + (%s * INTERVAL '1 day'),
                    FALSE
                );
                """,
                (
                    token_hash,
                    str(athlete_id).strip(),
                    int(ATHLETE_SESSION_DAYS),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    return raw_token


def athlete_id_from_persistent_session(raw_token):
    """
    Validate the browser token against Neon and return the athlete ID.
    """
    if not raw_token:
        return None

    token_hash = _session_token_hash(raw_token)

    conn = get_database_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.athlete_id
                FROM athlete_sessions s
                JOIN athlete_logins a
                  ON LOWER(TRIM(a.athlete_id)) = LOWER(TRIM(s.athlete_id))
                WHERE s.token_hash = %s
                  AND s.revoked = FALSE
                  AND s.expires_at > NOW()
                  AND a.active = TRUE
                  AND COALESCE(a.must_change_password, FALSE) = FALSE
                LIMIT 1;
                """,
                (token_hash,),
            )
            row = cursor.fetchone()

        return row[0] if row else None

    finally:
        conn.close()


def revoke_persistent_athlete_session(raw_token):
    if not raw_token:
        return

    token_hash = _session_token_hash(raw_token)

    conn = get_database_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE athlete_sessions
                SET revoked = TRUE
                WHERE token_hash = %s;
                """,
                (token_hash,),
            )
        conn.commit()
    finally:
        conn.close()


def browser_session_token():
    """
    Read the persistent token from the URL.

    Streamlit session_state disappears on a hard browser refresh, while
    query parameters remain. The token itself is random and its hash is
    the only value stored in Neon.
    """
    token = st.query_params.get("session")

    if isinstance(token, list):
        token = token[0] if token else None

    return str(token).strip() if token else None


def set_browser_session_token(raw_token):
    if raw_token:
        st.query_params["session"] = raw_token


def clear_oauth_params_keep_session():
    """
    Remove Strava callback parameters without deleting the athlete's
    persistent VEKDYN login token.
    """
    current_session = browser_session_token()
    st.query_params.clear()

    if current_session:
        st.query_params["session"] = current_session


def handle_coros_callback_before_login():
    """Finish COROS OAuth even when the redirect creates a fresh Streamlit session."""
    code = st.query_params.get("code")
    state = st.query_params.get("state")
    oauth_error = st.query_params.get("error")

    if isinstance(state, list):
        state = state[0] if state else None
    if isinstance(code, list):
        code = code[0] if code else None
    if isinstance(oauth_error, list):
        oauth_error = oauth_error[0] if oauth_error else None

    # Strava uses a different state format. Leave its callback alone.
    if not state or not str(state).startswith("coros_"):
        return

    if oauth_error:
        st.query_params.clear()
        st.session_state["coros_callback_error"] = (
            "COROS authorization was cancelled or denied."
        )
        return

    if not code:
        return

    try:
        athlete_key, athlete_id = exchange_coros_authorization_code(
            str(code),
            str(state),
        )

        # Re-establish the VEKDYN athlete session after COROS redirects back.
        persistent_token = issue_persistent_athlete_session(athlete_id)
        st.session_state.logged_in = True
        st.session_state.athlete_id = athlete_id
        st.session_state.password_change_required = False
        st.session_state["coros_success"] = (
            "COROS connected. Recovery data is ready to sync."
        )

        st.query_params.clear()
        set_browser_session_token(persistent_token)
        st.rerun()

    except (requests.RequestException, RuntimeError, psycopg2.Error) as error:
        st.query_params.clear()
        st.session_state["coros_callback_error"] = f"COROS connection failed: {error}"


try:
    create_athlete_session_table()
except Exception as error:
    st.error(
        f"Could not initialize persistent athlete sessions: {error}"
    )


# COROS can return without the VEKDYN session query parameter, so process it
# before the login page decides whether the athlete is authenticated.
handle_coros_callback_before_login()


# =========================================================
# ATHLETE ACCOUNT PROFILE — SHARED WITH COACH HUB
# =========================================================

def load_logged_in_athlete_profile(athlete_id):
    """
    Resolve the authenticated login to the exact athlete/team record created
    in VEKDYN Coach.
    """

    clean_id = str(athlete_id).strip().lower()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    athlete_id,
                    athlete_key,
                    team_id,
                    display_name,
                    COALESCE(event_group, 'Distance')
                FROM athlete_logins
                WHERE LOWER(TRIM(athlete_id)) = %s
                  AND active = TRUE
                LIMIT 1;
                """,
                (clean_id,),
            )
            row = cursor.fetchone()

    if not row:
        return None

    stored_athlete_id = str(row[0]).strip()
    athlete_key = str(row[1] or row[0]).strip()
    team_id = str(row[2] or "").strip()
    display_name = str(row[3] or "").strip()
    event_group = str(row[4] or "Distance").strip() or "Distance"

    if not athlete_key or not team_id or not display_name:
        raise RuntimeError(
            "This athlete login exists, but its athlete/team profile has not "
            "been linked by VEKDYN Coach yet. Reset/create the athlete login "
            "once from the Coach Hub."
        )

    team_labels = {
        "ollu_distance": "OLLU",
        "sam_houston": "Sam Houston",
        "dark_horse_endurance": "Dark Horse Endurance",
    }

    return {
        "athlete_id": stored_athlete_id,
        "athlete_key": athlete_key,
        "team_id": team_id,
        "name": display_name,
        "team": team_labels.get(team_id, team_id.replace("_", " ").title()),
        "event_group": event_group,
    }


# =========================================================
# AUTHENTICATION
# =========================================================

def authenticate_athlete(athlete_id, password):
    """
    Verify the athlete's current password.

    Returns:
        (stored_athlete_id, must_change_password) when valid
        None when invalid
    """
    athlete_id = athlete_id.strip().lower()

    # Password remains case-sensitive.
    password = password.strip()

    conn = get_database_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    athlete_id,
                    password_hash,
                    active,
                    COALESCE(must_change_password, FALSE)
                FROM athlete_logins
                WHERE LOWER(TRIM(athlete_id)) = %s
                LIMIT 1;
                """,
                (athlete_id,),
            )

            account = cursor.fetchone()

        if account is None:
            return None

        stored_athlete_id = account[0]
        stored_password_hash = account[1]
        active = account[2]
        must_change_password = bool(account[3])

        if not active:
            return None

        if isinstance(stored_password_hash, str):
            stored_password_hash = stored_password_hash.encode("utf-8")

        entered_password = password.encode("utf-8")

        try:
            password_matches = bcrypt.checkpw(
                entered_password,
                stored_password_hash,
            )
        except ValueError:
            return None

        if password_matches:
            return stored_athlete_id, must_change_password

        return None

    except Exception as error:
        st.error(f"Authentication error: {error}")
        return None

    finally:
        conn.close()


def update_athlete_password(athlete_id, new_password):
    """
    Replace the temporary/current password with a bcrypt hash of the athlete's
    new password and clear the first-login requirement.
    """
    clean_athlete_id = str(athlete_id).strip().lower()
    clean_password = str(new_password)

    if len(clean_password) < 8:
        raise ValueError("Your password must be at least 8 characters long.")

    new_password_hash = bcrypt.hashpw(
        clean_password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")

    conn = get_database_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE athlete_logins
                SET password_hash = %s,
                    must_change_password = FALSE,
                    password_updated_at = NOW()
                WHERE LOWER(TRIM(athlete_id)) = %s
                  AND active = TRUE;
                """,
                (new_password_hash, clean_athlete_id),
            )

            if cursor.rowcount != 1:
                raise RuntimeError("The athlete account could not be updated.")

            # A password change invalidates every older persistent login for
            # this athlete. A fresh session is issued after the change.
            cursor.execute(
                """
                UPDATE athlete_sessions
                SET revoked = TRUE
                WHERE LOWER(TRIM(athlete_id)) = %s
                  AND revoked = FALSE;
                """,
                (clean_athlete_id,),
            )

        conn.commit()

    finally:
        conn.close()


# =========================================================
# SESSION STATE
# =========================================================

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if "athlete_id" not in st.session_state:
    st.session_state.athlete_id = None

if "training_week_offset" not in st.session_state:
    st.session_state.training_week_offset = 0

if "training_month_offset" not in st.session_state:
    st.session_state.training_month_offset = 0

if "home_week_offset" not in st.session_state:
    st.session_state.home_week_offset = 0

if "home_selected_date" not in st.session_state:
    st.session_state.home_selected_date = date.today()

if "password_change_required" not in st.session_state:
    st.session_state.password_change_required = False


# A hard browser refresh creates a new Streamlit session. Restore the
# authenticated athlete from the persistent Neon-backed token.
if not st.session_state.logged_in:
    saved_session_token = browser_session_token()

    if saved_session_token:
        try:
            restored_athlete_id = athlete_id_from_persistent_session(
                saved_session_token
            )
        except Exception:
            restored_athlete_id = None

        if restored_athlete_id:
            st.session_state.logged_in = True
            st.session_state.athlete_id = restored_athlete_id
        else:
            # Invalid/expired tokens should not remain in the address bar.
            st.query_params.clear()


# =========================================================
# STYLE
# =========================================================

st.markdown(
    """
    <style>

    /* Athlete daily feedback: entered note text should be clearly readable. */
    div[data-testid="stTextArea"] textarea {
        color: #111111 !important;
        -webkit-text-fill-color: #111111 !important;
        caret-color: #111111 !important;
    }

    div[data-testid="stTextArea"] textarea::placeholder {
        color: #9ca3af !important;
        -webkit-text-fill-color: #9ca3af !important;
        opacity: 1 !important;
    }


    /* -----------------------------------------------------
       APP SHELL
    ----------------------------------------------------- */

    .stApp {
        background: #f6f8f6;
        color: #111827;
    }

    .block-container {
        max-width: 980px;
        padding-top: 2.2rem;
        padding-bottom: 4rem;
    }

    #MainMenu {
        visibility: hidden;
    }

    footer {
        visibility: hidden;
    }

    header {
        visibility: hidden;
    }

    /* Keep the Streamlit sidebar completely out of the athlete app. */
    [data-testid="stSidebar"],
    [data-testid="collapsedControl"],
    [data-testid="stSidebarCollapsedControl"] {
        display: none !important;
    }

    /* -----------------------------------------------------
       TYPOGRAPHY / BRAND
    ----------------------------------------------------- */

    .vekdyn {
        font-size: 16px;
        font-weight: 800;
        letter-spacing: 1.8px;
        margin-bottom: 36px;
        color: #111827;
    }

    .vekdyn span {
        color: #2f9e44;
    }

    .welcome {
        font-size: 34px;
        font-weight: 800;
        line-height: 1.15;
        margin-bottom: 6px;
        color: #111827;
    }

    .subtext {
        color: #6b7280;
        font-size: 15px;
        margin-bottom: 0;
    }

    .pace-label {
        color: #6b7280;
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 1px;
    }

    .pace-value {
        font-size: 20px;
        font-weight: 700;
        margin-top: 5px;
        color: #111827;
    }

    /* -----------------------------------------------------
       TABS
    ----------------------------------------------------- */

    button[data-baseweb="tab"] {
        color: #374151 !important;
        font-weight: 600 !important;
    }

    button[data-baseweb="tab"][aria-selected="true"] {
        color: #2f9e44 !important;
    }

    div[data-baseweb="tab-highlight"] {
        background-color: #2f9e44 !important;
    }

    /* -----------------------------------------------------
       INPUTS / CARDS / DIVIDERS
    ----------------------------------------------------- */

    [data-testid="stVerticalBlockBorderWrapper"] {
        background: #ffffff;
        border-color: #dfe5df !important;
        border-radius: 14px !important;
    }

    div[data-testid="stMarkdownContainer"] p,
    div[data-testid="stCaptionContainer"] {
        color: #4b5563;
    }

    hr {
        border-color: #dfe5df !important;
    }

    /* Buttons should visually match the Coach dashboard. */
    div.stButton > button,
    div.stLinkButton > a {
        border-radius: 10px !important;
    }

    div.stButton > button[kind="primary"],
    div.stLinkButton > a[kind="primary"] {
        background: #2f9e44 !important;
        border-color: #2f9e44 !important;
        color: white !important;
    }

    /* -----------------------------------------------------
       RESPONSIVE HEADER
    ----------------------------------------------------- */

    .athlete-header-wrap {
        width: 100%;
        margin-bottom: 18px;
    }

    .school-logo-caption {
        text-align: center;
        color: #4b5563;
        font-size: 13px;
        margin-top: 4px;
    }

    @media (max-width: 720px) {
        .block-container {
            padding-top: 1.4rem;
        }

        .welcome {
            font-size: 28px;
        }
    }


    /* -----------------------------------------------------
       ATHLETE NOTE TEXT
    ----------------------------------------------------- */

    div[data-testid="stTextArea"] textarea {
        color: #111111 !important;
        -webkit-text-fill-color: #111111 !important;
        caret-color: #111111 !important;
    }

    div[data-testid="stTextArea"] textarea::placeholder {
        color: #9ca3af !important;
        -webkit-text-fill-color: #9ca3af !important;
        opacity: 1 !important;
    }


    /* -----------------------------------------------------
       2026 MOBILE ATHLETE APP SHELL
    ----------------------------------------------------- */
    .block-container { max-width: 760px !important; padding-bottom: 7.2rem !important; }
    .mobile-topbar { display:flex; align-items:center; justify-content:space-between; margin:0 0 1.15rem; }
    .mobile-brand { font-size:20px; font-weight:900; letter-spacing:2px; color:#10213c; }
    .mobile-brand span { color:#25a95a; }
    .profile-bubble { width:38px; height:38px; border:1px solid #d9e0e7; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:20px; background:#fff; }
    .mobile-greeting { margin:.1rem 0 1.5rem; }
    .mobile-greeting .welcome { font-size:29px; }
    .mobile-section-title { font-size:28px; font-weight:900; color:#10213c; margin:.4rem 0 .15rem; }
    .mobile-week-range { color:#667085; font-size:15px; margin-bottom:.75rem; }

    /* Turn Streamlit tabs into a phone-style bottom navigation bar. */
    div[data-testid="stTabs"] > div[data-baseweb="tab-list"] {
        position:fixed !important; left:50% !important; bottom:0 !important;
        transform:translateX(-50%) !important; width:min(760px,100vw) !important;
        z-index:9999 !important; background:rgba(255,255,255,.97) !important;
        border-top:1px solid #e5e7eb !important; box-shadow:0 -6px 20px rgba(15,23,42,.06) !important;
        display:grid !important; grid-template-columns:repeat(4,1fr) !important;
        padding:.45rem .55rem calc(.45rem + env(safe-area-inset-bottom)) !important;
        gap:.15rem !important;
    }
    div[data-testid="stTabs"] > div[data-baseweb="tab-list"] button[data-baseweb="tab"] {
        height:54px !important; border-radius:12px !important; justify-content:center !important;
        font-size:13px !important; padding:.35rem .25rem !important;
    }
    div[data-testid="stTabs"] > div[data-baseweb="tab-list"] button[data-baseweb="tab"][aria-selected="true"] {
        background:#eef9f1 !important; color:#24a75a !important;
    }
    div[data-baseweb="tab-highlight"] { display:none !important; }

    /* Compact Strava-like week strip. */
    div[data-testid="stHorizontalBlock"] div.stButton > button {
        min-height:62px; padding:.35rem .15rem !important; font-size:12px !important;
    }

    @media (max-width:720px) {
        .block-container { padding:1rem 1rem 7rem !important; }
        .mobile-brand { font-size:19px; }
        .mobile-section-title { font-size:27px; }
        .mobile-greeting .welcome { font-size:26px; }
        div[data-testid="stTabs"] > div[data-baseweb="tab-list"] button[data-baseweb="tab"] { font-size:12px !important; }
    }

    /* V3: pin the keyed navigation container itself to the phone bottom. */
    .st-key-athlete_bottom_nav {
        position:fixed !important; left:50% !important; bottom:0 !important;
        transform:translateX(-50%) !important; width:min(760px,100vw) !important;
        z-index:9999 !important; background:rgba(255,255,255,.98) !important;
        border-top:1px solid #e5e7eb !important; box-shadow:0 -6px 20px rgba(15,23,42,.06) !important;
        padding:.38rem .55rem calc(.38rem + env(safe-area-inset-bottom)) !important;
    }
    .st-key-athlete_bottom_nav div[data-testid="stHorizontalBlock"] { gap:.15rem !important; }
    .st-key-athlete_bottom_nav div.stButton > button {
        min-height:52px !important; border:0 !important; box-shadow:none !important;
        font-size:12px !important; padding:.2rem .05rem !important; border-radius:10px !important;
    }

    /* V3: all seven days share the available width — no wrap and no horizontal overflow. */
    div[data-testid="stSegmentedControl"] { width:100% !important; overflow:visible !important; padding-bottom:2px; }
    div[data-testid="stSegmentedControl"] > div {
        display:flex !important; flex-wrap:nowrap !important; width:100% !important; min-width:0 !important; gap:4px !important;
    }
    div[data-testid="stSegmentedControl"] button {
        flex:1 1 0 !important; min-width:0 !important; width:auto !important; height:58px !important;
        white-space:normal !important; border-radius:10px !important; font-size:11px !important; padding:3px 2px !important;
    }
    @media (max-width:420px) {
      div[data-testid="stSegmentedControl"] > div { gap:2px !important; }
      div[data-testid="stSegmentedControl"] button { font-size:10px !important; padding:2px 0 !important; }
    }

    /* V5 PHONE LAYOUT: Streamlit normally stacks columns/segments on narrow screens. */
    .st-key-athlete_bottom_nav div[data-testid="stHorizontalBlock"] {
        display:grid !important;
        grid-template-columns:repeat(4,minmax(0,1fr)) !important;
        flex-wrap:nowrap !important;
        gap:4px !important;
        width:100% !important;
    }
    .st-key-athlete_bottom_nav div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        width:100% !important;
        min-width:0 !important;
        flex:none !important;
    }
    .st-key-athlete_bottom_nav div.stButton,
    .st-key-athlete_bottom_nav div.stButton > button { width:100% !important; }
    .st-key-athlete_bottom_nav div.stButton > button {
        min-height:58px !important;
        padding:4px 1px !important;
        font-size:11px !important;
        line-height:1.15 !important;
        white-space:normal !important;
    }

    /* Seven-day selector: hard seven-column grid, never wrap to a second row. */
    div[data-testid="stSegmentedControl"] > div,
    div[data-testid="stSegmentedControl"] [role="radiogroup"] {
        display:grid !important;
        grid-template-columns:repeat(7,minmax(0,1fr)) !important;
        grid-auto-flow:column !important;
        width:100% !important;
        min-width:0 !important;
        gap:3px !important;
        overflow:visible !important;
    }
    div[data-testid="stSegmentedControl"] button,
    div[data-testid="stSegmentedControl"] [role="radio"] {
        width:100% !important;
        min-width:0 !important;
        max-width:none !important;
        margin:0 !important;
        padding:4px 0 !important;
        font-size:10px !important;
        line-height:1.1 !important;
        white-space:normal !important;
        overflow:hidden !important;
    }
    @media (max-width:480px) {
        .st-key-athlete_bottom_nav { padding-left:6px !important; padding-right:6px !important; }
        .st-key-athlete_bottom_nav div[data-testid="stHorizontalBlock"] { gap:2px !important; }
        .st-key-athlete_bottom_nav div.stButton > button { font-size:10px !important; }
        div[data-testid="stSegmentedControl"] > div,
        div[data-testid="stSegmentedControl"] [role="radiogroup"] { gap:2px !important; }
        div[data-testid="stSegmentedControl"] button,
        div[data-testid="stSegmentedControl"] [role="radio"] { font-size:9px !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# LOGIN PAGE
# =========================================================

if not st.session_state.logged_in:

    st.markdown(
        '<div class="vekdyn">VEK<span>DYN</span></div>',
        unsafe_allow_html=True,
    )

    st.title(
        "Athlete Sign In"
    )

    st.caption(
        "Sign in with your VEKDYN athlete account."
    )

    with st.form(
        "athlete_login_form",
        clear_on_submit=False,
    ):

        athlete_id_input = st.text_input(
            "Athlete ID",
            placeholder="Enter Athlete ID",
        )

        password_input = st.text_input(
            "Password",
            type="password",
        )

        submitted = st.form_submit_button(
            "Sign in",
            use_container_width=True,
            type="primary",
        )

    if submitted:

        clean_athlete_id = (
            athlete_id_input
            .strip()
            .lower()
        )

        clean_password = (
            password_input.strip()
        )

        if not clean_athlete_id:

            st.error(
                "Enter your Athlete ID."
            )

        elif not clean_password:

            st.error(
                "Enter your password."
            )

        else:

            authenticated_id = (
                authenticate_athlete(
                    clean_athlete_id,
                    clean_password,
                )
            )

            if authenticated_id is not None:

                authenticated_athlete_id, must_change_password = authenticated_id

                st.session_state.logged_in = True
                st.session_state.athlete_id = authenticated_athlete_id
                st.session_state.password_change_required = must_change_password

                # A temporary password must be replaced before VEKDYN issues
                # the athlete's persistent 30-day browser session.
                if not must_change_password:
                    persistent_token = issue_persistent_athlete_session(
                        authenticated_athlete_id
                    )
                    set_browser_session_token(
                        persistent_token
                    )

                st.rerun()

            else:

                st.error(
                    "Incorrect Athlete ID or password."
                )

    st.stop()


# =========================================================
# FIRST LOGIN — CREATE PERMANENT PASSWORD
# =========================================================

if st.session_state.logged_in and st.session_state.password_change_required:
    st.markdown(
        '<div class="vekdyn">VEK<span>DYN</span></div>',
        unsafe_allow_html=True,
    )

    st.title("Create your password")
    st.caption(
        "Your coach gave you a temporary password. Create your own password "
        "before entering VEKDYN Athlete."
    )

    with st.form("first_login_password_form", clear_on_submit=True):
        new_password = st.text_input(
            "New password",
            type="password",
            help="Use at least 8 characters.",
        )
        confirm_password = st.text_input(
            "Confirm new password",
            type="password",
        )
        create_password_submitted = st.form_submit_button(
            "Create password",
            type="primary",
            use_container_width=True,
        )

    if create_password_submitted:
        if len(new_password) < 8:
            st.error("Your password must be at least 8 characters long.")
        elif new_password != confirm_password:
            st.error("The passwords do not match.")
        else:
            try:
                update_athlete_password(
                    st.session_state.athlete_id,
                    new_password,
                )

                st.session_state.password_change_required = False

                persistent_token = issue_persistent_athlete_session(
                    st.session_state.athlete_id
                )
                set_browser_session_token(persistent_token)

                st.session_state.password_changed_success = True
                st.rerun()

            except (ValueError, RuntimeError) as error:
                st.error(str(error))
            except Exception as error:
                st.error(f"Your password could not be updated: {error}")

    if st.button("Back to sign in", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.athlete_id = None
        st.session_state.password_change_required = False
        st.query_params.clear()
        st.rerun()

    st.stop()


# =========================================================
# LOGGED-IN ATHLETE
# =========================================================

logged_in_athlete_id = (
    st.session_state.athlete_id
)

# Process a Strava OAuth return only after login is established.
handle_strava_callback()


# =========================================================
# ATHLETE PROFILE — RESOLVED FROM THE LOGIN
# =========================================================

try:
    athlete = load_logged_in_athlete_profile(logged_in_athlete_id)
except Exception as error:
    st.error(f"VEKDYN could not load your athlete profile: {error}")
    if st.button("Sign out", key="profile_load_signout"):
        st.session_state.logged_in = False
        st.session_state.athlete_id = None
        st.rerun()
    st.stop()

if athlete is None:
    st.error("This VEKDYN athlete account could not be found or is inactive.")
    if st.button("Sign out", key="missing_profile_signout"):
        st.session_state.logged_in = False
        st.session_state.athlete_id = None
        st.rerun()
    st.stop()


if st.session_state.get("password_changed_success"):
    st.success("Password created successfully. You are signed in to VEKDYN ✓")
    st.session_state.password_changed_success = False


# =========================================================
# TEAM-SPECIFIC ATHLETE THEME
# =========================================================

if athlete.get("team_id") == "dark_horse_endurance":
    st.markdown(
        """
        <style>
        :root { --dh-purple:#7c3cff; --dh-purple-bright:#9a5cff; --dh-purple-soft:#b596ff; --dh-bg:#080911; --dh-card:#11111d; --dh-border:#29263d; --dh-text:#f7f5ff; --dh-muted:#9c96b5; }
        html, body, [data-testid="stAppViewContainer"], .stApp {
          background:radial-gradient(circle at 78% 4%, rgba(80,38,126,.20) 0%, rgba(8,9,17,0) 28%),linear-gradient(180deg,#090a12 0%,#080911 100%) !important;
          color:var(--dh-text) !important;
        }
        .block-container { max-width:620px !important; padding:1.25rem .95rem 7.5rem !important; }
        .mobile-topbar { margin-bottom:1.15rem !important; }
        .mobile-brand { color:#fff !important; font-size:21px !important; letter-spacing:2.5px !important; }
        .mobile-brand span { color:#36d274 !important; }
        .profile-bubble { width:42px !important; height:42px !important; background:#0f1019 !important; border:1px solid #3a3451 !important; color:#f8f6ff !important; }
        .mobile-greeting { margin:.1rem 0 1.5rem !important; }
        .mobile-greeting .welcome { color:#fff !important; font-size:30px !important; font-weight:900 !important; }
        .subtext,.mobile-week-range,div[data-testid="stCaptionContainer"] { color:var(--dh-muted) !important; }
        .mobile-section-title { color:#fff !important; font-size:29px !important; font-weight:900 !important; }
        h1,h2,h3,h4,.vekdyn,.pace-value { color:#fff !important; }
        div[data-testid="stMarkdownContainer"] p { color:#d7d2e5; }
        hr { border-color:#29263d !important; }
        [data-testid="stVerticalBlockBorderWrapper"] { background:linear-gradient(145deg,rgba(20,19,34,.98),rgba(13,13,23,.99)) !important; border:1px solid var(--dh-border) !important; border-radius:13px !important; }
        [data-testid="stAlert"] { background:#12111e !important; border:1px solid #30284b !important; color:#eee9ff !important; }
        div[data-testid="stSegmentedControl"] { width:100% !important; overflow:visible !important; }
        div[data-testid="stSegmentedControl"] > div { display:grid !important; grid-template-columns:repeat(7,minmax(0,1fr)) !important; gap:6px !important; width:100% !important; }
        div[data-testid="stSegmentedControl"] button { min-width:0 !important; width:100% !important; height:70px !important; padding:4px 1px !important; white-space:normal !important; background:#10111b !important; border:1px solid #2c2a40 !important; border-radius:9px !important; color:#aba6bd !important; font-size:11px !important; line-height:1.25 !important; box-shadow:none !important; }
        div[data-testid="stSegmentedControl"] button[aria-pressed="true"], div[data-testid="stSegmentedControl"] button[data-selected="true"] { background:linear-gradient(180deg,#8e4cff,#6830ef) !important; border-color:#9c60ff !important; color:white !important; box-shadow:0 5px 18px rgba(124,60,255,.24) !important; }
        div.stButton > button, div.stLinkButton > a { background:#11111c !important; border:1px solid #302b46 !important; color:#eeeaff !important; border-radius:10px !important; }
        div.stButton > button[kind="primary"], div.stLinkButton > a[kind="primary"] { background:linear-gradient(180deg,#8647ff,#6a31ee) !important; border-color:#955cff !important; color:white !important; }
        [data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea { background:#0f1018 !important; color:#f8f6ff !important; -webkit-text-fill-color:#f8f6ff !important; border-color:#332d4a !important; }
        .st-key-athlete_bottom_nav { background:rgba(9,10,17,.98) !important; border-top:1px solid #252335 !important; box-shadow:0 -10px 30px rgba(0,0,0,.24) !important; backdrop-filter:blur(18px) !important; }
        .st-key-athlete_bottom_nav div.stButton > button { background:transparent !important; border:0 !important; color:#aaa3be !important; font-size:12px !important; min-height:58px !important; }
        .st-key-athlete_bottom_nav div.stButton > button[kind="primary"] { background:transparent !important; color:#8d52ff !important; box-shadow:none !important; }
        .dh-workout-card { margin-top:.75rem; background:linear-gradient(145deg,#151422,#10101a); border:1px solid #2d2940; border-radius:13px; padding:17px 17px 13px; }
        .dh-workout-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:10px; }
        .dh-workout-head-title { color:#fff; font-size:22px; line-height:1.1; font-weight:900; }
        .dh-workout-date { color:#9790ad; font-size:13px; white-space:nowrap; }
        .dh-session { display:grid; grid-template-columns:74px 1fr; gap:14px; align-items:center; padding:13px 0; }
        .dh-session + .dh-session { border-top:1px solid #292638; }
        .dh-session-badge { height:66px; border-radius:11px; display:flex; flex-direction:column; align-items:center; justify-content:center; font-weight:900; font-size:16px; }
        .dh-session-badge.am { background:linear-gradient(145deg,#3a211b,#22171a); color:#ff9a52; }
        .dh-session-badge.pm { background:linear-gradient(145deg,#232047,#19172f); color:#9c72ff; }
        .dh-session-icon { font-size:22px; line-height:1; margin-bottom:3px; }
        .dh-session-title { color:#fff; font-size:17px; font-weight:850; line-height:1.2; }
        .dh-session-detail { color:#9d96b3; font-size:14px; margin-top:5px; line-height:1.35; }
        .dh-note { border-top:1px solid #292638; margin-top:3px; padding:14px 0 5px; display:grid; grid-template-columns:44px 1fr; gap:10px; }
        .dh-note-icon { color:#b1a8c8; font-size:22px; }
        .dh-note-label { color:#938ca8; font-size:12px; margin-bottom:2px; }
        .dh-note-text { color:#eeeaff; font-size:14px; }
        .dh-feedback-shell { margin-top:14px; background:linear-gradient(145deg,#151422,#10101a); border:1px solid #2d2940; border-radius:13px; padding:15px 16px; }
        .dh-feedback-row { display:grid; grid-template-columns:54px 1fr 18px; gap:12px; align-items:center; }
        .dh-feedback-icon { width:50px;height:50px;border-radius:10px;background:#201a37;color:#925cff;display:flex;align-items:center;justify-content:center;font-size:23px; }
        .dh-feedback-title { color:#fff;font-size:16px;font-weight:850; }
        .dh-feedback-sub { color:#9891aa;font-size:12px;margin-top:2px; }
        .dh-feedback-chevron { color:#9e96b3;font-size:22px; }
        @media (max-width:420px) { .block-container { padding-left:.7rem !important; padding-right:.7rem !important; } .mobile-greeting .welcome { font-size:27px !important; } div[data-testid="stSegmentedControl"] > div { gap:4px !important; } div[data-testid="stSegmentedControl"] button { height:66px !important; font-size:10px !important; } .dh-session { grid-template-columns:66px 1fr; gap:12px; } .dh-session-badge { height:62px; } }
        </style>
        """,
        unsafe_allow_html=True,
    )


# =========================================================
# SCHOOL BRANDING
# =========================================================

TEAM_LOGO_CANDIDATES = {
    "sam_houston": [
        "sam_houston.png",
        "sam_houston.jpg",
        "sam_houston.jpeg",
        "sam_houston.webp",
        "shsu.png",
        "shsu.jpg",
    ],
    "ollu_distance": [
        "ollu_distance.png",
        "ollu_distance.jpg",
        "ollu_distance.jpeg",
        "ollu_distance.webp",
        "ollu.png",
        "ollu.jpg",
    ],
    "dark_horse_endurance": [
        "dark_horse_endurance.png",
        "dark_horse_endurance.jpg",
        "dark_horse_endurance.jpeg",
        "dark_horse_endurance.webp",
        "dark_horse.png",
        "dark_horse.jpg",
    ],
}

TEAM_LOGO_LABELS = {
    "sam_houston": "Sam Houston State University",
    "ollu_distance": "Our Lady of the Lake University",
    "dark_horse_endurance": "Dark Horse Endurance",
}


def find_team_logo(team_id):
    """
    Look for the athlete's school logo without changing any database logic.

    Supported folders:
      ./team_logos/
      ./team_images/
      project root
    """

    candidate_names = TEAM_LOGO_CANDIDATES.get(team_id, [])

    search_directories = [
        Path(__file__).with_name("team_logos"),
        Path(__file__).with_name("team_images"),
        Path(__file__).parent,
    ]

    for directory in search_directories:
        for candidate_name in candidate_names:
            candidate_path = directory / candidate_name
            if candidate_path.exists():
                return candidate_path

    return None


# =========================================================
# ATHLETE + COACH NOTES — SHARED NEON FEED
# =========================================================

TEAM_TIMEZONE = ZoneInfo("America/Chicago")


def initialize_notes_database():
    """Create the shared athlete/coach notes table used by both VEKDYN apps."""

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

        database.commit()


def save_athlete_note(athlete_key, author_name, author_role, note_text):
    """Save one note into the same Neon feed the coach dashboard reads."""

    clean_note = str(note_text).strip()
    clean_author = str(author_name).strip()
    clean_role = str(author_role).strip().upper()

    if not clean_note:
        raise ValueError("Write a note before sending.")

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

        database.commit()


def load_athlete_notes(athlete_key, limit=40):
    """Load the shared athlete/coach conversation for the logged-in athlete."""

    initialize_notes_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    author_name,
                    author_role,
                    note_text,
                    created_at
                FROM athlete_notes
                WHERE athlete_key = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (athlete_key, int(limit)),
            )

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
    if not created_at:
        return ""

    try:
        return created_at.astimezone(TEAM_TIMEZONE).strftime(
            "%b %d, %Y · %I:%M %p"
        ).replace(" 0", " ")
    except (AttributeError, ValueError):
        return str(created_at)



# =========================================================
# DAY-SPECIFIC ATHLETE FEEDBACK - NEON
# =========================================================

def initialize_daily_feedback_database():
    """
    One athlete note per calendar day.

    This intentionally replaces a general messaging thread: feedback stays
    attached to the training day the athlete is commenting on.
    """
    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS athlete_daily_feedback (
                    id BIGSERIAL PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    athlete_key TEXT NOT NULL,
                    feedback_date DATE NOT NULL,
                    note_text TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (team_id, athlete_key, feedback_date)
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_athlete_daily_feedback_lookup
                ON athlete_daily_feedback (team_id, athlete_key, feedback_date);
                """
            )
        database.commit()


def load_daily_feedback(feedback_date):
    initialize_daily_feedback_database()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT note_text, updated_at
                FROM athlete_daily_feedback
                WHERE team_id = %s
                  AND athlete_key = %s
                  AND feedback_date = %s
                LIMIT 1;
                """,
                (
                    athlete["team_id"],
                    athlete["athlete_key"],
                    feedback_date,
                ),
            )
            row = cursor.fetchone()

    if not row:
        return {"note_text": "", "updated_at": None}

    return {
        "note_text": row[0] or "",
        "updated_at": row[1],
    }


def save_daily_feedback(feedback_date, note_text):
    initialize_daily_feedback_database()

    cleaned_note = (note_text or "").strip()

    with get_database_connection() as database:
        with database.cursor() as cursor:
            if cleaned_note:
                cursor.execute(
                    """
                    INSERT INTO athlete_daily_feedback (
                        team_id,
                        athlete_key,
                        feedback_date,
                        note_text,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (team_id, athlete_key, feedback_date)
                    DO UPDATE SET
                        note_text = EXCLUDED.note_text,
                        updated_at = NOW();
                    """,
                    (
                        athlete["team_id"],
                        athlete["athlete_key"],
                        feedback_date,
                        cleaned_note,
                    ),
                )
            else:
                cursor.execute(
                    """
                    DELETE FROM athlete_daily_feedback
                    WHERE team_id = %s
                      AND athlete_key = %s
                      AND feedback_date = %s;
                    """,
                    (
                        athlete["team_id"],
                        athlete["athlete_key"],
                        feedback_date,
                    ),
                )
        database.commit()


def render_daily_feedback(feedback_date):
    """
    Compact feedback attached to the selected day.

    Saving a blank note removes that day's feedback.
    """
    try:
        saved = load_daily_feedback(feedback_date)
    except Exception as error:
        st.warning(f"VEKDYN could not load your day note: {error}")
        return

    st.markdown("### How did you feel?")

    # Keep the athlete's typed daily-feedback text black.
    # This is intentionally injected here, after team-theme CSS, so later
    # theme rules cannot turn this field's text white.
    st.markdown(
        """
        <style>
        div[data-testid="stTextArea"] textarea,
        div[data-testid="stTextArea"] textarea:focus,
        div[data-testid="stTextArea"] textarea:active {
            color: #111111 !important;
            -webkit-text-fill-color: #111111 !important;
            caret-color: #111111 !important;
        }
        div[data-testid="stTextArea"] textarea::placeholder {
            color: #9ca3af !important;
            -webkit-text-fill-color: #9ca3af !important;
            opacity: 1 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        "Leave a short note for your coach about this day — for example "
        "how the workout felt, soreness, fatigue, or anything unusual."
    )

    widget_key = f"daily_feedback_{feedback_date.isoformat()}"
    loaded_key = f"{widget_key}_loaded"

    if not st.session_state.get(loaded_key):
        st.session_state[widget_key] = saved["note_text"]
        st.session_state[loaded_key] = True

    with st.form(f"daily_feedback_form_{feedback_date.isoformat()}"):
        note_text = st.text_area(
            "Day note",
            key=widget_key,
            placeholder="Example: Felt smooth today. Legs were a little heavy early but opened up.",
            height=92,
            label_visibility="collapsed",
        )

        save_note = st.form_submit_button(
            "Save day note",
            use_container_width=True,
        )

    if save_note:
        try:
            save_daily_feedback(feedback_date, note_text)
            st.success(
                "Day note saved for your coach."
                if note_text.strip()
                else "Day note cleared."
            )
        except Exception as error:
            st.error(f"VEKDYN could not save your day note: {error}")


# =========================================================
# THRESHOLD TRAINING PACES - NEON
# =========================================================

def get_threshold_profile():
    """
    Load the logged-in athlete's individual threshold profile
    written by the coach in VEKDYN Coach.
    """

    conn = get_database_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    short_lactate,
                    short_pace,
                    medium_lactate,
                    medium_pace,
                    long_lactate,
                    long_pace
                FROM athlete_threshold_profiles
                WHERE team_id = %s
                  AND athlete_key = %s
                LIMIT 1;
                """,
                (
                    athlete["team_id"],
                    athlete["athlete_key"],
                ),
            )

            row = cursor.fetchone()

    except Exception as error:
        st.error(f"Threshold database error: {error}")
        return {
            "short": {"pace": "--", "lactate": None},
            "medium": {"pace": "--", "lactate": None},
            "long": {"pace": "--", "lactate": None},
        }

    finally:
        conn.close()

    if not row:
        return {
            "short": {"pace": "--", "lactate": None},
            "medium": {"pace": "--", "lactate": None},
            "long": {"pace": "--", "lactate": None},
        }

    return {
        "short": {
            "lactate": row[0],
            "pace": row[1] or "--",
        },
        "medium": {
            "lactate": row[2],
            "pace": row[3] or "--",
        },
        "long": {
            "lactate": row[4],
            "pace": row[5] or "--",
        },
    }


threshold_profile = get_threshold_profile()


# =========================================================
# WORKOUT DATABASE
# =========================================================

def get_workouts(
    start_date,
    end_date,
):
    """
    Read workouts written in VEKDYN Coach from the shared
    Neon team_workouts table.

    The athlete receives:

    1. Team workouts:
       athlete_key IS NULL

    2. Individual workouts:
       athlete_key matches this athlete
    """

    conn = get_database_connection()

    try:

        with conn.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    workout_date,
                    workout_type,
                    warm_up,
                    workout,
                    cool_down,
                    notes,
                    athlete_key,
                    video_url,
                    COALESCE(session_slot, 'AM'),
                    effort_level,
                    planned_miles
                FROM team_workouts
                WHERE team_id = %s
                  AND workout_date BETWEEN %s AND %s
                  AND (
                      athlete_key IS NULL
                      OR athlete_key = %s
                  )
                ORDER BY
                    workout_date ASC,
                    CASE WHEN COALESCE(session_slot, 'AM') = 'AM' THEN 0 ELSE 1 END,
                    id ASC;
                """,
                (
                    athlete["team_id"],
                    start_date,
                    end_date,
                    athlete["athlete_key"],
                ),
            )

            rows = cursor.fetchall()

    except Exception as error:

        st.error(
            f"Workout database error: {error}"
        )

        return []

    finally:

        conn.close()


    workouts = []

    for row in rows:

        workouts.append(
            {
                "date": row[0],
                "title": (
                    row[1]
                    or "Training"
                ),
                "warmup": (
                    row[2]
                    or ""
                ),
                "main": (
                    row[3]
                    or ""
                ),
                "cooldown": (
                    row[4]
                    or ""
                ),
                "coach_notes": (
                    row[5]
                    or ""
                ),
                "assigned_to": row[6],
                "video_url": row[7] or "",
                "session": (row[8] or "AM").upper(),
                "effort": row[9] or "",
                "planned_miles": float(row[10]) if row[10] is not None else None,
            }
        )

    return workouts


# =========================================================
# WORKOUT DISPLAY
# =========================================================

def display_workout(
    workout,
):

    session_label = workout.get("session", "AM")
    st.caption(f"{session_label} SESSION")
    st.markdown(f"### {workout['title']}")
    meta = []
    if workout.get("effort"):
        meta.append(f"Effort: {workout['effort']}")
    if workout.get("planned_miles") is not None:
        meta.append(f"Planned: {workout['planned_miles']:g} mi")
    if meta:
        st.caption(" · ".join(meta))

    if workout["warmup"]:

        st.write(
            f"**Warm-up:** "
            f"{workout['warmup']}"
        )

    if workout["main"]:

        st.write(
            f"**Workout:** "
            f"{workout['main']}"
        )

    if workout["cooldown"]:

        st.write(
            f"**Cool-down:** "
            f"{workout['cooldown']}"
        )

    if workout["coach_notes"]:

        st.caption(
            f"Coach note: "
            f"{workout['coach_notes']}"
        )

    if workout.get("video_url"):
        st.markdown("**Coach's workout breakdown**")
        st.video(workout["video_url"])


# =========================================================
# HEADER — MOBILE ATHLETE APP
# =========================================================

first_name = athlete["name"].split()[0]
school_logo = find_team_logo(athlete["team_id"])

st.markdown(
    '<div class="mobile-topbar">'
    '<div class="mobile-brand">VEK<span>DYN</span></div>'
    '<div class="profile-bubble">♙</div>'
    '</div>',
    unsafe_allow_html=True,
)

logo_col, greeting_col = st.columns([0.72, 3.5], vertical_alignment="center")
with logo_col:
    if school_logo:
        if athlete.get("team_id") == "dark_horse_endurance":
            st.image(str(school_logo), width=66)
        else:
            st.image(str(school_logo), use_container_width=True)
    else:
        st.markdown(
            '<div class="profile-bubble" style="width:58px;height:58px;">🏃</div>',
            unsafe_allow_html=True,
        )
with greeting_col:
    st.markdown(
        f'<div class="mobile-greeting">'
        f'<div class="welcome">Good evening, {html.escape(first_name)}.</div>'
        f'<div class="subtext">{html.escape(athlete["team"])} • {html.escape(athlete["event_group"])}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

# =========================================================
# ATHLETE APP NAVIGATION + DAILY TRAINING UX
# =========================================================

def athlete_logout():
    """Fully end the athlete session and return to the unified VEKDYN login."""

    # Revoke the athlete app's Neon-backed persistent session when possible.
    current_token = browser_session_token()
    if current_token:
        try:
            revoke_persistent_athlete_session(current_token)
        except Exception:
            # Logout should still succeed even if Neon is temporarily unavailable.
            pass

    # Clear athlete and unified login state.
    for key in [
        "athlete_id",
        "password_change_required",
        "vekdyn_authenticated_user",
        "vekdyn_role",
        "logged_in_user",
        "active_team",
        "pending_team",
    ]:
        st.session_state.pop(key, None)

    st.session_state["logged_in"] = False
    st.session_state["just_logged_out"] = True

    # Remove athlete/coach/unified persistence tokens and explicitly tell
    # vekdyn_unified.py this rerun is an intentional logout.
    st.query_params.clear()
    st.query_params["logout"] = "1"
    st.rerun()


def workout_day_value(workout):
    value = workout.get("date")
    return value.date() if hasattr(value, "date") else value


def render_day_picker(week_start, workouts, selected_day, key_prefix):
    """Compact horizontal Sunday-Saturday selector that stays on one row on phones."""
    workout_dates = {workout_day_value(item) for item in workouts}
    days = [week_start + timedelta(days=i) for i in range(7)]
    labels = []
    label_to_day = {}
    for day_value in days:
        dot = " •" if day_value in workout_dates else ""
        label = f"{day_value.strftime('%a')}\n{day_value.day}{dot}"
        labels.append(label)
        label_to_day[label] = day_value

    current_label = next((label for label, d in label_to_day.items() if d == selected_day), labels[0])
    choice = st.segmented_control(
        "Workout day",
        options=labels,
        default=current_label,
        key=f"{key_prefix}_segmented",
        label_visibility="collapsed",
    )
    return label_to_day.get(choice, selected_day)


def render_selected_day_workouts(workouts, selected_day):
    """Show only the workout(s) for the day the athlete selected."""

    selected_workouts = [
        item for item in workouts
        if workout_day_value(item) == selected_day
    ]

    if not selected_workouts:
        st.info("No workout has been assigned for this day.")
        return

    for workout_index, workout_item in enumerate(selected_workouts):
        with st.container(border=True):
            display_workout(workout_item)

        if workout_index < len(selected_workouts) - 1:
            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)


def render_dark_horse_day_card(workouts, selected_day, today):
    """Render the Dark Horse home workout card as one compact mobile panel."""
    selected = [item for item in workouts if workout_day_value(item) == selected_day]
    heading = "Today's workout" if selected_day == today else selected_day.strftime("%A's workout")
    date_text = selected_day.strftime("%a, %b %d, %Y")
    if not selected:
        block = (
            '<div class="dh-workout-card"><div class="dh-workout-head">'
            f'<div class="dh-workout-head-title">{html.escape(heading)}</div>'
            f'<div class="dh-workout-date">{html.escape(date_text)}</div></div>'
            '<div class="dh-session-detail">No workout has been assigned for this day.</div></div>'
        )
        st.markdown(block, unsafe_allow_html=True)
        return
    sessions_html=[]
    notes=[]
    for item in selected:
        session=str(item.get("session") or "AM").upper()
        is_pm=session=="PM"
        icon="☾" if is_pm else "☀"
        cls="pm" if is_pm else "am"
        title=html.escape(str(item.get("title") or "Training"))
        parts=[]
        main=str(item.get("main") or "").strip()
        if main: parts.append(main)
        if item.get("effort"): parts.append(f"Effort: {item['effort']}")
        if item.get("planned_miles") is not None: parts.append(f"{item['planned_miles']:g} mi planned")
        if item.get("warmup"): parts.append(f"Warm-up: {item['warmup']}")
        if item.get("cooldown"): parts.append(f"Cool-down: {item['cooldown']}")
        detail=html.escape(" · ".join(parts)) if parts else "Workout details from your coach"
        sessions_html.append(
            f'<div class="dh-session"><div class="dh-session-badge {cls}"><div class="dh-session-icon">{icon}</div>{html.escape(session)}</div>'
            f'<div><div class="dh-session-title">{title}</div><div class="dh-session-detail">{detail}</div></div></div>'
        )
        note=str(item.get("coach_notes") or "").strip()
        if note and note not in notes: notes.append(note)
    notes_html=""
    if notes:
        notes_html=(
            '<div class="dh-note"><div class="dh-note-icon">▣</div><div><div class="dh-note-label">Coach note</div>'
            f'<div class="dh-note-text">{html.escape(" · ".join(notes))}</div></div></div>'
        )
    block=(
        '<div class="dh-workout-card"><div class="dh-workout-head">'
        f'<div class="dh-workout-head-title">{html.escape(heading)}</div>'
        f'<div class="dh-workout-date">{html.escape(date_text)}</div></div>'
        + ''.join(sessions_html) + notes_html + '</div>'
    )
    st.markdown(block, unsafe_allow_html=True)


def render_dark_horse_feedback_intro():
    st.markdown(
        '<div class="dh-feedback-shell"><div class="dh-feedback-row">'
        '<div class="dh-feedback-icon">▥</div><div><div class="dh-feedback-title">How did you feel?</div>'
        '<div class="dh-feedback-sub">Log your readiness, energy, and notes.</div></div>'
        '<div class="dh-feedback-chevron">›</div></div></div>',
        unsafe_allow_html=True,
    )


def render_threshold_paces():
    """
    Show only prescribed threshold paces.

    VEKDYN does not prescribe an easy-run pace here. Easy running remains
    athlete-controlled unless a coach explicitly writes something in the workout.
    """

    st.markdown("### Threshold paces")
    st.caption("Prescribed threshold pace by repetition length.")

    short_col, medium_col, long_col = st.columns(3)

    pace_groups = [
        (short_col, "SHORT REPS", threshold_profile["short"]),
        (medium_col, "MEDIUM REPS", threshold_profile["medium"]),
        (long_col, "LONG REPS", threshold_profile["long"]),
    ]

    for column, label, profile in pace_groups:
        with column:
            with st.container(border=True):
                st.markdown(
                    f'<div class="pace-label">{label}</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f'<div class="pace-value">{profile["pace"]}</div>',
                    unsafe_allow_html=True,
                )

                lactate = profile.get("lactate")
                st.caption(
                    f"{lactate} mmol" if lactate is not None else "-- mmol"
                )


def render_connections_page():
    """Athlete-owned integrations plus account logout."""

    st.header("Connections")
    st.caption("Connect your own training accounts directly to VEKDYN.")

    athlete_key = athlete["athlete_key"]

    # -----------------------------------------------------
    # STRAVA
    # -----------------------------------------------------
    try:
        strava_connection = load_saved_strava_connection(athlete_key)
    except Exception as error:
        strava_connection = {}
        st.warning(f"Could not check Strava connection: {error}")

    with st.container(border=True):
        if strava_connection:
            st.write("🟢 **Strava connected**")
            connected_name = (
                strava_connection.get("strava_name")
                or "Strava athlete"
            )
            st.caption(
                f"Connected as {connected_name}. "
                "Your coach can use the synced training data in VEKDYN Coach."
            )

            reconnect_url = create_strava_login_url(athlete_key)
            if reconnect_url:
                st.link_button(
                    "Reconnect Strava",
                    reconnect_url,
                    use_container_width=True,
                )

        else:
            st.write("⚪ **Strava not connected**")
            st.caption(
                "Authorize your own Strava account here. "
                "Your coach never receives your Strava password."
            )

            strava_url = create_strava_login_url(athlete_key)
            if strava_url:
                st.link_button(
                    "Connect Strava",
                    strava_url,
                    type="primary",
                    use_container_width=True,
                )
            else:
                st.warning(
                    "Strava is not configured yet for this Athlete app."
                )

    if st.session_state.get("strava_success"):
        st.success(st.session_state.pop("strava_success"))

    # -----------------------------------------------------
    # COROS
    # -----------------------------------------------------
    try:
        coros_connection = load_saved_coros_connection(athlete_key)
    except Exception as error:
        coros_connection = {}
        st.warning(f"Could not check COROS connection: {error}")

    with st.container(border=True):
        if coros_connection:
            st.write("🟢 **COROS connected**")
            st.caption(
                "Your COROS account is linked to VEKDYN. "
                "Sleep, resting HR and sleep HRV can be shared with your coach."
            )

            sync_col, reconnect_col = st.columns(2)

            with sync_col:
                if st.button(
                    "Sync Recovery Data",
                    type="primary",
                    use_container_width=True,
                    key="athlete_connections_sync_coros",
                ):
                    try:
                        latest = sync_coros_recovery(athlete_key, days=7)
                        st.session_state["coros_sync_success"] = True
                        st.session_state["coros_latest_recovery"] = latest
                        st.rerun()
                    except (
                        requests.RequestException,
                        RuntimeError,
                        psycopg2.Error,
                    ) as error:
                        st.session_state["coros_sync_error"] = str(error)
                        st.rerun()

            with reconnect_col:
                try:
                    reconnect_coros_url = create_coros_login_url(
                        athlete_key,
                        logged_in_athlete_id,
                    )
                    st.link_button(
                        "Reconnect COROS",
                        str(reconnect_coros_url),
                        use_container_width=True,
                    )
                except (
                    requests.RequestException,
                    RuntimeError,
                    psycopg2.Error,
                ) as error:
                    st.warning(f"COROS reconnect unavailable: {error}")

            try:
                latest_recovery = load_latest_coros_recovery(athlete_key)
            except Exception:
                latest_recovery = {}

            if latest_recovery:
                sleep_minutes = latest_recovery.get("sleep_minutes")
                hrv_avg = latest_recovery.get("hrv_avg")
                recovery_date = latest_recovery.get("date")

                sleep_text = "--"
                if sleep_minutes is not None:
                    sleep_text = (
                        f"{sleep_minutes // 60}h "
                        f"{sleep_minutes % 60}m"
                    )

                hrv_text = (
                    f"{hrv_avg} ms"
                    if hrv_avg is not None
                    else "--"
                )

                sleep_col, hrv_col = st.columns(2)
                with sleep_col:
                    st.metric("Sleep", sleep_text)
                with hrv_col:
                    st.metric("Sleep HRV", hrv_text)

                detail_parts = []

                if latest_recovery.get("sleep_score") is not None:
                    detail_parts.append(
                        f"sleep score {latest_recovery['sleep_score']}"
                    )

                if latest_recovery.get("hrv_baseline") is not None:
                    detail_parts.append(
                        f"HRV baseline "
                        f"{latest_recovery['hrv_baseline']} ms"
                    )

                if latest_recovery.get("hrv_status"):
                    detail_parts.append(
                        str(latest_recovery["hrv_status"])
                    )

                date_text = (
                    recovery_date.strftime("%b %d, %Y")
                    if recovery_date
                    else "latest day"
                )

                if detail_parts:
                    st.caption(
                        f"{date_text} · "
                        + " · ".join(detail_parts)
                    )
                else:
                    st.caption(date_text)

        else:
            st.write("⚪ **COROS not connected**")
            st.caption(
                "Authorize your own COROS account here. "
                "Your coach never receives your COROS password."
            )

            try:
                coros_url = create_coros_login_url(
                    athlete_key,
                    logged_in_athlete_id,
                )
                st.link_button(
                    "Connect COROS",
                    str(coros_url),
                    type="primary",
                    use_container_width=True,
                )
            except (
                requests.RequestException,
                RuntimeError,
                psycopg2.Error,
            ) as error:
                st.warning(f"COROS connection is not ready: {error}")

    if st.session_state.get("coros_success"):
        st.success(st.session_state.pop("coros_success"))

    if st.session_state.pop("coros_sync_success", False):
        st.success("COROS recovery data synced to VEKDYN ✓")

    if st.session_state.get("coros_sync_error"):
        st.warning(
            "COROS sync: "
            + st.session_state.pop("coros_sync_error")
        )

    if st.session_state.get("coros_callback_error"):
        st.warning(
            st.session_state.pop("coros_callback_error")
        )

    st.divider()

    # -----------------------------------------------------
    # ACCOUNT
    # -----------------------------------------------------
    st.markdown("### Account")
    st.caption(
        "Signing out removes this device's persistent VEKDYN session."
    )

    if st.button(
        "Log Out",
        use_container_width=True,
        key="athlete_logout_button",
    ):
        athlete_logout()


# =========================================================
# NAVIGATION
# =========================================================

if "athlete_nav" not in st.session_state:
    st.session_state.athlete_nav = "Home"

nav_labels = ["Home", "Training", "Performance", "Connections"]
nav_icons = {"Home": "⌂", "Training": "🏃", "Performance": "▥", "Connections": "↗"}

# Real bottom navigation: key the container so CSS can reliably pin the whole row.
with st.container(key="athlete_bottom_nav"):
    nav_cols = st.columns(4, gap="small")
    for _i, _label in enumerate(nav_labels):
        with nav_cols[_i]:
            if st.button(
                f"{nav_icons[_label]}\n{_label}",
                key=f"athlete_bottom_nav_{_label.lower()}",
                use_container_width=True,
                type="primary" if st.session_state.athlete_nav == _label else "secondary",
            ):
                st.session_state.athlete_nav = _label
                st.rerun()

active_nav = st.session_state.athlete_nav


# =========================================================
# HOME — CURRENT WEEK / SELECTED DAY
# =========================================================

if active_nav == "Home":
    st.markdown('<div class="mobile-section-title">My workouts</div>', unsafe_allow_html=True)

    today = date.today()
    current_sunday = (
        today
        - timedelta(days=(today.weekday() + 1) % 7)
    )
    current_saturday = current_sunday + timedelta(days=6)

    # Home is the athlete's current week. Keep one selected day, like Final Surge.
    selected_day = st.session_state.home_selected_date
    if not (current_sunday <= selected_day <= current_saturday):
        selected_day = today
        st.session_state.home_selected_date = selected_day

    st.markdown(
        f"<div class='mobile-week-range'>"
        f"{current_sunday.strftime('%b %d')} – "
        f"{current_saturday.strftime('%b %d, %Y')}"
        f"</div>",
        unsafe_allow_html=True,
    )

    current_week_workouts = get_workouts(
        current_sunday,
        current_saturday,
    )

    new_selected_day = render_day_picker(
        current_sunday,
        current_week_workouts,
        selected_day,
        "home_day",
    )

    if new_selected_day != selected_day:
        st.session_state.home_selected_date = new_selected_day
        st.rerun()

    selected_day = st.session_state.home_selected_date

    if athlete.get("team_id") == "dark_horse_endurance":
        render_dark_horse_day_card(current_week_workouts, selected_day, today)
        render_dark_horse_feedback_intro()
        with st.expander("Add / edit today's note", expanded=False):
            render_daily_feedback(selected_day)
    else:
        if selected_day == today:
            st.markdown("### Today's workout")
        else:
            st.markdown(
                f"### {selected_day.strftime('%A, %B %d')}"
            )
        render_selected_day_workouts(current_week_workouts, selected_day)
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
        render_daily_feedback(selected_day)



# =========================================================
# TRAINING — MONTHLY BIG-PICTURE CALENDAR
# =========================================================

def shifted_month(base_date, offset):
    """Return the first day of the month offset from base_date."""
    month_index = (base_date.year * 12 + (base_date.month - 1)) + offset
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def render_month_training_calendar(month_first, workouts):
    """Responsive month calendar: 7-column grid on desktop, agenda cards on phones."""
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(month_first.year, month_first.month)

    workouts_by_day = {}
    for item in workouts:
        workout_date = workout_day_value(item)
        workouts_by_day.setdefault(workout_date, []).append(item)

    today = date.today()
    weekday_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    def workout_lines(day_value):
        lines = []
        for item in workouts_by_day.get(day_value, []):
            title = html.escape(str(item.get("title") or "Training"))
            session = html.escape(str(item.get("session") or "AM").upper())
            effort = html.escape(str(item.get("effort") or "").strip())
            lines.append(f'<div class="cal-workout"><b>{session}</b> · {title}</div>')
            if effort and effort.lower() != title.lower():
                lines.append(f'<div class="cal-effort">{effort}</div>')
        return "".join(lines) or '<div class="cal-empty">—</div>'

    desktop_headers = "".join(
        f'<div class="cal-head">{label}</div>' for label in weekday_labels
    )
    desktop_days = []
    for week in weeks:
        for day_value in week:
            in_month = day_value.month == month_first.month
            classes = "cal-day"
            if not in_month:
                classes += " outside"
            if day_value == today:
                classes += " today"
            dot = '<span class="today-dot">●</span>' if day_value == today else ""
            content = workout_lines(day_value) if in_month else ""
            desktop_days.append(
                f'<div class="{classes}">'
                f'<div class="cal-date">{dot}{day_value.day}</div>{content}</div>'
            )

    mobile_days = []
    for day_number in range(1, calendar.monthrange(month_first.year, month_first.month)[1] + 1):
        day_value = date(month_first.year, month_first.month, day_number)
        day_workouts = workouts_by_day.get(day_value, [])
        if not day_workouts and day_value != today:
            continue
        today_class = " mobile-today" if day_value == today else ""
        mobile_days.append(
            f'<div class="mobile-day{today_class}">'
            f'<div class="mobile-date"><b>{day_value.strftime("%a")}</b>'
            f'<span>{day_value.strftime("%b %d")}</span></div>'
            f'<div class="mobile-workouts">{workout_lines(day_value)}</div></div>'
        )

    if not mobile_days:
        mobile_days.append('<div class="mobile-no-workouts">No workouts scheduled this month.</div>')

    calendar_html = f"""
    <style>
      .training-calendar-desktop {{
        display:grid; grid-template-columns:repeat(7,minmax(0,1fr));
        border:1px solid #dfe5df; border-radius:14px; overflow:hidden;
        background:#fff;
      }}
      .cal-head {{padding:10px 5px;text-align:center;font-weight:800;font-size:13px;
        border-bottom:1px solid #dfe5df;background:#f8faf8;color:#111827;}}
      .cal-day {{min-height:112px;padding:8px;border-right:1px solid #e5e7eb;
        border-bottom:1px solid #e5e7eb;min-width:0;}}
      .cal-day:nth-child(7n) {{border-right:none;}}
      .cal-day.outside {{background:#fafafa;color:#a3a3a3;}}
      .cal-day.today {{background:#f0faf2;}}
      .cal-date {{text-align:right;font-weight:800;font-size:13px;color:#111827;margin-bottom:7px;}}
      .outside .cal-date {{color:#a3a3a3;}}
      .today-dot {{color:#2f9e44;margin-right:4px;}}
      .cal-workout {{font-size:11px;line-height:1.35;color:#374151;overflow-wrap:anywhere;margin-top:4px;}}
      .cal-effort {{font-size:10px;line-height:1.3;color:#6b7280;overflow-wrap:anywhere;margin-top:2px;}}
      .cal-empty {{font-size:12px;color:#9ca3af;}}
      .training-calendar-mobile {{display:none;}}

      @media (max-width:720px) {{
        .training-calendar-desktop {{display:none;}}
        .training-calendar-mobile {{display:block;}}
        .mobile-day {{display:grid;grid-template-columns:82px minmax(0,1fr);gap:12px;
          padding:14px 4px;border-bottom:1px solid #e5e7eb;}}
        .mobile-day.mobile-today {{background:#f0faf2;border-radius:12px;padding-left:10px;padding-right:10px;}}
        .mobile-date {{display:flex;flex-direction:column;font-size:14px;color:#111827;}}
        .mobile-date span {{font-size:12px;color:#6b7280;margin-top:2px;}}
        .mobile-workouts .cal-workout {{font-size:14px;line-height:1.4;margin-top:0;margin-bottom:3px;}}
        .mobile-workouts .cal-effort {{font-size:12px;margin-bottom:4px;}}
        .mobile-no-workouts {{padding:18px 0;color:#6b7280;text-align:center;}}
      }}
    </style>
    <div class="training-calendar-desktop">
      {desktop_headers}{''.join(desktop_days)}
    </div>
    <div class="training-calendar-mobile">
      {''.join(mobile_days)}
    </div>
    """
    st.markdown(calendar_html, unsafe_allow_html=True)


if active_nav == "Training":
    st.header("Training")
    st.caption(
        "See the whole training block. Your Home tab keeps the day-to-day view."
    )

    today = date.today()
    month_first = shifted_month(
        date(today.year, today.month, 1),
        st.session_state.training_month_offset,
    )

    previous_col, current_col, next_col = st.columns([1, 1, 1])

    with previous_col:
        if st.button(
            "← Previous month",
            use_container_width=True,
            key="athlete_prev_month",
        ):
            st.session_state.training_month_offset -= 1
            st.rerun()

    with current_col:
        if st.button(
            "This month",
            use_container_width=True,
            key="athlete_this_month",
        ):
            st.session_state.training_month_offset = 0
            st.rerun()

    with next_col:
        if st.button(
            "Next month →",
            use_container_width=True,
            key="athlete_next_month",
        ):
            st.session_state.training_month_offset += 1
            st.rerun()

    st.markdown(
        f"<div style='text-align:center;font-weight:850;"
        f"font-size:22px;margin:.35rem 0 1rem;'>"
        f"{month_first.strftime('%B %Y')}"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Load the complete visible calendar grid, including spillover days
    # from the previous/next month.
    cal = calendar.Calendar(firstweekday=6)
    visible_weeks = cal.monthdatescalendar(
        month_first.year,
        month_first.month,
    )
    grid_start = visible_weeks[0][0]
    grid_end = visible_weeks[-1][-1]

    month_workouts = get_workouts(grid_start, grid_end)
    render_month_training_calendar(month_first, month_workouts)

    month_only_workouts = [
        item for item in month_workouts
        if workout_day_value(item).month == month_first.month
        and workout_day_value(item).year == month_first.year
    ]

    planned_values = [
        item.get("planned_miles")
        for item in month_only_workouts
        if item.get("planned_miles") is not None
    ]

    if planned_values:
        planned_month_miles = round(sum(planned_values), 1)
        st.caption(
            f"Planned mileage shown this month: {planned_month_miles:g} mi"
        )


# =========================================================
# PERFORMANCE — THRESHOLD + PERFORMANCE TOOLS
# =========================================================

if active_nav == "Performance":
    st.markdown('<div class="mobile-section-title">Performance</div>', unsafe_allow_html=True)
    st.caption("Your coach-prescribed threshold profile and performance tools.")
    render_threshold_paces()

    st.markdown("### Performance calculator")
    st.caption(
        "Performance calculations stay separate from the daily workout feed so Home remains fast and simple."
    )
    with st.container(border=True):
        st.markdown("**Race equivalency**")
        distance_col, minutes_col, seconds_col = st.columns([1.5, 1, 1])
        with distance_col:
            source_distance = st.selectbox(
                "Race distance",
                ["1500m", "Mile", "3K", "5K", "8K", "10K"],
                key="athlete_perf_distance",
            )
        with minutes_col:
            source_minutes = st.number_input(
                "Minutes", min_value=0, max_value=180, value=15, step=1,
                key="athlete_perf_minutes",
            )
        with seconds_col:
            source_seconds = st.number_input(
                "Seconds", min_value=0.0, max_value=59.9, value=0.0, step=0.1,
                key="athlete_perf_seconds",
            )

        distance_m = {"1500m":1500.0, "Mile":1609.344, "3K":3000.0, "5K":5000.0, "8K":8000.0, "10K":10000.0}
        total_seconds = float(source_minutes) * 60.0 + float(source_seconds)
        if total_seconds > 0:
            target_seconds = total_seconds * (5000.0 / distance_m[source_distance]) ** 1.06
            target_minutes = int(target_seconds // 60)
            target_remainder = target_seconds - target_minutes * 60
            st.metric("Equivalent 5K", f"{target_minutes}:{target_remainder:04.1f}")
            st.caption("Training estimate only — not a guarantee of race performance.")


# =========================================================
# CONNECTIONS
# =========================================================

if active_nav == "Connections":
    render_connections_page()