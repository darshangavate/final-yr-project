# Backend ownership and entry points

Run production from the repository root so the backend, data and models are
available. See [deployment settings](../docs/deployment.md).

```sh
pip install -r backend/requirements/app.txt
python -m uvicorn backend.api:app --host 0.0.0.0 --port $PORT --workers 1
```

Render supplies `PORT`. Local development also supports the established
`python -m uvicorn api:app --app-dir backend --reload` command. Provision a new
local database explicitly with `python backend/init_db.py`; provisioning and
research commands are not production health checks.

## Responsibilities

| Location | Ownership |
| --- | --- |
| `application.py`, `api.py` | FastAPI composition, middleware, routers and compatible ASGI entry |
| `core/config.py`, `bootstrap.py`, `core/lifecycle.py` | Environment precedence, validated configuration and ordered startup |
| `core/security.py` | Session lookup, role/ownership enforcement and secure cookies |
| `core/context.py` | Compatibility bindings shared by routers and established tests; preserve its patch hooks |
| `core/data_paths.py`, `core/serialization.py` | Repository-relative assets, version manifests and JSON conversion |
| `routers/`, `schemas/` | HTTP contracts, validation and allowed response fields |
| `repositories/database.py` | SQLAlchemy metadata, connections, transactions and persistence |
| `services/authentication.py`, `services/vendors.py` | OTP/sessions, profiles and admin directory |
| `services/pilot_slots.py`, `services/pilot_access.py`, `services/navigation.py` | Slot workflows, access checks and optional routing |
| `services/capacity_analysis.py`, `services/relocation_recommendations.py` | Operational/planning capacity and advisory relocation |
| `services/historical_associations.py`, `services/historical_occupancy_reconciliation.py`, `services/legacy_allocation_import.py` | Historical evidence and controlled reconciliation, not startup reset jobs |
| Other `services/` modules | Authorized recommendation context, eligibility, explanations, response projection and safe unavailable-GIS errors |
| `geo/` | Existing geographic generation, validation and saved-reference resolution |
| `whatsapp/` | Signed Meta webhooks, conversations, templates and local simulator |

## Numerical, research and compatibility code retained deliberately

- `mcda_engine.py`, `decision_policy.py`, `business_aware_mcda.py` and
  `recommendation_explanations.py`: operational ranking, weights and the
  explicitly controlled category-aware experiment.
- `allocation_optimizer.py`, `allocation_planning_service.py`: advisory Goal
  Programming; final allocation remains a transactional officer action.
- `recommendation_evaluation.py`: sensitivity helpers referenced by the API and
  research workflows.
- `ahp_calibration.py`, `expert_label_aggregation.py`,
  `suitability_model_training.py`: reproducible research pipelines used by
  `scripts/generate_synthetic_expert_demo.py` and project evidence.
- `05_recommend.py`: explicit operational recommendation CLI. The distinct
  `research/mcda_baseline.py` and its `scripts/` bridge also remain.
- `app_config.py`, `auth_service.py`, `database.py`, `vendor_service.py`,
  `pilot_geometry.py`, `pilot_slots.py`, `pilot_navigation.py`: compatibility
  aliases used by tests, scripts and documented commands, not duplicate code.
- Package `__init__.py` files preserve stable package imports.

The unused `_LEGACY_SQLITE_SCHEMA` literal moved intact to
[the schema archive](../docs/archive/pre-sqlalchemy-schema.sql). This is
historical documentation, **not an executable migration**. `METADATA` and
existing additive upgrades remain authoritative.

## Production and test safety

Production requires secure cookies, disabled OTP debugging, an explicit
existing database with operational tables and zone records, and
`ALLOW_DESTRUCTIVE_DB_RESET=false`. SQLite must already exist on a persistent
disk at an absolute path. An unavailable PostgreSQL connection or empty schema
cannot trigger an automatic replacement database. Existing additive upgrades
run only after these checks. Preserve the database and `AUTH_HMAC_SECRET`.

Both roles use `/api/auth/me`. The Vercel first-party `/api` proxy avoids
cross-site cookie restrictions; API responses are private/no-store. A real OTP
delivery provider remains a release dependency; development settings cannot
serve as a production bypass.

Tests use disposable databases. `tests/operational_fixtures.py` reads saved
geometry from the local database without modifying it, and clears assignments
and request references in the copy. Existing geographic slots are preserved.
Historical OpenAPI/asset/AST snapshots remain evidence; differences from the
current v2 model are reported rather than silently rebased.
