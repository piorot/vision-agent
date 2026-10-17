import base64
import json
import os

import anthropic
from fastapi import FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()
client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY

MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
APP_TOKEN = os.getenv("APP_TOKEN")  # optional shared secret for /ask
ALLOWED = {"image/jpeg", "image/png", "image/gif", "image/webp"}

SYSTEM = (
    "You answer the question shown in the image. Reply with ONLY a JSON object: "
    '{"answer": "<short answer>", "vibrations": <integer 0-10>}. '
    "If the answer is a number from 0 to 10, vibrations equals that number. "
    "Otherwise: yes=2, no=1, multiple-choice A=1 B=2 C=3 D=4, anything else=0."
)


MAX_BYTES = 5_000_000


class Base64Request(BaseModel):
    image_base64: str
    media_type: str | None = None  # inferred from a data: URL prefix if omitted
    question: str = ""


def check_token(x_token: str | None):
    if APP_TOKEN and x_token != APP_TOKEN:
        raise HTTPException(401, "Bad token")


async def answer_image(data: bytes, media_type: str, question: str):
    if media_type not in ALLOWED:
        raise HTTPException(415, "Unsupported image type")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Image too large (max 5MB)")

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
    text = msg.content[0].text.strip()
    try:
        result = json.loads(text[text.index("{"): text.rindex("}") + 1])
        result["vibrations"] = max(0, min(10, int(result.get("vibrations", 0))))
    except (ValueError, TypeError):
        result = {"answer": text, "vibrations": 0}
    return result


@app.post("/ask")
async def ask(image: UploadFile, question: str = Form(""), x_token: str | None = Header(None)):
    check_token(x_token)
    return await answer_image(await image.read(), image.content_type, question)


@app.post("/ask/base64")
async def ask_base64(req: Base64Request, x_token: str | None = Header(None)):
    check_token(x_token)
    b64, media_type = req.image_base64.strip(), req.media_type
    if b64.startswith("data:"):
        header, _, b64 = b64.partition(",")
        media_type = media_type or header[5:].split(";")[0]
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError:
        raise HTTPException(400, "Invalid base64")
    return await answer_image(data, media_type or "", req.question)


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
