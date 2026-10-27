# VendorMap production integration audit — 2026-10-27

This records the initial integration pass. See
[final production readiness](final-production-readiness.md) for the subsequent
backend cleanup, database safeguards and updated test results.

## Root cause and live evidence

The deployed frontend bundle (`/assets/index-DYfcqPO6.js` at inspection time)
contains the absolute Render API origin. Backend login returns role/identity
JSON and sets `nmc_session`, an HttpOnly, host-only, `SameSite=Lax` cookie.
Protected handlers read that cookie and look up its hash in `auth_sessions`.
They do not authenticate a bearer token. The client already uses
`credentials: include`. Cross-site Vercel-to-Render fetches cannot send a Lax
cookie, explaining successful login followed by 401s. A cookie may also be
rejected on the cross-site login response. Both admin and vendor correctly use
the same role-aware `/api/auth/me`; separate role validation endpoints are not
missing. See [MDN's SameSite rules](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie).

Read-only checks against the existing deployment returned:

| Request | Observed result before redeployment |
| --- | --- |
| Vercel `/` | 200, built HTML |
| Vercel `/api/health` | 404; API proxy absent |
| Vercel `/admin` | 404; deep-route fallback absent |
| Render `/api/health` | 200 after an initial 25-second cold-start timeout |
| Render `/api/auth/me`, no cookie | 401, as required |
| Render response to Vercel Origin | Exact `Access-Control-Allow-Origin` and credentials allowed |
| Render API cache headers | No explicit Cache-Control header |

No production credentials were submitted and no production records were
written. The observed CORS policy is correct; CORS does not override SameSite.
The live authenticated cookie rejection itself was not captured using a real
production account. The diagnosis combines the deployed bundle, actual cookie
code, browser rules, and the reported login/network sequence.

## Implemented changes

| Files | Fix |
| --- | --- |
| `frontend/vercel.json`, `frontend/vite.config.js` | First-party `/api` rewrite to Render, SPA refresh fallback, static exclusions, cache headers; reject absolute Vercel API overrides at build time |
| `frontend/src/services/api.js` | Confirm cookie round trip with `/api/auth/me` after either login; share concurrent identity checks only; distinguish session expiry, permission errors, startup errors and network failure |
| `frontend/src/app/ImplementationApp.jsx` | Clear private workspaces on session expiry; preserve valid sessions/routes during startup errors and offer retry; ignore stale initialization results |
| `frontend/src/pwa/sw.js`, `frontend/src/pwa/sw.test.js` | Bypass API interception entirely; preserve immutable static allowlist, generic offline page, updates and session cookies |
| `backend/application.py` | Private/no-store API responses, including auth and authorization errors; reject untrusted browser writes while preserving proxy/LAN use |
| `backend/core/config.py`, `backend/bootstrap.py` | Require secure production cookies, disabled OTP debug, explicit persistent DB target; reject implicit, in-memory, relative or nonexistent production SQLite before bootstrap writes |
| `backend/repositories/database.py` | Accept `postgres://` aliases using the existing psycopg driver |
| `backend/services/authentication.py` | Reject the nondelivering development OTP stub in production before recording a misleading challenge |
| `backend/services/zone_references.py`, `backend/routers/vendors.py`, `backend/routers/zones.py` | Translate unavailable saved GIS references into a safe 503 without inventing replacement geometry or changing GIS logic |
| `tests/test_production_integration.py` | Real authorization on HTTPS test origins, synthetic vendor onboarding, recommendations, approval/rejection and assigned slot, refresh, expiry, logout, CSRF and configuration checks |
| `frontend/src/services/api.test.js`, `frontend/tests/deployment.test.mjs`, `frontend/tests/pwa-browser.mjs` | Cookie verification, status handling, proxy configuration, PWA privacy/update behavior and officer form/refresh/logout coverage |
| `.env.example`, `frontend/.env.example`, `README.md`, `docs/deployment.md`, this report | Remove conflicting example defaults; correct dependency paths; give deployment settings, environment names and persistence requirements |

No database was reset or migrated. No private historical data was newly
tracked. K-Means, MCDA, GIS resolution and recommendation implementations were
unchanged; relevant files and existing snapshot artifacts were compared with
HEAD. No dependencies or product features were added.

## Verification

All writes used disposable databases. Production-like tests use HTTPS,
`Secure; HttpOnly; SameSite=Lax` cookies and authentication with the bypass
disabled. Only OTP delivery is mocked; OTP verification, ownership, model
ranking, capacity, requests and allocation operations are real. Saved
geographic fixtures are copied from the local database in read-only mode,
with assignments and request references removed. They never overwrite it.

The browser test runs the production frontend build through a local
same-origin proxy against an isolated real backend. Native iPhone installation
is not established by its emulated user-agent checks.

Final results:

| Executed check | Result |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -q tests/test_authentication.py tests/test_app_config.py tests/test_production_integration.py --tb=short` | 25 passed; authentication bypass disabled for auth/production tests |
| `npm test` in `frontend` | 62 passed |
| `node --test tests/network-access.test.mjs tests/deployment.test.mjs` in `frontend` | 4 passed; proxy forwards cookies/Set-Cookie and Vercel rejects an absolute API override |
| `npm run build` in `frontend` | Passed; 1,690 modules transformed |
| `node tests/pwa-browser.mjs` in `frontend` | 11 check groups passed; no browser runtime exceptions; detailed results in `reports/vendormap/pwa-results.json` |
| `.venv/Scripts/python.exe -m pip check` | No broken requirements |
| `.venv/Scripts/python.exe -m pytest -q` | **186 passed, 18 failed, 6 errors** |
| Same original backend suite with seven modified runtime modules loaded from `git show HEAD`, excluding the new production tests | **180 passed, the same 18 failed and 6 errors** |
| `git diff --check` | Passed |
| Read-only catalog/file audit | All 23 catalog paths exist locally; no path-component case mismatches; the operational database is correctly untracked |

The HEAD comparison loaded original application, bootstrap, configuration,
database, vendor/zone router and authentication source in a separate process.
It did not check out files or reset data. Its initial sandbox run could not
access temporary databases; the approved rerun above completed normally.
The failure names match the current full-suite run exactly:

- Eleven allocation/admin workflow tests use business-category fixtures that
  conflict with the current strict profile checks and receive 422.
- Two admin/vendor zone tests omit the saved geographic pilot fixtures. HEAD
  raises a ValueError; the patch returns a safe 503 instead.
- Three architecture checks compare against older OpenAPI, asset and scientific
  snapshots. Those snapshots, the scientific code and the source data are
  unchanged from HEAD.
- Two tests retain outdated personalization or historical identity expectations.
- Six manual-model tests error because their fixture uses `DatabaseConnection`
  as an unsupported context manager.

These failures were not hidden, skipped in the normal suite, or resolved by
weakening business checks or changing scientific logic. Existing fixture and
snapshot maintenance remains separate work.

## Remaining deployment work and release limits

Both frontend and backend require redeployment. Use the exact hosting commands,
environment variable names and values/constraints in [deployment.md](deployment.md).
On Vercel, remove the existing absolute `VITE_API_BASE` dashboard override and
set Root Directory to `frontend`. A build-time variable change requires a fresh
frontend deployment. No frontend secrets are required.

Required Render names: `APP_ENV`, `DATABASE_URL`, `AUTH_HMAC_SECRET`,
`SESSION_COOKIE_SECURE`, `OTP_PROVIDER`, `OTP_DEBUG`, `NMC_ADMIN_USERNAME`,
`NMC_ADMIN_PASSWORD`, `CORS_ORIGINS`. Render supplies `PORT`.
`ALLOW_DESTRUCTIVE_DB_RESET` must remain false. Optional integration names are
listed in deployment.md; no secret values are included in this report.

The only OTP provider is a development stub. Production vendor login needs a
selected and implemented real provider; setting an invented environment value
does not enable delivery. Admin cookie integration is independent of this
blocker. Vendor flows passed with test-only delivery mocking.

The Render dashboard, production `DATABASE_URL`, storage mount, existing
database/schema/row counts, private HMAC identity seed, filesystem permissions
and saved pilot layouts could not be inspected through public HTTP endpoints.
The 23 current local catalog paths exist, but this does not prove they are all
present on Render. Confirm the intended database and restore required existing
saved GIS data from backups if missing. A valid SQLite path is not proof of a
persistent mount. Render's default filesystem is
[ephemeral](https://render.com/docs/disks).

No deployment or push was performed. Live authenticated workflows, the Vercel
rewrite after deployment, a clean Linux dependency installation, PostgreSQL
integration, actual SMS delivery, physical-device installation, provider
availability and restart durability still require verification with safe
accounts. The complete backend suite is not green; details follow in the test
results. No production-ready claim is made while these limits remain.

## Git commands

From the repository root on the current `main` branch:

```powershell
git add -- .env.example README.md docs/deployment.md docs/production-deployment-audit.md backend/application.py backend/bootstrap.py backend/core/config.py backend/repositories/database.py backend/routers/vendors.py backend/routers/zones.py backend/services/authentication.py backend/services/zone_references.py frontend/.env.example frontend/vercel.json frontend/vite.config.js frontend/src/app/ImplementationApp.jsx frontend/src/pwa/sw.js frontend/src/pwa/sw.test.js frontend/src/services/api.js frontend/src/services/api.test.js frontend/tests/deployment.test.mjs frontend/tests/pwa-browser.mjs reports/vendormap/pwa-results.json tests/test_production_integration.py
git diff --cached --stat
git commit -m "Fix production cookie sessions and Vercel Render integration"
git push origin main
```

These commands do not stage local `.env` files or private vendor sources. Verify
the hosting environment before allowing any automatic backend redeployment;
production now refuses unsafe or implicit database configuration.
