# Architecture after cleanup

The stack remains React 18 + Vite, FastAPI, Python, SQLite, React Leaflet,
Shapely/PyProj and the existing numerical libraries. Persistence uses SQLAlchemy
Core with SQLite/PostgreSQL; numerical algorithms and the approved UI remain
unchanged. See [backend ownership](../backend/README.md) for retained modules.

## Structure and ownership

```text
backend/
  api.py                         compatible ASGI entry
  application.py                 app, middleware and router composition
  core/                          config, security, lifecycle, serialization
  routers/                       auth, vendors, zones, recommendations,
                                 allocations, pilot_slots, navigation,
                                 research, health
  schemas/                       existing Pydantic contracts and projections
  services/                      authentication, vendors, pilot_slots,
                                 navigation, responses, explanations,
                                 recommendation_context, pilot_access
  repositories/database.py       SQLAlchemy metadata and portable transactions
  geo/pilot_geometry.py          geographic generation and validation
  mcda_engine.py                 operational weighted MCDA
  allocation_optimizer.py        advisory Goal Programming
  decision_policy.py             weights/AHP policy
  *_evaluation.py / *_training.py existing research experiments
frontend/src/
  main.jsx                       React entry and language provider
  app/ImplementationApp.jsx      role-aware shell and existing navigation
  features/
    auth/                        login/OTP screens and authentication CSS
    admin/                       dashboard, tabs, map and view models
    vendor/                      portal, workspace hook, forms/maps/sections
    pilot-slots/                 single slot map, lifecycle, selection state
    navigation/                  routing/GPS hook and navigation utilities
  shared/                        UI, maps and utilities
  services/api.js                 one cookie-authenticated request client
  styles/tokens.css               global design tokens
  styles/foundation.css           ordered global/shared styling
  i18n/                          unchanged multilingual infrastructure
research/mcda_baseline.py         separate historical MCDA implementation
scripts/                         preprocessing, clustering, research CLIs
app/expert_review.py              retained Streamlit review entry
backend/requirements/            app, research, dev and verified constraints
tests/                           backend workflows and architecture guardrails
frontend/tests/                  browser-only fixtures and smoke runner
data/ models/ reports/           preserved runtime/research assets
docs/                            contracts, migration manifests and deployment
```

The browser calls `services/api.js`, which retains cookie credentials and uses
same-origin `/api` through Vite locally and Vercel in production. FastAPI routers validate request
schemas and enforce session roles/ownership through security helpers. Services
perform domain workflows and call the SQLAlchemy repository. Repository transaction
boundaries and capacity checks remain intact. Startup retains the original
idempotent bootstrap; the refactor did not run it against the live database.

Operational recommendations use `backend.mcda_engine`, existing evidence and
weights, personalized preferences and live capacity. Goal Programming remains
advisory, with final allocation checks in existing transactions. Research callers
explicitly select `research.mcda_baseline` or `backend.mcda_engine`; these are
different implementations and were not merged. K-Means artifacts, GIS processing,
AHP and supervised experiments remain in their established pipelines.

Pilot services use saved WGS84 boundaries and geometry validation in `geo`.
Navigation uses the existing assigned-slot endpoint and server-side routing
provider; the React hook manages GPS and route state within the same Leaflet map.
Provider failure leaves the slot visible without inventing a route.

## Moves, splits and removals

Backend moves: `app_config` -> `core/config`, `database` ->
`repositories/database`, `auth_service` -> `services/authentication`,
`vendor_service` -> `services/vendors`, `pilot_geometry` ->
`geo/pilot_geometry`, `pilot_slots` -> `services/pilot_slots`,
`pilot_navigation` -> `services/navigation`. Old modules are small alias wrappers.
The former API is split across nine routers, schema modules and shared helpers.

Frontend moves are enumerated in [frontend-moves.json](frontend-moves.json).
Meaningful splits include vendor workspace state, navigation/GPS state, admin
tab panels, recommendation maps/evidence and map lifecycle components. Shared
primitives handle repeated presentation. The unused generic data hook was
removed after reference auditing; feature workspace hooks remain.

Five global CSS entry points are consolidated in their previous declaration
order, with provenance comments. Twenty exact duplicate rules and fifteen dead
schematic rules were removed. Authentication and feature CSS remain separately
owned. Nonidentical overrides required by the existing design remain ordered;
this is a compatibility foundation, not a complete visual redesign.
See [css-cleanup.json](css-cleanup.json).

Every actual removal has evidence in
[deletion-manifest.json](deletion-manifest.json): five empty placeholders, one
byte-identical unused root logo, and five replaced CSS sources. The public NMC
logo remains at the same URL. Uncertain files and existing user scripts remain.

## Compatibility and protected assets

`uvicorn api:app --app-dir backend`, `backend.api:app`, direct database setup,
both recommendation CLIs, Streamlit review and historical Python module paths
remain available. The API entry aliases a shared compatibility context so older
tests/callers can still patch database/session bindings. New code should import
the owning modules directly. This bridge is deliberate residual coupling, not a
new dependency-injection framework.

The original OpenAPI, asset and AST snapshots remain historical evidence;
current v2 behavior has documented pre-existing differences. The local SQLite
default remains `data/operational/vendor_management.db`; production requires an
explicit existing `DATABASE_URL`. Saved pilot boundaries remain
`data/pilot/lekha_nagar_boundaries.geojson`. Original files beneath `data`,
`models` and `reports` have SHA-256 guards. Selected scientific function/class
ASTs are compared with the original source backup, permitting import changes
without silently rewriting algorithm bodies.

## Remaining debt

The shell retains state-driven navigation, deep-link restoration and history
replacement. Some research and numerical
files remain large to preserve reproducibility. The compatibility context and
ordered CSS overrides can be reduced later with focused behavior tests.
SQLite deployment requires controlled writers, persistent storage and backups.
Fixtures do not establish live OTP delivery, GPS accuracy or provider uptime.
