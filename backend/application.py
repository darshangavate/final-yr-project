"""ASGI composition; startup performs the existing idempotent bootstrap."""
from backend.core.config import get_cors_origins
from backend.core import context
from backend.core.context import FastAPI, CORSMiddleware, lifespan
from fastapi import Request
from fastapi.responses import JSONResponse
app = FastAPI(
    title=(
        "Nashik Vendor Zoning "
        "Decision-Support API"
    ),
    version="0.2.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PATCH",
        "DELETE",
        "PUT",
    ],
    allow_headers=[
        "Content-Type",
    ],
)


@app.middleware("http")
async def private_api_responses(request: Request, call_next):
    """Keep sessions/private data out of browser and reverse-proxy caches."""
    is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
    origin = request.headers.get("origin")
    # Preserve SameSite=Lax and reject browser writes from untrusted origins,
    # including login CSRF. Browsers alone control Sec-Fetch-Site; retain LAN
    # support when Vite forwards a same-origin request under a changed Host.
    if (is_api and request.method not in {"GET", "HEAD", "OPTIONS"} and origin
            and request.headers.get("sec-fetch-site") != "same-origin"
            and origin not in {str(request.base_url).rstrip("/"), *get_cors_origins()}):
        response = JSONResponse({"detail": "Untrusted request origin"}, status_code=403)
    else:
        response = await call_next(request)
    if is_api:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
    return response

from backend.routers import health
app.include_router(health.router)
context.health = health.health
from backend.routers import auth
app.include_router(auth.router)
context.request_vendor_otp = auth.request_vendor_otp
context.verify_vendor_otp = auth.verify_vendor_otp
context.admin_login = auth.admin_login
context.current_user = auth.current_user
context.logout = auth.logout
from backend.routers import vendors
app.include_router(vendors.router)
context.register_operational_vendor = vendors.register_operational_vendor
context.vendor_dashboard = vendors.vendor_dashboard
context.unified_vendor_profile = vendors.unified_vendor_profile
context.patch_vendor_profile = vendors.patch_vendor_profile
context.vendor_active_allocation = vendors.vendor_active_allocation
context.vendor_eligible_zones = vendors.vendor_eligible_zones
context.vendor = vendors.vendor
from backend.routers import allocations
app.include_router(allocations.router)
context.submit_allocation_request = allocations.submit_allocation_request
context.allocation_request_list = allocations.allocation_request_list
context.allocation_request_detail = allocations.allocation_request_detail
context.cancel_request = allocations.cancel_request
context.approve_request = allocations.approve_request
context.reject_request = allocations.reject_request
context.allocation_list = allocations.allocation_list
context.allocation_detail = allocations.allocation_detail
context.create_allocation_plan = allocations.create_allocation_plan
context.admin_overview = allocations.admin_overview
from backend.routers import zones
app.include_router(zones.router)
context.zone_live_status = zones.zone_live_status
context.patch_zone_live_status = zones.patch_zone_live_status
context.options = zones.options
context.geometry = zones.geometry
context.zone = zones.zone
context.admin_zones = zones.admin_zones
context.official_zones = zones.official_zones
context.catalog = zones.catalog
from backend.routers import recommendations
app.include_router(recommendations.router)
context.recommendations = recommendations.recommendations
context.recommendation_sensitivity = recommendations.recommendation_sensitivity
from backend.routers import research
app.include_router(research.router)
context.research_status = research.research_status
context.implementation_summary = research.implementation_summary
context.overview = research.overview
context.review_summary = research.review_summary
from backend.routers import pilot_slots
app.include_router(pilot_slots.router)
context.get_pilot_layout = pilot_slots.get_pilot_layout
context.initialize_pilot_layout = pilot_slots.initialize_pilot_layout
context.get_pilot_slot = pilot_slots.get_pilot_slot
context.patch_pilot_slot = pilot_slots.patch_pilot_slot
context.preview_pilot_geometry = pilot_slots.preview_pilot_geometry
context.validate_pilot_edit = pilot_slots.validate_pilot_edit
context.save_pilot_geometry = pilot_slots.save_pilot_geometry
from backend.routers import navigation
app.include_router(navigation.router)
context.pilot_navigation_destination = navigation.pilot_navigation_destination
context.pilot_walking_route = navigation.pilot_walking_route
from backend.whatsapp import webhook
app.include_router(webhook.router)
context.verify_whatsapp_webhook = webhook.verify_webhook
context.receive_whatsapp_webhook = webhook.receive_webhook
context.app = app
