import base64
import json
import os

import anthropic
from fastapi import FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


@app.post("/ask")
async def ask(image: UploadFile, question: str = Form(""), x_token: str | None = Header(None)):
    if APP_TOKEN and x_token != APP_TOKEN:
        raise HTTPException(401, "Bad token")
    if image.content_type not in ALLOWED:
        raise HTTPException(415, "Unsupported image type")
    data = await image.read()
    if len(data) > 5_000_000:
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
                    "media_type": image.content_type,
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


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
