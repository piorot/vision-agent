import asyncio
import base64
import itertools
import json
import logging
import os
import re
import sys

import anthropic
from fastapi import FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from pywebpush import WebPushException, webpush

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vision-agent")

app = FastAPI()
client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY

MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
APP_TOKEN = os.getenv("APP_TOKEN")  # optional shared secret for /ask
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")  # Web Push is off unless both VAPID keys are set
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "https://vision-agent.fly.dev")
DATA_DIR = os.getenv("DATA_DIR", "data")  # on Fly this is the mounted volume
SUBS_FILE = os.path.join(DATA_DIR, "push_subscriptions.json")
MAX_SUBSCRIPTIONS = 50
ALLOWED = {"image/jpeg", "image/png", "image/gif", "image/webp"}

SYSTEM = (
    "You answer the question shown in the image. Reply with ONLY a JSON object: "
    '{"answer": "<short answer>", "vibrations": <integer 0-10>}. '
    "If the answer is a number from 0 to 10, vibrations equals that number. "
    "Otherwise: yes=2, no=1, multiple-choice A=1 B=2 C=3 D=4, anything else=0."
)


# Pages currently listening on /events. In-memory only, so this assumes a single machine.
subscribers: set[asyncio.Queue] = set()
answer_ids = itertools.count(1)
latest: dict | None = None  # {"event": {...}, "image": bytes, "media_type": str}; only the newest is kept
KEEPALIVE_SECONDS = 25


def load_subscriptions() -> dict[str, dict]:
    try:
        with open(SUBS_FILE) as f:
            return {sub["endpoint"]: sub for sub in json.load(f)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_subscriptions():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SUBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(list(subscriptions.values()), f)
    os.replace(tmp, SUBS_FILE)  # atomic, so a crash never leaves a half-written file


subscriptions: dict[str, dict] = load_subscriptions()  # Web Push endpoint -> subscription
push_tasks: set[asyncio.Task] = set()

MAX_BYTES = 5_000_000
LOG_CHARS = 300


def clip(text: str) -> str:
    """One-line, length-capped repr for log output."""
    return repr(text if len(text) <= LOG_CHARS else text[:LOG_CHARS] + "...")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Report where the problem is, without echoing the (possibly binary) request body.
    problems = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": problems}, status_code=422)


def sniff_media_type(data: bytes) -> str | None:
    """Detect the real image type from magic bytes; client-supplied labels can be wrong."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class Base64Request(BaseModel):
    image_base64: str
    media_type: str | None = None  # optional hint; the real type is detected from the bytes
    question: str = ""


def check_token(x_token: str | None):
    if APP_TOKEN and x_token != APP_TOKEN:
        raise HTTPException(401, "Bad token")


async def answer_image(data: bytes, question: str):
    media_type = sniff_media_type(data)
    if media_type not in ALLOWED:
        raise HTTPException(415, "Unsupported image type; send JPEG, PNG, GIF or WebP (convert HEIC to JPEG first)")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Image too large (max 5MB)")

    try:
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data).decode()}},
                    {"type": "text", "text": question or "Answer the question in the image."},
                ],
            }],
        )
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Upstream model error ({e.status_code})")
    text = next((b.text for b in msg.content if b.type == "text"), "").strip()
    try:
        result = json.loads(text[text.index("{"): text.rindex("}") + 1])
        result["vibrations"] = max(0, min(10, int(result.get("vibrations", 0))))
    except (ValueError, TypeError):
        result = {"answer": text, "vibrations": 0}
    result["id"] = next(answer_ids)
    log.info("question=%s answer=%s vibrations=%s", clip(question), clip(str(result.get("answer", ""))), result["vibrations"])
    broadcast(result, data, media_type)
    return result


def broadcast(result: dict, image: bytes, media_type: str):
    """Remember the newest answer + image and push the answer to every open page."""
    global latest
    event = {**result, "image_url": f"/latest-image?id={result['id']}"}
    latest = {"event": event, "image": image, "media_type": media_type}
    for q in subscribers:
        q.put_nowait(event)
    task = asyncio.get_running_loop().create_task(push_all(event))
    push_tasks.add(task)  # keep a reference so it is not garbage-collected mid-flight
    task.add_done_callback(push_tasks.discard)


def send_one(sub: dict, payload: str) -> bool:
    """Send one push. Returns False when the subscription is gone and should be dropped."""
    try:
        webpush(
            sub, data=payload, vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT},
            ttl=60, headers={"Urgency": "high"},  # a stale buzz is worse than none
        )
    except WebPushException as e:
        if e.response is not None and e.response.status_code in (404, 410):
            return False
        log.warning("push failed: %s", e)
    except Exception:
        log.exception("push failed")
    return True


async def push_all(event: dict):
    """Notify every subscribed device, including ones whose page is closed."""
    if not (VAPID_PRIVATE_KEY and subscriptions):
        return
    payload = json.dumps({
        "id": event["id"],
        "answer": str(event.get("answer", ""))[:200],
        "vibrations": event["vibrations"],
    })
    subs = list(subscriptions.values())
    alive = await asyncio.gather(*(asyncio.to_thread(send_one, sub, payload) for sub in subs))
    gone = [sub["endpoint"] for sub, ok in zip(subs, alive) if not ok]
    if gone:
        for endpoint in gone:
            subscriptions.pop(endpoint, None)
        save_subscriptions()


async def event_stream(q: asyncio.Queue):
    try:
        while True:
            try:
                result = await asyncio.wait_for(q.get(), KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # keeps proxies from closing an idle connection
                continue
            yield f"data: {json.dumps(result)}\n\n"
    finally:
        subscribers.discard(q)


@app.post("/ask")
async def ask(image: UploadFile, question: str = Form(""), x_token: str | None = Header(None)):
    check_token(x_token)
    return await answer_image(await image.read(), question)


@app.post("/ask/base64")
async def ask_base64(request: Request, x_token: str | None = Header(None)):
    check_token(x_token)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Request body is not valid JSON; send Content-Type: application/json with a body like {\"image_base64\": \"...\"}")
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object like {\"image_base64\": \"...\"}")
    if "image_base64" not in body:
        raise HTTPException(400, "Missing required field: image_base64")
    try:
        req = Base64Request(**body)
    except ValidationError as e:
        field = e.errors()[0]["loc"][0]
        raise HTTPException(400, f"Field {field} must be a string")
    b64 = req.image_base64.strip()
    if b64.startswith("data:"):
        b64 = b64.partition(",")[2]
    b64 = re.sub(r"\s+", "", b64).replace("-", "+").replace("_", "/")  # tolerate line breaks, URL-safe
    if not b64:
        raise HTTPException(400, "image_base64 is empty")
    b64 += "=" * (-len(b64) % 4)
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError:
        raise HTTPException(400, "image_base64 is not valid base64 (it contains invalid characters or is truncated)")
    return await answer_image(data, req.question)


@app.get("/events")
async def events(token: str | None = None):
    # EventSource cannot send headers, so the token travels as a query parameter here.
    check_token(token)
    q: asyncio.Queue = asyncio.Queue()
    if latest:
        q.put_nowait({**latest["event"], "replay": True})  # new pages see the last answer without vibrating
    subscribers.add(q)
    return StreamingResponse(
        event_stream(q),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def is_str(v) -> bool:
    return isinstance(v, str) and bool(v)


@app.get("/push/key")
async def push_key(token: str | None = None):
    check_token(token)
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        raise HTTPException(404, "Web Push is not configured")
    return {"key": VAPID_PUBLIC_KEY}


@app.post("/push/subscribe")
async def push_subscribe(request: Request, x_token: str | None = Header(None)):
    check_token(x_token)
    if not VAPID_PRIVATE_KEY:
        raise HTTPException(404, "Web Push is not configured")
    try:
        body = await request.json()
    except ValueError:
        body = None
    keys = body.get("keys") if isinstance(body, dict) else None
    if not (isinstance(keys, dict) and is_str(body.get("endpoint")) and is_str(keys.get("p256dh")) and is_str(keys.get("auth"))):
        raise HTTPException(400, "Body must be a PushSubscription: {endpoint, keys: {p256dh, auth}}")
    if not body["endpoint"].startswith("https://"):
        raise HTTPException(400, "Push endpoint must be https")
    if body["endpoint"] not in subscriptions and len(subscriptions) >= MAX_SUBSCRIPTIONS:
        raise HTTPException(429, "Too many subscribed devices")
    subscriptions[body["endpoint"]] = {
        "endpoint": body["endpoint"], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
    }
    save_subscriptions()
    return {"ok": True, "devices": len(subscriptions)}


@app.get("/latest-image")
async def latest_image(token: str | None = None):
    # <img> cannot send headers, so the token travels as a query parameter here too.
    check_token(token)
    if not latest:
        raise HTTPException(404, "No image yet")
    return Response(latest["image"], media_type=latest["media_type"], headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/sw.js")
async def service_worker():
    # Served from the root so its scope covers the whole site; never cached so updates ship on deploy.
    return FileResponse("static/sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
