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

COROS_MCP_URL = "https://mcpus.coros.com/mcp"
COROS_TIMEZONE = "America/Chicago"
COROS_PROTOCOL_VERSION = "2025-06-18"
COROS_REDIRECT_URI = st.secrets.get(
    "ATHLETE_COROS_REDIRECT_URI",
    st.secrets.get("ATHLETE_STRAVA_REDIRECT_URI", "http://localhost:8501"),
)


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

    if not redirect_uri.startswith(("https://", "http://localhost")):
        raise RuntimeError(
            "ATHLETE_COROS_REDIRECT_URI must be an HTTPS URL (or localhost for testing)."
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
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
        caret-color: #ffffff !important;
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
        :root { --dh-purple:#9b3cff; --dh-purple-soft:#b46cff; --dh-border:#4c246d; --dh-text:#f7f4fb; --dh-muted:#b9afc4; }
        .stApp { background:radial-gradient(circle at 70% 0%,#130a1d 0%,#07070a 32%,#030305 78%) !important; color:var(--dh-text) !important; }
        .block-container { max-width:1180px !important; padding-top:2rem !important; }
        .vekdyn,.welcome,.pace-value,h1,h2,h3,h4 { color:var(--dh-text) !important; }
        .vekdyn span { color:#38d477 !important; }
        .subtext,.school-logo-caption,.pace-label,div[data-testid="stCaptionContainer"],div[data-testid="stMarkdownContainer"] p { color:var(--dh-muted) !important; }
        button[data-baseweb="tab"] { color:#cfc5d8 !important; font-weight:700 !important; }
        button[data-baseweb="tab"][aria-selected="true"] { color:var(--dh-purple-soft) !important; }
        div[data-baseweb="tab-highlight"] { background-color:var(--dh-purple) !important; }
        [data-testid="stVerticalBlockBorderWrapper"] { background:linear-gradient(135deg,rgba(30,18,42,.96),rgba(12,10,17,.98)) !important; border:1px solid var(--dh-border) !important; border-radius:14px !important; box-shadow:inset 0 1px 0 rgba(180,108,255,.05); }
        hr { border-color:#3b214d !important; }
        div.stButton > button,div.stLinkButton > a { background:#110d18 !important; border:1px solid #563078 !important; color:#f7f4fb !important; border-radius:10px !important; }
        div.stButton > button:hover,div.stLinkButton > a:hover { border-color:var(--dh-purple) !important; color:white !important; }
        div.stButton > button[kind="primary"],div.stLinkButton > a[kind="primary"] { background:linear-gradient(135deg,#6d20bd,#a23cff) !important; border-color:#a64cff !important; color:white !important; box-shadow:0 0 18px rgba(155,60,255,.18); }
        [data-testid="stAlert"] { background:#160f20 !important; border:1px solid #542c76 !important; color:#eee8f5 !important; }
        [data-testid="stTextInput"] input,[data-testid="stTextArea"] textarea { background:#0c0910 !important; color:white !important; border-color:#4b2867 !important; }
        .school-logo-caption { color:#a96aff !important; letter-spacing:.08em; text-transform:uppercase; }
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
# HEADER
# =========================================================

st.markdown(
    '<div class="vekdyn">VEK<span>DYN</span></div>',
    unsafe_allow_html=True,
)

first_name = (
    athlete["name"]
    .split()[0]
)

school_logo = find_team_logo(
    athlete["team_id"]
)

header_text_col, header_logo_col = st.columns(
    [3.2, 1.2],
    vertical_alignment="center",
)

with header_text_col:
    st.markdown(
        f'<div class="welcome">'
        f'Good evening, {first_name}.'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        f'<div class="subtext">'
        f'{athlete["team"]} • '
        f'{athlete["event_group"]}'
        f'</div>',
        unsafe_allow_html=True,
    )

with header_logo_col:
    if school_logo:
        st.image(
            str(school_logo),
            use_container_width=True,
        )

        st.markdown(
            f'<div class="school-logo-caption">'
            f'{TEAM_LOGO_LABELS.get(athlete["team_id"], athlete["team"])}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        # Clean fallback if a logo file has not been added yet.
        st.markdown(
            f"""
            <div style="
                min-height: 118px;
                border: 1px solid #dfe5df;
                border-radius: 14px;
                background: #ffffff;
                display: flex;
                align-items: center;
                justify-content: center;
                text-align: center;
                padding: 18px;
                color: #6b7280;
                font-weight: 700;
            ">
                {TEAM_LOGO_LABELS.get(athlete["team_id"], athlete["team"])}
            </div>
            """,
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
    """Final-Surge-style compact Sunday-Saturday selector."""

    workout_dates = {workout_day_value(item) for item in workouts}

    day_columns = st.columns(7)
    for day_index, day_column in enumerate(day_columns):
        day_value = week_start + timedelta(days=day_index)
        has_workout = day_value in workout_dates

        # Keep the button compact. The dot tells the athlete a workout exists.
        label = f"{day_value.strftime('%a')}\n{day_value.day}"
        if has_workout:
            label += " •"

        with day_column:
            if st.button(
                label,
                key=f"{key_prefix}_{day_value.isoformat()}",
                use_container_width=True,
                type="primary" if day_value == selected_day else "secondary",
            ):
                return day_value

    return selected_day


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

tab_home, tab_training, tab_connections = st.tabs(
    [
        "Home",
        "Training",
        "Connections",
    ]
)


# =========================================================
# HOME — CURRENT WEEK / SELECTED DAY
# =========================================================

with tab_home:
    st.markdown("## My workouts")

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
        f"<div style='text-align:center;font-weight:800;"
        f"font-size:18px;margin:.25rem 0 .8rem;'>"
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

    if selected_day == today:
        st.markdown("### Today's workout")
    else:
        st.markdown(
            f"### {selected_day.strftime('%A, %B %d')}"
        )

    render_selected_day_workouts(
        current_week_workouts,
        selected_day,
    )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    render_daily_feedback(selected_day)

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    render_threshold_paces()


# =========================================================
# TRAINING — MONTHLY BIG-PICTURE CALENDAR
# =========================================================

def shifted_month(base_date, offset):
    """Return the first day of the month offset from base_date."""
    month_index = (base_date.year * 12 + (base_date.month - 1)) + offset
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def render_month_training_calendar(month_first, workouts):
    """
    Native Streamlit month calendar.

    Avoids a large raw-HTML grid so Streamlit cannot display the calendar
    markup as literal text.
    """
    cal = calendar.Calendar(firstweekday=6)
    weeks = cal.monthdatescalendar(month_first.year, month_first.month)

    workouts_by_day = {}
    for item in workouts:
        workout_date = workout_day_value(item)
        workouts_by_day.setdefault(workout_date, []).append(item)

    # Sunday-Saturday header.
    header_cols = st.columns(7)
    for col, label in zip(
        header_cols,
        ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    ):
        with col:
            st.markdown(
                f"<div style='text-align:center;font-weight:800;"
                f"font-size:13px;padding:6px 0;'>{label}</div>",
                unsafe_allow_html=True,
            )

    today = date.today()

    for week_index, week in enumerate(weeks):
        day_cols = st.columns(7)

        for col, day_value in zip(day_cols, week):
            with col:
                in_month = day_value.month == month_first.month
                is_today = day_value == today
                day_workouts = workouts_by_day.get(day_value, [])

                if not in_month:
                    st.markdown(
                        f"<div style='text-align:right;color:#a3a3a3;"
                        f"font-size:12px;padding:4px 2px;'>"
                        f"{day_value.day}</div>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        "<div style='height:76px'></div>",
                        unsafe_allow_html=True,
                    )
                    continue

                if is_today:
                    st.markdown(
                        f"<div style='text-align:right;font-weight:900;"
                        f"font-size:14px;'>● {day_value.day}</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='text-align:right;font-weight:800;"
                        f"font-size:13px;'>{day_value.day}</div>",
                        unsafe_allow_html=True,
                    )

                if day_workouts:
                    for item in day_workouts:
                        title = str(item.get("title") or "Training")
                        session = str(item.get("session") or "AM").upper()
                        effort = str(item.get("effort") or "").strip()

                        st.caption(f"{session} · {title}")
                        if effort and effort.lower() != title.lower():
                            st.caption(effort)
                else:
                    st.caption("—")

                st.markdown(
                    "<div style='min-height:42px'></div>",
                    unsafe_allow_html=True,
                )

        if week_index < len(weeks) - 1:
            st.markdown(
                "<hr style='margin:2px 0 8px;border:none;"
                "border-top:1px solid rgba(128,128,128,.18);'>",
                unsafe_allow_html=True,
            )


with tab_training:
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
# CONNECTIONS
# =========================================================

with tab_connections:
    render_connections_page()