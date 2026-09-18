"""taskapi test suite — end-to-end flows through TestClient."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("TASKAPI_DATA", "/tmp/opencode/taskapi-test-data")

from fasterapi import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKAPI_DATA", str(tmp_path))
    from app.db import store as store_mod

    # Single patch point: lifespan seeds it, dependencies read it.
    monkeypatch.setattr(store_mod, "store", store_mod.Store())
    with TestClient(create_app()) as c:
        yield c, store_mod.store


def _login(client, username="demo", password="demo1234"):
    r = client.post("/auth/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_and_me(client):
    c, _ = client
    assert c.post("/auth/token", data={"username": "demo", "password": "nope"}).status_code == 401
    headers = _login(c)
    assert c.get("/auth/me", headers=headers).json()["username"] == "demo"
    assert c.get("/auth/me").status_code == 401


def test_task_crud(client):
    c, _ = client
    h = _login(c)
    assert c.post("/tasks", json={"title": "", "priority": 9}, headers=h).status_code == 422
    created = c.post("/tasks", json={"title": "Write docs", "priority": 1}, headers=h).json()
    assert created["status"] == "open" and created["id"] == 1
    assert c.get("/tasks", headers=h).json()["total"] == 1
    assert c.get("/tasks/999", headers=h).status_code == 404
    patched = c.patch("/tasks/1", json={"status": "done"}, headers=h).json()
    assert patched["status"] == "done"
    assert c.get("/tasks?status=done", headers=h).json()["total"] == 1
    assert c.get("/tasks?status=open", headers=h).json()["total"] == 0
    assert c.delete("/tasks/1", headers=h).status_code == 204
    assert c.get("/tasks", headers=h).json()["total"] == 0


def test_completion_feeds_activity(client):
    c, fresh = client
    h = _login(c)
    c.post("/tasks", json={"title": "Ship it"}, headers=h)
    c.patch("/tasks/1", json={"status": "done"}, headers=h)
    feed = fresh.read_feed()
    assert feed and feed[-1]["kind"] == "task.done" and "#1" in feed[-1]["text"]


def test_file_roundtrip(client):
    c, _ = client
    h = _login(c)
    r = c.post("/files", files={"file": ("notes.txt", b"hello-files", "text/plain")}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["size"] == 11
    dl = c.get("/files/notes.txt", headers=h)
    assert dl.status_code == 200 and dl.content == b"hello-files"
    assert c.get("/files/missing.txt", headers=h).status_code == 404


def test_feed_streams_sse(client):
    c, _ = client
    h = _login(c)
    c.post("/tasks", json={"title": "A"}, headers=h)
    c.patch("/tasks/1", json={"status": "done"}, headers=h)
    r = c.get("/feed", headers=h)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: activity" in r.text and "data:" in r.text


def test_health_and_docs(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/docs").status_code == 200
    assert "paths" in c.get("/openapi.json").json()
