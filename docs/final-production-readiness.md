# VendorMap final readiness audit — 2026-10-27

## Scope and backend organization

The existing responsibility split is retained: `core/`, `routers/`, `schemas/`,
`services/`, `repositories/`, `geo/` and `whatsapp/`. A new
[backend ownership guide](../backend/README.md) explains runtime, compatibility,
numerical and research entry points. No production Python module was moved or
deleted, and no API route was removed by this work.

The reference audit scanned the tracked Python sources and backend modules,
then checked imports, router registration, legacy `sys.modules` aliases,
`importlib` use, CLI entry points, tests, Vite imports/globs, catalog paths and
documentation. The final syntax pass parsed 121 Python files; the backend has
71 Python modules. Static reachability alone was not used to justify deletion:
`from package import module`, aliases and direct CLIs need manual tracing.

Confirmed cleanup:

- Removed `frontend/src/shared/hooks/useApiData.js`: no import, dynamic import,
  test or CLI caller. The frontend's only source glob explicitly loads public
  datasets, not hooks. Evidence is recorded in `deletion-manifest.json`.
- Archived 234 lines of unused embedded pre-SQLAlchemy DDL from
  `repositories/database.py` into `docs/archive/pre-sqlalchemy-schema.sql`.
  The combined literal is preserved exactly after newline normalization;
  SHA-256: `fc3a524aab0532982f044d143be19ea456a7a775d96405bab9c78b30d44a34f9`.
  It is not executable startup code or a migration. Active `METADATA` remains
  authoritative and unchanged.
- Removed a duplicate `Path` import and corrected stale architecture and
  dependency-path documentation.

Retained deliberately: compatibility alias modules, `05_recommend.py`,
`init_db.py`, package initializers, research routes, AHP calibration, expert
label aggregation, supervised training, recommendation evaluation, category
experiments, Goal Programming, GIS modules and migration/reconciliation tools.
For example, `scripts/generate_synthetic_expert_demo.py` uses the research
modules; tests and existing CLIs use the compatibility names. The historical
MCDA baseline and operational MCDA are distinct implementations. Runtime and
research dependency groups still have callers; no dependency was removed.

## Genuine defects fixed

1. `DatabaseConnection` now commits successful context-managed work, rolls back
   failures, always closes the connection and propagates exceptions. This fixes
   the unsupported context-manager API used by six manual-model fixtures.
   Dedicated persistence/rollback tests verify the behavior.
2. Production bootstrap now inspects the configured database before additive
   initialization: operational tables and existing zone records must be present.
   An empty PostgreSQL schema or empty SQLite file cannot silently become a new
   production database. Connection errors fail safely without a fallback URL.
3. `ALLOW_DESTRUCTIVE_DB_RESET=true` is rejected in production. The existing
   database, stable `AUTH_HMAC_SECRET`, records and supported additive upgrades
   are preserved.
4. The SQLite-to-PostgreSQL migration utility now accepts Render's `postgres://`
   alias using the existing psycopg driver, consistently with the application.
   Only URL handling was tested; no migration was run.
5. Unknown capacity is consistently unavailable for zone selection, application
   submission and approval, matching the existing recommendation live filter.
   A previously hidden test exposed that the explorer allowed `NE_F_003` while
   recommendations excluded it. The shared eligibility guard now reports
   `CAPACITY_UNKNOWN`; no capacity value, score, coordinate or record is invented.
   Existing allocations remain readable; a highlighted current/pending zone can
   still be displayed with its eligibility state.

The earlier first-party authentication, PWA and deployment fixes are retained.
Both logins validate their cookie through `/api/auth/me`; protected APIs enforce
roles/ownership. Sessions use HttpOnly, Secure production cookies and database
token hashes. Vercel proxies `/api` to Render; API responses are private/no-store
and bypass service-worker interception. No bearer token or authentication bypass
was added. The approved UI is unchanged.

## Failure investigation and test integrity

The preceding pass established 18 failures and six fixture errors also present
in committed code. This pass investigated their causes rather than weakening
production checks:

| Earlier failure group | Classification and action |
| --- | --- |
| Eleven allocation/admin workflow failures | Fixtures supplied `food` for unrelated historical profiles; the API correctly returned 422. Lifecycle tests now let the server derive category. Stale prose assertions now check the actual eligibility reasons. Separate authenticated tests explicitly reject category spoofing. |
| Two admin/vendor zone failures | Their empty test DBs omitted saved GIS fixtures. Shared test support now copies geometry read-only and strips assignments and request references in the disposable copy. |
| Six manual-model setup errors | Genuine database context-manager defect fixed. Newly reachable tests also exposed the SQLAlchemy exception-wrapper expectation and unknown-capacity mismatch. |
| Personalization multiplier failure | Original v1 expectations ran against active v2 policy. The v1 test now explicitly selects the legacy policy branch; independent v2 tests retain their exact approved blending/non-compounding assertions. Scientific implementation is unchanged. |
| Historical name assertion | An assertion pinned a private person's mutable source name. It now uses synthetic identity metadata while still checking historical profile overlay and source-file immutability. |
| Three architecture snapshot failures | Retained as failures. The pre-refactor OpenAPI snapshot predates 14 added API paths, with no paths removed; asset and scientific snapshots predate current source/v2 changes. The snapshots were not rewritten, disabled or skipped. |

The model has 146 geometrically scoreable candidates, but only 145 are
operationally eligible in a fresh fixture because `NE_F_003` has unknown
capacity. Tests now distinguish those counts and explicitly assert its
exclusion. The 20 eligible unscorable locations retain their existing behavior.

All operational test writes target disposable databases. The source geometry
database is opened read-only, and production authentication tests disable the
authorization bypass. OTP delivery alone is mocked for synthetic test accounts.
Production startup runs through the real FastAPI lifespan in those tests.

## Validation results

| Executed check | Final result |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest -q --tb=short` | **214 passed, 3 failed, 0 errors**, 1 existing Starlette/httpx deprecation warning; 281.31 seconds |
| Focused manual-model, production integration, allocation and admin workflow tests | **43 passed**; genuine authorization, submission, approval/rejection, rollback and startup checks |
| `npm test` in `frontend` | **62 passed** |
| `node --test tests/network-access.test.mjs tests/deployment.test.mjs` | **4 passed** |
| `npm run build` | Passed; 1,690 modules transformed |
| `node tests/pwa-browser.mjs` | **11 browser check groups passed**, no runtime exceptions; rerun after the eligibility fix |
| `.venv/Scripts/python.exe -m pip check` | No broken requirements |
| Python AST parsing | 121 files parsed without syntax errors |
| `git diff --check` | Passed |
| Protected-file/slot fingerprints | 98 tracked data/model files and five checked scientific source files unchanged; all 85 saved slot rows, including geometry/assignment/status, unchanged |
| Active SQLAlchemy metadata | `_build_metadata` AST unchanged from HEAD |
| Secret/staging review | No configured local secret values found in the diff or built frontend; no files staged, committed or pushed; environment and operational DB paths remain ignored |

Remaining failures are exactly:

1. `test_openapi_contract_matches_pre_refactor_baseline`
2. `test_original_runtime_and_research_assets_are_unchanged`
3. `test_scientific_implementations_match_original_function_bodies`

These are the pre-existing historical snapshot mismatches described above.
They require a separately reviewed snapshot update against the approved v2
application/data, not modification of working algorithms or vendor records to
match an older snapshot. No current workflow test remains failing.

## Required hosting configuration

These settings are derived from the repository; existing dashboard values
could not be inspected. [deployment.md](deployment.md) contains full details.

| Vercel setting | Required configuration |
| --- | --- |
| Root directory | `frontend` |
| Framework preset | Vite |
| Build command | `npm run build` |
| Output directory | `dist` |
| Environment | Remove or empty `VITE_API_BASE` in each relevant environment; no server secrets |
| Proxy | Committed `frontend/vercel.json`: `/api/:path*` → `https://final-yr-project-ik72.onrender.com/api/:path*`, then SPA fallback |

`API_PROXY_TARGET` is for local Vite development/preview only. Absolute Vercel
API overrides fail the build deliberately, preventing cross-site Lax-cookie
failures. Rebuild after changing dashboard variables.

| Render setting | Required configuration |
| --- | --- |
| Root directory | Repository root; leave the setting empty |
| Build command | `pip install -r backend/requirements/app.txt` |
| Start command | `python -m uvicorn backend.api:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health path | `/api/health` (liveness only) |
| Database | Existing `DATABASE_URL`, correct populated operational schema; existing SQLite must use an absolute persistent-disk path |

Required Render names: `APP_ENV=production`, `DATABASE_URL`,
`AUTH_HMAC_SECRET` (preserve the existing value), `SESSION_COOKIE_SECURE=true`,
`OTP_PROVIDER`, `OTP_DEBUG=false`, `NMC_ADMIN_USERNAME`, `NMC_ADMIN_PASSWORD`,
`CORS_ORIGINS=https://vendormap-eta.vercel.app`. Add only approved exact custom
or preview origins. Keep `ALLOW_DESTRUCTIVE_DB_RESET=false`; Render supplies
`PORT`. Optional routing/Gemini/WhatsApp names are listed in deployment.md.

**Vendor OTP remains a release blocker:** the only provider is the development
stub, which cannot deliver messages and is correctly refused in production.
A real provider must be implemented and configured; an invented environment
value or development environment label is not a solution. Admin session
integration does not depend on SMS delivery.

Both frontend and backend need redeployment. Check Render's intended DB and
environment before pushing if Git deploys automatically. Do not replace the
database or HMAC secret, run model rebuilds, or run migration/reset commands.

## Live observations and required post-deployment checks

Read-only checks on 2026-10-27 found Vercel `/` returning 200, but `/api/health`
and `/admin/requests` still returning 404: the local proxy and SPA changes have
not been deployed. Render initially timed out at 55 seconds; a retry returned
200 from `/api/health` and the required 401 from anonymous `/api/auth/me`, with
the exact Vercel CORS origin and credential support. No production account was
used and no production records were changed.

After deployment:

1. Verify Vercel `/api/health` returns JSON, and refreshing `/admin/requests` and
   `/vendor/profile` serves the SPA.
2. Use a safe officer account. Confirm login and `/api/auth/me` return 200,
   the cookie belongs to the Vercel host with HttpOnly/Secure/SameSite=Lax, and
   overview, pending applications, directory and zones authorize after refresh.
3. Confirm logout revokes the cookie/session, expired sessions return 401, and
   vendors receive 403 from admin APIs. Verify vendor OTP delivery before
   attempting the safe vendor profile/recommendation/application/slot workflow.
4. Confirm API responses have private/no-store headers and never appear in
   service-worker Cache Storage. Activate the waiting PWA update and recheck
   refresh/session preservation and generic offline behavior.
5. Verify the actual DB target, existing records, saved geographic slots,
   mounted storage, permissions and model/GIS files. Back up first; after a
   controlled Render restart verify records and unexpired sessions persist.

Local tests do not establish production PostgreSQL connectivity, disk mounts,
restart durability, clean Linux dependency installation, real OTP delivery or
physical-device installation. The Render/Vercel dashboards and production
records were not accessible for inspection. No deployment or Git commit/push
was performed.

## Safe normal commit and push

From the repository root on `main`, review the prepared paths before committing:

```powershell
git diff --check
git add -- .env.example README.md backend docs frontend scripts/migrate_sqlite_to_postgres.py tests reports/vendormap/pwa-results.json
git diff --cached --stat
git diff --cached --name-only
git commit -m "Finalize production integration and safe backend cleanup"
git push origin main
```

The add list includes the confirmed hook deletion and schema archive. It
excludes `data/` and `models/`; `.env`, frontend `.env`, database files, caches
and build output remain ignored. Review unrelated future changes separately.
No force push, branch reset or production migration is required.
