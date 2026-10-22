from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from granola_export.errors import AuthError


def default_auth_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Granola/supabase.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Granola/supabase.json"
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Granola/supabase.json"
    )


def validate_token(token: str) -> str:
    token = token.strip()
    if not token or any(char.isspace() for char in token):
        raise AuthError("The access token is empty or contains whitespace.")
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, UnicodeDecodeError):
        claims = {}
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(expiry, (int, float)) and expiry <= time.time():
        raise AuthError("The access token expired. Open Granola and use --desktop-auth again.")
    return token


def load_token(auth_file: Path | None = None, token_env: str = "GRANOLA_ACCESS_TOKEN") -> str:
    if token := os.environ.get(token_env):
        return validate_token(token)
    path = auth_file or default_auth_path()
    if not path.exists():
        detail = (
            "Granola encrypts its session file."
            if path.with_suffix(".json.enc").exists()
            else (f"No session file found at {path}.")
        )
        raise AuthError(f"{detail} Use --desktop-auth or set {token_env} to an access token.")
    try:
        data = json.loads(path.read_text())
        tokens = data.get("workos_tokens", data)
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        token = tokens["access_token"]
        if not isinstance(token, str):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise AuthError("Cannot read a WorkOS access token from the auth file.") from error
    return validate_token(token)


def receive_desktop_token(announce: Callable[[str], None], timeout: float = 180) -> str:
    """Receive one token in memory from the user's Granola developer console."""
    nonce = secrets.token_urlsafe(32)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_args: object) -> None:
            pass

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != f"/{nonce}":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length < 32768:
                    raise ValueError
                token = validate_token(self.rfile.read(length).decode())
            except (ValueError, UnicodeDecodeError, AuthError, TimeoutError):
                self.send_error(400, "Invalid token")
                return
            received.append(token)
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"Token received for this export only.")

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        address = f"http://127.0.0.1:{server.server_port}/{nonce}"
        announce(
            "In Granola: View > Toggle Developer Tools > Console.\n"
            "Some versions require selecting Toggle Developer Tools five times.\n"
            "Review and run this command:\n\n"
            f"void window.electron.getRefreshedAccessToken().then(t => fetch('{address}', "
            "{method:'POST',body:t})).then(r => {if (!r.ok) throw Error('Connection failed'); "
            "console.log('Export connected')});\n\n"
            "Waiting for Granola; the token stays in memory and is never printed or saved."
        )
        deadline = time.monotonic() + timeout
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received:
        raise AuthError("Timed out waiting for Granola. Run again with --desktop-auth.")
    return received[0]
