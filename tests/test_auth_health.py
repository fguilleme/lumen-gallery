import os

import pytest
from fastapi.testclient import TestClient

from app import Settings, create_app


def make_client(tmp_path, *, auth_required=True):
    source = tmp_path / "source"
    source.mkdir()
    settings = Settings(
        project_dir=tmp_path,
        photo_root=source,
        data_dir=tmp_path / "data",
        access_password="correct-password",
        session_secret="test-session-secret",
        auth_required=auth_required,
    )
    return TestClient(create_app(settings))


def test_public_index_and_health_do_not_require_session(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>gallery</html>", encoding="utf-8")
    settings = Settings(
        project_dir=tmp_path,
        photo_root=tmp_path / "photos",
        data_dir=tmp_path / "data",
        access_password="secret",
        session_secret="session-secret",
    )
    client = TestClient(create_app(settings))

    assert client.get("/").status_code == 200
    assert client.get("/").text == "<html>gallery</html>"
    assert client.get("/health").status_code == 200


def test_generated_password_and_session_secret_are_separate_private_files(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_PASSWORD", raising=False)
    monkeypatch.delenv("PHOTO_SESSION_SECRET", raising=False)

    Settings(project_dir=tmp_path)

    password_file = tmp_path / "data" / "access-password"
    session_file = tmp_path / "data" / "session-secret"
    assert password_file.read_text().strip()
    assert session_file.read_text().strip()
    assert password_file.read_text() != session_file.read_text()
    assert os.stat(password_file).st_mode & 0o777 == 0o600
    assert os.stat(session_file).st_mode & 0o777 == 0o600


def test_photo_auth_required_defaults_to_true(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_AUTH_REQUIRED", raising=False)

    settings = Settings.from_env(tmp_path)

    assert settings.auth_required is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " Off "])
def test_photo_auth_required_accepts_explicit_false_values(tmp_path, monkeypatch, value):
    monkeypatch.setenv("PHOTO_AUTH_REQUIRED", value)

    settings = Settings.from_env(tmp_path)

    assert settings.auth_required is False


def test_state_changing_cross_origin_requests_are_rejected(tmp_path):
    client = make_client(tmp_path)

    rejected = client.post(
        "/api/login",
        json={"password": "correct-password"},
        headers={"Origin": "https://evil.example"},
    )

    assert rejected.status_code == 403
    assert client.get("/api/session").json() == {"authenticated": False}


def test_all_photo_and_scan_apis_require_session(tmp_path):
    client = make_client(tmp_path)

    assert client.post("/api/scan").status_code == 401
    assert client.post("/api/upload", files=[("files", ("x.jpg", b"x", "image/jpeg"))]).status_code == 401
    assert client.get("/api/photos").status_code == 401
    assert client.get("/api/facets").status_code == 401
    assert client.get("/api/photos/1").status_code == 401
    assert client.get("/api/photos/1/thumb").status_code == 401
    assert client.get("/api/photos/1/preview").status_code == 401
    assert client.get("/api/photos/1/stream").status_code == 401
    assert client.get("/api/photos/1/download").status_code == 401


def test_passwordless_mode_allows_anonymous_reads_and_reports_access_mode(tmp_path):
    client = make_client(tmp_path, auth_required=False)

    assert client.get("/api/session").json() == {"authenticated": True, "auth_required": False}
    assert client.get("/api/photos").status_code == 200
    assert client.get("/api/facets").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/photos/1").status_code == 404
    assert client.get("/api/photos/1/thumb").status_code == 404
    assert client.get("/api/photos/1/preview").status_code == 404
    assert client.get("/api/photos/1/download").status_code == 404


def test_passwordless_mode_keeps_cross_origin_scan_protection(tmp_path):
    client = make_client(tmp_path, auth_required=False)

    rejected = client.post("/api/scan", headers={"Origin": "https://evil.example"})

    assert rejected.status_code == 403


def test_session_login_and_logout_protect_private_api(tmp_path):
    client = make_client(tmp_path)

    assert client.get("/api/session").json() == {"authenticated": False}
    assert client.get("/api/status").status_code == 401

    bad = client.post("/api/login", json={"password": "wrong"})
    assert bad.status_code == 401
    assert client.get("/api/session").json() == {"authenticated": False}

    login = client.post("/api/login", json={"password": "correct-password"})
    assert login.status_code == 200
    assert login.json() == {"authenticated": True}
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/api/session").json() == {"authenticated": True}
    assert client.get("/api/status").status_code == 200

    logout = client.post("/api/logout")
    assert logout.status_code == 200
    assert logout.json() == {"authenticated": False}
    assert client.get("/api/session").json() == {"authenticated": False}
    assert client.get("/api/status").status_code == 401
