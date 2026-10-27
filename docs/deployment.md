# Setup and deployment

## Windows development

Run from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
```

For a new setup only, copy `.env.example` to `.env` and supply real local
configuration. Preserve an existing `.env`. Process environment values take
precedence. Configuration lives in `backend/core/config.py`; compatibility
`backend/app_config.py` remains available. Never expose secrets through Vite
variables or commit `.env`.

Database initialization writes seed/bootstrap state and should be run only when
setting up the application, with backups appropriate to an existing database:

```powershell
python backend/init_db.py
```

The unchanged backend command is:

```powershell
python -m uvicorn api:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

In another PowerShell terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Use the same hostname for browser and backend
cookies. Login and role-aware `/admin` and `/vendor` navigation remain unchanged.

## Dependencies and verification

`backend/requirements.txt` is the complete installation entry point.
It includes app, research and development requirements with constraints captured
from the verified Windows Python 3.13 environment. To install only app packages:

```powershell
python -m pip install -c backend/requirements/constraints.txt -r backend/requirements/app.txt
```

Research packages include scikit-learn, joblib, GeoPandas, matplotlib and
Streamlit. XGBoost remains optional. SciPy and PyProj are now direct declarations.
The constraints snapshot is not proof of a fresh installation on every OS;
validate a clean environment on the deployment target. `npm ci` uses the existing
lockfile. No new frontend dependencies were introduced.

```powershell
python -m pytest -q
python -m pip check
cd frontend
npm test
node --test tests/network-access.test.mjs tests/deployment.test.mjs
npm run build
node tests/pwa-browser.mjs
```

The browser smoke runner requires installed Edge (or `EDGE_PATH`) and uses
browser-only API/GPS fixtures. Tests use isolated databases; do not run production
startup or research regeneration as a harmless verification command.

## Deployment requirements

The deployed frontend is `https://vendormap-eta.vercel.app`; the backend is
`https://final-yr-project-ik72.onrender.com`. Authentication uses a host-only
HttpOnly `nmc_session` cookie, `SameSite=Lax`, with a twelve-hour server expiry.
Both roles validate through `/api/auth/me`. Login JSON contains the identity,
not a bearer token. Session tokens are random and their SHA-256 hashes live in
the operational database; `AUTH_HMAC_SECRET` protects phone identities and OTPs.

On Vercel set **Root Directory = frontend**, Framework = Vite, Build Command =
`npm run build`, Output Directory = `dist`. The committed `frontend/vercel.json`
proxies `/api/*` to Render before applying the SPA fallback. Leave
`VITE_API_BASE` empty or remove it from **all relevant Vercel environments**.
An old absolute override in the dashboard takes precedence over local files;
Vercel builds now reject it. Rebuild after removing it. `API_PROXY_TARGET` is
only used by local Vite development/preview, not Vercel hosting.

This keeps browser fetches and cookies on the frontend origin, avoiding
cross-site cookie restrictions without changing SameSite or exposing tokens to
JavaScript. Vercel proxies external origins as documented in
[Vercel rewrites](https://vercel.com/docs/routing/rewrites). `SameSite=Lax`
excludes cross-site fetches even when `credentials: include` is set; see
[cookie behavior](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie).

API responses have `Cache-Control: private, no-store`; Vercel rewrite caching
is explicitly disabled. SPA pages, the worker and manifest revalidate, while
fingerprinted assets may be immutable. Missing static assets are not rewritten
to HTML. API requests bypass the worker; the shared client enforces no-store.

On Render use the **repository root** as Root Directory so `data/`, `models/`
and the backend package remain available. Build Command:

```sh
pip install -r backend/requirements/app.txt
```

Start Command (Render's Linux shell expands `$PORT`):

```sh
python -m uvicorn backend.api:app --host 0.0.0.0 --port $PORT --workers 1
```

Do not use `--reload` in production. `/api/health` is the liveness path. It does
not prove OTP delivery, model readiness, database contents or persisted GIS
layouts. Repository-relative paths are resolved from modules, independently of
the working directory. A clean Linux dependency installation and deployment
remain to be verified on Render; a Windows build is not proof of that.

Required Render environment variable names:

| Name | Production setting |
| --- | --- |
| `APP_ENV` | `production` |
| `DATABASE_URL` | Existing PostgreSQL URL, or existing absolute SQLite file URL on a persistent disk |
| `AUTH_HMAC_SECRET` | Preserve the existing stable secret; never generate one per startup |
| `SESSION_COOKIE_SECURE` | `true` |
| `OTP_PROVIDER` | Requires a real implemented provider for production vendor login; see blocker below |
| `OTP_DEBUG` | `false` |
| `NMC_ADMIN_USERNAME` | Existing officer username from deployment secrets |
| `NMC_ADMIN_PASSWORD` | Existing strong officer password from deployment secrets |
| `CORS_ORIGINS` | `https://vendormap-eta.vercel.app`, plus explicitly approved HTTPS custom/preview origins if needed |

`PORT` is supplied by Render. Keep `ALLOW_DESTRUCTIVE_DB_RESET=false` (also the
default). No secrets belong in Vercel or `VITE_*` variables. The only frontend
configuration name relevant here is `VITE_API_BASE`, which must be unset/empty.
CORS accepts exact origins and credentials; wildcards are rejected. Browser
writes also validate the origin, retaining same-origin Vite/LAN proxy support
using the browser-controlled `Sec-Fetch-Site` header. Native signed WhatsApp
webhooks without Origin continue through their existing signature validation.

Never point `DATABASE_URL` at a new empty database to fix authentication. Keep
the intended existing production target. PostgreSQL URL schemes `postgres://`,
`postgresql://` and `postgresql+psycopg://` use the existing psycopg dependency.
The production bootstrap rejects an implicit local SQLite fallback, in-memory
SQLite, a relative SQLite path, or a nonexistent SQLite file before writing.
For SQLite, confirm the file actually resides on a Render persistent disk;
an explicit URL alone does not prove that a disk is mounted. Render's default
filesystem is ephemeral; see [persistent disks](https://render.com/docs/disks).

Make a consistent backup first (SQLite backup API or stopped application),
preserve journals when relevant, and test restoration. Startup performs the
existing additive schema initialization and inserts missing official zones;
it does not reset records. A new installation must deliberately provision its
database first. Migration/reconciliation scripts are not part of the deploy
command and have not been run against production by this fix.

Preserve all GIS/model files referenced by `data/catalog.json`, including the
active `manual-142-v2` artifacts and baseline reference files. Current location
resolution also needs the saved geographic `pilot_slot_layouts` and
`pilot_slots` for `NN_F_013`, `PAN_F_019`, `SAT_F_014`, and `NR_F_003` in the
operational database. Restore the intended saved data if absent; do not
invent geometry or regenerate research models. Missing reference data now
returns a safe 503 from zone APIs while leaving the session intact.

Database/volume permissions must allow operational writes. Public datasets
and models need read access. The private historical vendor CSV is optional
for availability, but historical vendor login requires the existing HMAC
identities to have been securely seeded. Never publish private source CSVs.

Production also checks the configured database **before** schema creation: the
existing `operational_vendors`, `zone_live_status`, `allocations` and
`allocation_requests` tables and populated zone records must be present. An
empty PostgreSQL schema or empty SQLite file is rejected without seeding it.
Connectivity errors fail startup without falling back to another database.
Do not point at an empty database to repair a connection error; deliberately
provision or restore the intended database outside normal deployment first.
`ALLOW_DESTRUCTIVE_DB_RESET=true` is rejected in production.

**Vendor login release blocker:** the only implemented OTP provider is
`development`; it performs no real delivery. Production now returns 503 for
that provider instead of reporting a successful challenge that nobody can
receive. There is no magic production `OTP_PROVIDER` value to set. A real
provider and its credentials must be selected and implemented separately.
Do not set `APP_ENV=development` or expose OTPs to bypass this. Tests mock only
delivery and use synthetic phone data; they do not establish real SMS delivery.

Optional feature variables remain server-side: routing uses `ORS_API_KEY` and
`ORS_BASE_URL`; Gemini uses `GEMINI_EXPLANATIONS_ENABLED`, `GEMINI_API_KEY`,
`GEMINI_MODEL`, `GEMINI_TIMEOUT_SECONDS`, `GEMINI_EXPLANATION_CACHE_SIZE`,
`GEMINI_EXPLANATION_RATE_LIMIT`; WhatsApp uses `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `META_APP_SECRET`,
`WHATSAPP_GRAPH_VERSION`. Their credentials are needed only when enabling the
corresponding integration. Browser location requires HTTPS or localhost
and permission. Missing/unavailable routing must retain map-only guidance;
fixtures do not verify live routing. Restrict access to private source vendor
data and operational storage. `/api/health` retains its existing response and
is a liveness endpoint, not a comprehensive dependency readiness probe.

Redeploy **both** services after verifying the production environment and
backups. Existing sessions remain valid if the operational DB is preserved;
users whose browsers never accepted the old Render cookie must log in again
through Vercel. Let the existing PWA update prompt activate the new worker.
Check `/api/health` and refresh `/admin` and `/vendor/profile` on Vercel, then
verify login's `/api/auth/me` response before protected requests. Review
`docs/final-production-readiness.md` for the latest cleanup, executed tests,
remaining snapshot failures and live-verification limits. The earlier
`docs/production-deployment-audit.md` preserves the initial integration findings.
