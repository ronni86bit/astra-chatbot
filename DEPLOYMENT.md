# Deployment Guide — RF/SystemVue Question Assistant

Deployment-ready application code. Production use still requires office
infrastructure decisions (hosting, domain, secret management, and
confirmation of the open items in `OFFICE_REVIEW.md`).

---

## 1. Prerequisites

- Python ≥ 3.10 with `pip`
- Node 18+ with npm (only needed to build the frontend)
- Docker + Docker Compose (only for the containerized deployment)
- A Groq API key (`GROQ_API_KEY`) — or another supported provider
  (`LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY`, `LLM_PROVIDER=openai`
  + `OPENAI_API_KEY`)

## 2. Environment variables (backend `.env` in the project root)

Copy `.env.example` → `.env` and fill in:

| variable | purpose |
|---|---|
| `GROQ_API_KEY` | LLM provider key (per-provider variable, never cross-used) |
| `LLM_PROVIDER` | `groq` (dev default) / `openrouter` / `openai` |
| `LLM_MODEL` | optional model override (default: `openai/gpt-oss-120b` on Groq) |
| `LLM_BASE_URL` | optional OpenAI-compatible endpoint override |
| `CORS_ORIGINS` | comma-separated browser origins allowed by the API (dev defaults cover the Vite server; leave empty behind nginx) |
| `API_HOST` / `API_PORT` | backend bind address (default `127.0.0.1:8000`) |
| `SESSION_TTL` | idle seconds before a web conversation is dropped (default 7200) |

Real environment variables always win over `.env`. Keys are never logged,
never returned by the API, and never baked into images. The frontend has
its own optional `VITE_API_BASE_URL` (see `frontend/.env.example`) — the
browser only ever talks to our backend, never to the LLM provider.

## 3. Dataset placement

The audited workbook `SystemVue_RF_Unique_Question_Assistant_V63.xlsx`
must be in the project root (read-only). The Docker backend image bakes
it in at build time; if the office considers the workbook sensitive, do
not publish the image or the repository (see §8).

## 4. Option A — single process (simplest)

```bash
pip install -e .[llm,api]
cd frontend && npm install && npm run build && cd ..
python -m rf_catalogue.api          # serves UI + API on 127.0.0.1:8000
```

Open `http://127.0.0.1:8000`. The API serves `frontend/dist` directly,
so one process runs the whole app. Put your own reverse proxy in front
for TLS in production.

## 5. Option B — development mode (hot reload)

```bash
python -m rf_catalogue.api          # backend on :8000
cd frontend && npm run dev          # Vite on :5173, proxies /api
```

Open `http://localhost:5173`.

## 6. Option C — Docker Compose (production topology)

```bash
docker compose up --build           # frontend on http://localhost:8080
```

- `backend`: installs the package (`.[llm,api]`), bakes the workbook in,
  runs `uvicorn rf_catalogue.api:create_app_factory --factory`. Secrets
  are injected at runtime from `.env` via compose `environment`.
- `frontend`: multi-stage build (node → nginx). nginx serves the static
  app, reverse-proxies `/api` to the backend (same origin → no CORS in
  production), sets basic hardening headers, and caches hashed assets.
- Health check: backend container is probed via `GET /api/health`
  (alias `GET /health`); the web frontend starts only once it is healthy.

Rollback: `docker compose down` then re-deploy the previous image tag /
commit; the application is stateless (in-memory sessions only), so a
rollback loses nothing but live conversations.

## 7. Production backend start command (bare metal)

```
uvicorn rf_catalogue.api:create_app_factory --factory \
    --host 0.0.0.0 --port 8000 --workers 1
```

Keep one worker: conversation sessions are in-process. Scaling to
multiple replicas requires moving session state to shared storage —
not needed for the current single-team deployment.

## 8. Security notes / blockers

- **Public repository blocker:** this repository is currently PUBLIC and
  contains the office workbook (`SystemVue_RF_Unique_Question_Assistant_V63.xlsx`)
  and evaluation artifacts. If the office has not approved publication,
  make the repository private or remove the workbook from history before
  treating this deployment as approved.
- Never put the provider key in frontend code or images; it is read from
  the backend environment only.
- nginx adds `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`. Add TLS termination at your edge.
- Sessions are opaque random IDs; no user data is stored beyond the
  conversation intent state.
