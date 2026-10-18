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
            content = [type("B", (), {"type": "text", "text": '{"answer": "yes", "vibrations": 2}'})()]
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


def test_answer_is_broadcast_to_subscribers(client):
    import asyncio
    q = asyncio.Queue()
    main.subscribers.add(q)
    try:
        r = post(client, json={"image_base64": base64.b64encode(PNG).decode()})
    finally:
        main.subscribers.discard(q)
    msg = q.get_nowait()
    assert msg["vibrations"] == 2
    assert msg["id"] == r.json()["id"]


def test_event_stream_formats_and_cleans_up():
    import asyncio

    async def run():
        q = asyncio.Queue()
        main.subscribers.add(q)
        gen = main.event_stream(q)
        q.put_nowait({"id": 1, "answer": "yes", "vibrations": 2})
        chunk = await gen.__anext__()
        await gen.aclose()
        return chunk

    chunk = asyncio.run(run())
    assert chunk.startswith("data: ") and '"vibrations": 2' in chunk
    assert not main.subscribers


def test_events_requires_token(monkeypatch):
    monkeypatch.setattr(main, "APP_TOKEN", "secret")
    assert TestClient(main.app).get("/events?token=wrong").status_code == 401


def test_latest_image_and_replay(client):
    import asyncio
    main.latest = None
    assert client.get("/latest-image").status_code == 404
    r = post(client, json={"image_base64": base64.b64encode(PNG).decode()})
    img = client.get("/latest-image")
    assert img.status_code == 200 and img.content == PNG
    assert img.headers["content-type"] == "image/png"

    async def first_chunk():
        resp = await main.events()
        chunk = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return chunk

    chunk = asyncio.run(first_chunk())
    assert '"replay": true' in chunk
    assert f'"image_url": "/latest-image?id={r.json()["id"]}"' in chunk
    assert not main.subscribers


def test_latest_image_requires_token(monkeypatch):
    monkeypatch.setattr(main, "APP_TOKEN", "secret")
    assert TestClient(main.app).get("/latest-image?token=wrong").status_code == 401


SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}


@pytest.fixture
def push(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SUBS_FILE", str(tmp_path / "subs.json"))
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "subscriptions", {})
    monkeypatch.setattr(main, "VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setattr(main, "VAPID_PUBLIC_KEY", "pub")
    return tmp_path


def test_push_key_and_subscribe_persist(client, push):
    assert client.get("/push/key").json() == {"key": "pub"}
    r = client.post("/push/subscribe", json=SUB)
    assert r.status_code == 200 and r.json()["devices"] == 1
    client.post("/push/subscribe", json=SUB)  # same device again is an upsert
    assert main.load_subscriptions() == {SUB["endpoint"]: SUB}


def test_push_subscribe_rejects_bad_body(client, push):
    assert client.post("/push/subscribe", json={"endpoint": "https://x"}).status_code == 400
    assert client.post("/push/subscribe", json={**SUB, "endpoint": "http://x"}).status_code == 400
    assert client.post("/push/subscribe", content=b"nope").status_code == 400


def test_push_disabled_without_keys(client, push, monkeypatch):
    monkeypatch.setattr(main, "VAPID_PRIVATE_KEY", None)
    assert client.get("/push/key").status_code == 404
    assert client.post("/push/subscribe", json=SUB).status_code == 404


def test_push_requires_token(push, monkeypatch):
    monkeypatch.setattr(main, "APP_TOKEN", "secret")
    c = TestClient(main.app)
    assert c.get("/push/key").status_code == 401
    assert c.post("/push/subscribe", json=SUB).status_code == 401


def test_push_all_sends_and_prunes_gone_subscriptions(push, monkeypatch):
    import asyncio
    gone = {**SUB, "endpoint": "https://push.example/gone"}
    main.subscriptions.update({SUB["endpoint"]: SUB, gone["endpoint"]: gone})
    sent = []

    def fake_send(sub, payload):
        sent.append((sub["endpoint"], payload))
        return sub["endpoint"] != gone["endpoint"]

    monkeypatch.setattr(main, "send_one", fake_send)
    asyncio.run(main.push_all({"id": 7, "answer": "yes", "vibrations": 2}))
    assert len(sent) == 2
    assert '"vibrations": 2' in sent[0][1]
    assert list(main.subscriptions) == [SUB["endpoint"]]
    assert list(main.load_subscriptions()) == [SUB["endpoint"]]


def test_send_one_reports_gone_on_410(monkeypatch):
    class Resp:
        status_code = 410

    def boom(*a, **kw):
        raise main.WebPushException("gone", response=Resp())

    monkeypatch.setattr(main, "webpush", boom)
    assert main.send_one(SUB, "{}") is False


def test_broadcast_schedules_push(monkeypatch):
    import asyncio
    calls = []

    async def fake_push_all(event):
        calls.append(event["id"])

    monkeypatch.setattr(main, "push_all", fake_push_all)

    async def run():
        main.broadcast({"id": 5, "answer": "x", "vibrations": 1}, PNG, "image/png")
        await asyncio.sleep(0)

    asyncio.run(run())
    assert calls == [5]


def test_service_worker_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "showNotification" in r.text
    assert "javascript" in r.headers["content-type"]
