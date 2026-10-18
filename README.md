# Vision Vibe

FastAPI app: POST an image to `/ask`, Claude answers, every open web page vibrates N times (Android only).

## Run locally
    pip install -r requirements.txt
    ANTHROPIC_API_KEY=sk-... uvicorn main:app --reload
    curl -F image=@question.png localhost:8000/ask

## Deploy to Fly.io
    curl -L https://fly.io/install.sh | sh      # install flyctl
    fly auth login
    # edit app name in fly.toml, then:
    fly launch --no-deploy --copy-config
    fly secrets set ANTHROPIC_API_KEY=sk-... APP_TOKEN=pick-a-long-random-string
    fly deploy
    fly open

Remote call: `curl -H "X-Token: <APP_TOKEN>" -F image=@q.png https://<app>.fly.dev/ask`

## Base64 input
    curl -X POST https://<app>.fly.dev/ask/base64 \
      -H "X-Token: <APP_TOKEN>" -H "Content-Type: application/json" \
      -d '{"image_base64": "<base64>", "media_type": "image/png", "question": "optional"}'

`image_base64` may also be a `data:image/...;base64,...` URL. Line breaks and URL-safe base64 are tolerated. `media_type` is optional and ignored: the real type (JPEG, PNG, GIF, WebP) is detected from the bytes. HEIC is not supported; convert to JPEG first.

## Live broadcast
Open pages subscribe to `GET /events` (Server-Sent Events, `?token=<APP_TOKEN>` if set). Every answer is pushed to all of them. Subscribers are held in memory, so run a single machine (`fly scale count 1`). iOS has no `navigator.vibrate`; on Android the page must have been tapped once and be in the foreground.

The newest image and answer are kept in memory only (nothing is stored on disk). Each page shows them large, and a page that opens later sees the last answer without vibrating. The image is served from `GET /latest-image` (`?token=` if `APP_TOKEN` is set); it is lost on restart.
