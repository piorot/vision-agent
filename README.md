# Vision Vibe

FastAPI app: POST an image to `/ask`, Claude answers, the web page vibrates N times (Android only).

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
