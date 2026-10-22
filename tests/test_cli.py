from granola_export.cli import main


def test_missing_auth_returns_error_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GRANOLA_ACCESS_TOKEN", raising=False)
    assert main(["export", "--auth-file", str(tmp_path / "absent")]) == 1
    output = capsys.readouterr()
    assert "--desktop-auth" in output.err
    assert "Traceback" not in output.err
