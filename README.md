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

## Vibrate with the page closed (Web Push)
Browsers only allow `navigator.vibrate` after a tap on an open page. To buzz a phone for every new image with no page open, the server sends a Web Push message and the service worker (`static/sw.js`) shows a notification carrying the vibration pattern (Android Chrome). Each device taps **Enable vibration alerts** once and allows notifications; each answer then shows a notification and buzzes N times. iPhones cannot vibrate from the web at all.

One-time setup:

    python scripts/gen_vapid.py            # prints the `fly secrets set ...` line; run it
    fly volumes create vision_data --size 1 --region ams
    fly deploy

Device subscriptions are saved to `$DATA_DIR/push_subscriptions.json` (the `/data` volume on Fly), so they survive deploys. Keep the VAPID keys stable: new keys invalidate every subscription (pages re-subscribe when opened). Without the keys, push is off and the page falls back to vibrating while it is open. Locally, `pip install -r requirements.txt` needs `pip install "setuptools<80" wheel && pip install --no-build-isolation http-ece` first (see the Dockerfile).

## Several questions in one image
The model returns one answer per question. `/ask` responds with `answers: [{answer, vibrations}, ...]` (`answer` joins them with ` | `; `vibrations` is the first answer's count, for single-question callers). Each answer is buzzed in turn with a pause of `VIBRATION_PAUSE_SECONDS` (default 10) after the previous buzz ends. With Web Push this is one notification per answer sent by the server on that schedule, because Android caps the length of a single vibration pattern; an open page without push does the same with timers. A newer image cancels the remaining buzzes of the older one.

## Keeping the screen on
The page asks the browser (Screen Wake Lock API) to keep the screen on while it is open, and takes the lock again whenever the tab becomes visible. It cannot override the power button, battery saver, or a different app in front, so keep the tab in the foreground.

## Automatic deploys
Every push to `main` runs the tests and then `fly deploy` (`.github/workflows/deploy.yml`). One-time setup: create a deploy token and add it as the repository secret `FLY_API_TOKEN`:

    fly tokens create deploy -a vision-agent

(GitHub repo → Settings → Secrets and variables → Actions → New repository secret.) Fly secrets (API key, VAPID keys) and the `vision_data` volume stay as set up before; the workflow only ships the code.
