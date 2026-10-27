# Nashik Street Vendor Management & Zoning Platform

React, FastAPI, and SQLite decision-support application with separate authenticated vendor and NMC officer workspaces. The research recommendation engine remains weighted MCDA with the existing K-Means environmental features.

## Authentication configuration

Copy `.env.example` to `.env` in the repository root and configure its values. Do not commit the resulting secrets. The backend resolves this file from `backend/core/config.py` (with the original module retained as a compatibility wrapper), so both Uvicorn and direct scripts load the same configuration regardless of the current working directory. Values already defined in the process environment take precedence over `.env`.

```text
APP_ENV=development
AUTH_HMAC_SECRET=<long random secret>
SESSION_COOKIE_SECURE=false
OTP_PROVIDER=development
OTP_DEBUG=true
NMC_ADMIN_USERNAME=<officer username>
NMC_ADMIN_PASSWORD=<strong local password>
```

`AUTH_HMAC_SECRET` must remain stable after historical identities are seeded. Use `SESSION_COOKIE_SECURE=true` behind production HTTPS. `OTP_DEBUG=true` is strictly for local development: it prints the OTP code, but never the phone number. The API never returns an OTP.

## Initialize operational and authentication data

From the repository root, create a virtual environment and install the Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
```

Then, after configuring the environment, initialize the local database:

```powershell
python backend/init_db.py
```

Initialization is idempotent. It preserves operational vendors, allocation requests, allocations, zone state, and audit history. When the privacy-restricted `data/raw/nmc/vendors.csv` is supplied separately, initialization normalizes valid `phone_number` and `alternate_phone` values and seeds only unambiguous HMAC mappings. Treat this CSV as privacy-restricted: do not newly commit private vendor sources or archives. Existing upstream tracking/history is unchanged. Plaintext phone numbers are not copied into SQLite or model files.

FastAPI runs the same ordered bootstrap at application startup as a safety check. Running `init_db.py` explicitly remains useful for a visible initialization summary.

## Run locally

Backend:

```powershell
python -m uvicorn api:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Frontend:

```powershell
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Unauthenticated visitors enter through `/login`; server sessions determine whether the application shows `/vendor` or `/admin` experiences.

### Access from a phone or another computer on the same network

Keep FastAPI running on `127.0.0.1:8000` using the command above, then run `npm run dev` in `frontend`. Open the **Network** URL printed by Vite (for example, `http://<computer-ip>:5173`) on the other device. Use the port Vite actually prints.

The frontend listens on all network interfaces and forwards `/api` requests to FastAPI. Browser API calls and session cookies use the same origin as the website, so LAN access does not require adding each changing IP to CORS or exposing port 8000.

Leave `VITE_API_BASE` empty or unset in frontend environment files. An explicit `http://localhost:8000` or `http://127.0.0.1:8000` points to the visiting device and breaks LAN access. If FastAPI runs on a different port, set `API_PROXY_TARGET` in `frontend/.env.local` and restart Vite. `npm run preview` also supports the proxy after a production build.

If the Network URL itself cannot be reached, allow Node.js through Windows Firewall on your private network and confirm both devices are on the same network without client isolation. For Vercel/Render hosting, leave `VITE_API_BASE` empty and use the `/api` reverse proxy in `frontend/vercel.json`; see [deployment](docs/deployment.md) for hosting settings and persistence requirements.

## Install VendorMap as a PWA

The installable application and service worker are enabled in production builds. From `frontend`:

```powershell
npm run build
npm run preview
```

Keep FastAPI running. Preview forwards `/api` to the local backend. Desktop installation can be checked at the printed `http://localhost:4173` or loopback URL. Real phones require a trusted **HTTPS** origin; a plain HTTP LAN IP does not enable the service worker or in-app install promotion. Serve the complete `frontend/dist` directory behind HTTPS with the existing `/api` proxy, or an explicitly configured API origin. Do not expose backend credentials in Vite variables.

Supported browsers receive a dismissible Install action after their native installability event. On iPhone/iPad, the app provides Share → Add to Home Screen guidance; it is hidden when already running standalone. No background push service or new notification permissions were added: existing in-app notifications remain unchanged.

The worker caches only an explicit build-generated allowlist of fingerprinted JS, CSS, logo assets and a fingerprinted generic offline page. API requests bypass the worker and use `cache: no-store` in the client; the server also marks API responses private and non-cacheable. Authenticated HTML, profiles, photos, allocations, tokens, map tiles and navigation URLs are never stored in Cache Storage. Losing connectivity removes the in-memory workspace; reconnecting revalidates the session and reloads current data. Offline writes are not queued or replayed.

Updates wait for a Reload action or for older app windows to close. Old immutable chunks remain available to older open windows; once a sole current-build window confirms readiness, obsolete VendorMap caches are removed. Session cookies and language preferences are preserved. No forced reload interrupts an open form.

For hosting, serve `/sw.js`, `/manifest.webmanifest` and the HTML entry with revalidation (`Cache-Control: no-cache`). Fingerprinted `/assets/*` files may be served as immutable. Keep the `/sw.js` path serving JavaScript rather than the SPA fallback, and preserve SPA routing for `/vendor/*` and `/admin/*`. Changing the logo's install icons is reproducible with `npm run pwa:icons` (uses installed Edge for SVG-to-PNG rendering; no additional UI dependencies).

`npm test` includes worker cache/privacy/update-policy tests. `npm run test:pwa:browser` requires a production build and installed Edge, uses a disposable backend database, and checks Chromium installability, registration, offline navigation/privacy, authentication, update activation and session preservation. Native Android/desktop install dialogs, iPhone Home Screen installation, OS icon cropping and installed-app lifecycle still need physical-device verification over HTTPS.

## Direct Meta WhatsApp Cloud API

The vendor bot uses Meta's WhatsApp Cloud API directly. It does not use Twilio, browser automation, WhatsApp Web, or an unofficial WhatsApp client. Add these values to the project-root `.env` file:

```text
WHATSAPP_ACCESS_TOKEN=<Meta system-user or temporary access token>
WHATSAPP_PHONE_NUMBER_ID=<WhatsApp phone number ID>
WHATSAPP_VERIFY_TOKEN=<private webhook verification token you choose>
META_APP_SECRET=<Meta application secret>
WHATSAPP_GRAPH_VERSION=<supported version such as vNN.N>
```

Never commit real values. The webhook endpoints are:

```text
GET  /api/whatsapp/webhook  # Meta subscription verification
POST /api/whatsapp/webhook  # signed WhatsApp notifications
```

For local deterministic testing without Meta delivery:

```powershell
python backend/whatsapp/simulator.py --mobile 9198XXXXXXXX --message "Hi"
```

The simulator invokes the same persistent conversation service as the webhook and never calls Meta. For duplicate-delivery testing, repeat a command with the same `--message-id`.

Manual Meta Developer Console setup is still required:

1. Create or select a Meta business application and add the WhatsApp product.
2. Add and verify a WhatsApp Business phone number; copy its Phone Number ID.
3. Create an appropriately scoped access token and store it only in `.env` or a deployment secret manager.
4. Expose the backend through public HTTPS and set the callback URL to `https://<host>/api/whatsapp/webhook`.
5. Enter the same private verify token configured in `WHATSAPP_VERIFY_TOKEN` and subscribe the app to message webhook events.
6. Copy the Meta App Secret into deployment secrets for `X-Hub-Signature-256` validation.

Conversation state and inbound-message deduplication are stored in SQLite. Sender numbers are normalized and HMACed using the existing authentication secret; plaintext mobile numbers are not stored in the WhatsApp tables. Historical-vendor recognition requires the privacy-restricted identity seed data to have been initialized securely.

## Roles and privacy

- `VENDOR`: accesses only the authenticated vendor profile, recommendations, and that vendor's requests.
- `NEW_VENDOR_ONBOARDING`: can complete one verified operational profile registration.
- `ADMIN`: accesses the NMC dashboard and administrative actions.

Sessions use an HttpOnly, SameSite=Lax cookie. SQLite stores only hashes of session tokens and OTPs. Historical and newly verified phone identities use HMAC values. Admin credentials come from environment configuration and are never stored in the database. Frontend visibility is not trusted for authorization; protected API endpoints enforce roles and ownership server-side.

## Tests and build

```powershell
python -m pytest -q
cd frontend
npm test
npm run build
```

Tests use isolated temporary databases and an explicit test-only authorization bypass for legacy workflow coverage. Dedicated authentication tests run with that bypass disabled.

## Model integrity

Authentication does not modify K-Means, MCDA weights, personalization multipliers, live-capacity rules, or allocation business logic. Historical vendor associations are evidence only and are never treated as current occupancy. Map points remain analytical references, not legal zone boundaries.

## Code organization and maintenance

See [architecture](docs/architecture.md) for the final folder tree, module relationships, compatibility entry points and protected assets; [migration checklist](docs/migration-checklist.md) for phases and reversibility; and [deployment](docs/deployment.md) for dependency groups, configuration and PowerShell commands. [Removal evidence](docs/deletion-manifest.json) and [frontend moves](docs/frontend-moves.json) document source cleanup.

The operational engine remains `backend.mcda_engine`; the different historical baseline is `research.mcda_baseline`, with `scripts/mcda_engine.py` retained as a compatibility entry. No database migration or model regeneration is required by this refactor.
