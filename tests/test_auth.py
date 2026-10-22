import base64
import json
import threading
from urllib.request import Request, urlopen

import pytest

from granola_export.auth import load_token, receive_desktop_token, validate_token
from granola_export.errors import AuthError


def test_legacy_double_encoded_tokens_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("GRANOLA_ACCESS_TOKEN", raising=False)
    path = tmp_path / "supabase.json"
    path.write_text(json.dumps({"workos_tokens": json.dumps({"access_token": "from-file"})}))
    assert load_token(path) == "from-file"
    monkeypatch.setenv("GRANOLA_ACCESS_TOKEN", "from-env")
    assert load_token(path) == "from-env"


def test_encrypted_auth_is_actionable_without_leaking_content(tmp_path, monkeypatch):
    monkeypatch.delenv("GRANOLA_ACCESS_TOKEN", raising=False)
    path = tmp_path / "supabase.json"
    path.with_suffix(".json.enc").write_bytes(b"encrypted-secret")
    with pytest.raises(AuthError, match="--desktop-auth") as error:
        load_token(path)
    assert "encrypted-secret" not in str(error.value)


def test_expired_token_is_rejected():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
    with pytest.raises(AuthError, match="expired"):
        validate_token(f"header.{payload}.signature")


def test_desktop_bridge_receives_token_without_announcing_it():
    messages = []
    workers = []

    def announce(message):
        messages.append(message)
        url = message.split("fetch('")[1].split("'")[0]

        def submit():
            with urlopen(
                Request(url, data=b"private-access-token", method="POST"), timeout=5
            ) as response:
                assert response.status == 200

        worker = threading.Thread(target=submit)
        workers.append(worker)
        worker.start()

    assert receive_desktop_token(announce, timeout=5) == "private-access-token"
    for worker in workers:
        worker.join()
    assert "private-access-token" not in "".join(messages)
