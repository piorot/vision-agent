import base64
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from fastapi.testclient import TestClient

import main

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class FakeMessages:
    async def create(self, **kwargs):
        class Msg:
            content = [type("B", (), {"text": '{"answer": "yes", "vibrations": 2}'})()]
        return Msg()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.client, "messages", FakeMessages())
    return TestClient(main.app)


def post(client, **kw):
    return client.post("/ask/base64", **kw)


def test_success(client):
    r = post(client, json={"image_base64": base64.b64encode(PNG).decode()})
    assert r.status_code == 200
    assert r.json()["vibrations"] == 2


def test_invalid_json(client):
    r = post(client, content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "not valid JSON" in r.json()["detail"]


def test_non_object_body(client):
    r = post(client, json=["x"])
    assert r.status_code == 400
    assert "JSON object" in r.json()["detail"]


def test_missing_field(client):
    r = post(client, json={"question": "hi"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Missing required field: image_base64"


def test_wrong_field_type(client):
    r = post(client, json={"image_base64": 123})
    assert r.status_code == 400
    assert "image_base64 must be a string" in r.json()["detail"]


def test_empty_base64(client):
    r = post(client, json={"image_base64": "data:image/png;base64,"})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_bad_base64(client):
    r = post(client, json={"image_base64": "!!!not base64!!!"})
    assert r.status_code == 400
    assert "not valid base64" in r.json()["detail"]


def test_unsupported_image(client):
    r = post(client, json={"image_base64": base64.b64encode(b"hello world").decode()})
    assert r.status_code == 415
    assert "Unsupported image type" in r.json()["detail"]
