# HELEN

Self-hosted reminder/check-off app for recurring tasks (medication, routines).
Two web UIs, Google Tasks two-way sync, and NFC trigger links.

- **Port 8001 — Settings UI**: Google OAuth setup, task definitions, trigger management. No checkboxes here.
- **Port 8002 — GUI**: today's task list with live checkboxes + the `/t/{slug}` trigger endpoint (with animation).
- **Three completion paths** (all bidirectional, except trigger which is completion-only):
  1. GUI checkbox on port 8002
  2. NFC trigger link `/t/{slug}` — picks the task whose scheduled time is closest to "now"
  3. Google Tasks app/website directly (polled every 30 s)

## Run from GHCR (recommended)

```bash
git clone https://github.com/l8tenever/helen.git
cd helen
# edit docker-compose.yml: replace l8tenever with your GitHub username (lowercase)
docker compose up -d
```

Then open:
- Settings → http://localhost:8001
- GUI      → http://localhost:8002

In the Settings UI, paste your Google OAuth client_id/client_secret (from Google Cloud Console, with the Tasks API enabled and redirect URI `http://localhost:8001/oauth/callback`), connect Google, and you're set.

## Build locally instead of pulling

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## CI

`.github/workflows/docker-build.yml` builds a multi-arch image (linux/amd64, linux/arm64) on every push to `main` and on version tags, then pushes to `ghcr.io/l8tenever/helen` with tags `latest`, `sha-<short>`, and `vX.Y.Z` when applicable.
