"""Vendors HTTP endpoints; existing contracts retained."""
from __future__ import annotations
from backend.core import context as dependencies
from backend.schemas.api import ActiveVendorAllocationResponse
from backend.services.authentication import AuthError
from backend.mcda_engine import DATA
from backend.decision_policy import active_mcda_configuration
from backend.services.zone_references import resolve_zone_locations
from backend.services.eligibility import zone_ineligibility_reason
from fastapi import Request as FastAPIRequest
from fastapi import HTTPException
from backend.repositories.database import OperationalConflictError
from backend.repositories.database import OperationalNotFoundError
from backend.repositories.database import OperationalValidationError
from backend.schemas.api import VendorProfileResponse
from backend.schemas.api import VendorProfilePhotoUpdate
from backend.schemas.api import VendorProfileUpdateBody
from backend.schemas.api import VendorRegistrationBody
from backend.services.responses import allocation_response
from backend.core.security import auth_error
from backend.services.authentication import bind_onboarding_session
from backend.core.serialization import clean
from backend.repositories.database import get_active_allocation_for_vendor
from backend.repositories.database import get_live_zone_status_map
from backend.repositories.database import get_vendor_dashboard_activity
from backend.services.vendors import get_vendor_profile
from backend.services.vendors import get_vendor_profile_photo
from backend.services.vendors import save_vendor_profile_photo
from fastapi import status as http_status
from backend.services.explanations import official_zone_descriptions
from backend.core.security import operational_error
import pandas as pd
from backend.services.vendors import register_vendor
from backend.core.security import request_session
from backend.core.security import require_vendor_owner
from backend.services.vendors import update_vendor_profile
from fastapi import APIRouter, Query
router = APIRouter()

@router.post(
    "/api/vendors",
    status_code=(
        http_status
        .HTTP_201_CREATED
    ),
    response_model=(
        VendorProfileResponse
    ),
    tags=[
        "Vendors"
    ],
)
def register_operational_vendor(
    payload: VendorRegistrationBody,
    request: FastAPIRequest,
):

    session = request_session(
        request,
        "NEW_VENDOR_ONBOARDING",
    )


    try:

        profile = register_vendor(
            payload.model_dump(),
            database_path=dependencies.DATABASE_PATH,
        )


        if not dependencies.AUTH_TEST_BYPASS:

            bind_onboarding_session(
                session[
                    "session_id"
                ],
                profile[
                    "vendor_id"
                ],
                database_path=dependencies.DATABASE_PATH,
            )


        return profile


    except AuthError as exc:

        auth_error(
            exc
        )


    except (
        OperationalConflictError,
        OperationalNotFoundError,
        OperationalValidationError,
    ) as exc:

        operational_error(
            exc
        )

@router.get(
    "/api/vendors/me/dashboard",
    tags=[
        "Vendors"
    ],
)
def vendor_dashboard(
    request: FastAPIRequest,
):

    session = request_session(
        request,
        "VENDOR",
    )


    vendor_id = session[
        "vendor_id"
    ]


    profile = get_vendor_profile(
        vendor_id,
        database_path=dependencies.DATABASE_PATH,
    )


    if profile is None:

        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            "Vendor profile not found",
        )


    activity = (
        get_vendor_dashboard_activity(
            vendor_id,
            dependencies.DATABASE_PATH,
        )
    )


    descriptions = (
        official_zone_descriptions()
    )


    active_row = (
        activity[
            "active_allocation"
        ]
    )


    active_allocation = None


    if active_row is not None:

        active_allocation = {
            "allocation_id":
                active_row[
                    "allocation_id"
                ],

            "zone_id":
                active_row[
                    "zone_id"
                ],

            "zone_description":
                descriptions.get(
                    active_row[
                        "zone_id"
                    ]
                ),

            "allocation_type":
                active_row[
                    "allocation_type"
                ],

            "allocated_at":
                active_row[
                    "allocated_at"
                ],

            "status":
                active_row[
                    "status"
                ],

            "live_zone_status":
                active_row[
                    "live_zone_status"
                ],

            "official_capacity":
                active_row[
                    "official_capacity"
                ],

            "current_vendor_count":
                active_row[
                    "current_vendor_count"
                ],

            "available_capacity":
                active_row[
                    "available_capacity"
                ],
        }


    requests = []


    counts = {
        status:
            0
        for status
        in (
            "PENDING",
            "APPROVED",
            "REJECTED",
            "CANCELLED",
        )
    }


    for row in (
        activity[
            "requests"
        ]
    ):

        counts[
            row[
                "status"
            ]
        ] += 1


        previous_zone_id = (
            activity[
                "previous_zone_by_request"
            ].get(
                row[
                    "request_id"
                ]
            )
        )


        if (
            previous_zone_id
            is None

            and row[
                "request_type"
            ]
            == "RELOCATION"

            and row[
                "status"
            ]
            == "PENDING"

            and active_row
            is not None
        ):

            previous_zone_id = (
                active_row[
                    "zone_id"
                ]
            )


        requests.append(
            {
                "request_id":
                    row[
                        "request_id"
                    ],

                "request_type":
                    row[
                        "request_type"
                    ],

                "requested_zone_id":
                    row[
                        "zone_id"
                    ],

                "zone_description":
                    descriptions.get(
                        row[
                            "zone_id"
                        ]
                    ),

                "status":
                    row[
                        "status"
                    ],

                "created_at":
                    row[
                        "created_at"
                    ],

                "updated_at":
                    row[
                        "updated_at"
                    ],

                "reviewed_at":
                    row[
                        "reviewed_at"
                    ],

                "rejection_reason":
                    row[
                        "rejection_reason"
                    ],

                "previous_zone_id":
                    previous_zone_id,

                "previous_zone_description":
                    descriptions.get(
                        previous_zone_id
                    ),
            }
        )


    has_pending = (
        counts[
            "PENDING"
        ]
        > 0
    )


    profile_active = (
        profile[
            "status"
        ]
        == "ACTIVE"
    )


    blocked_reason = None


    if not profile_active:

        blocked_reason = (
            "Vendor profile is inactive"
        )


    elif has_pending:

        blocked_reason = (
            "Pending request already exists"
        )


    return {
        "profile": {
            "vendor_id":
                profile[
                    "vendor_id"
                ],

            "application_vendor_id":
                profile[
                    "application_vendor_id"
                ],

            "vendor_type":
                profile[
                    "vendor_type"
                ],

            "full_name":
                profile[
                    "full_name"
                ],

            "business_name":
                profile[
                    "business_name"
                ],

            "business_category":
                profile[
                    "business_category"
                ],

            "preferred_division":
                profile[
                    "preferred_division"
                ],

            "preferred_locality":
                profile[
                    "preferred_locality"
                ],

            "priority_profile":
                profile[
                    "priority_profile"
                ],

            "prefer_parking":
                profile[
                    "parking_preference"
                ],

            "prefer_transport":
                profile[
                    "transport_preference"
                ],

            "prefer_market":
                profile[
                    "market_preference"
                ],

            "language":
                profile[
                    "preferred_language"
                ],

            "status":
                profile[
                    "status"
                ],

            "created_at":
                profile[
                    "created_at"
                ],

            "updated_at":
                profile[
                    "updated_at"
                ],
        },

        "active_allocation":
            active_allocation,

        "requests":
            requests,

        "request_summary": {
            "total_requests":
                len(
                    requests
                ),

            "pending_count":
                counts[
                    "PENDING"
                ],

            "approved_count":
                counts[
                    "APPROVED"
                ],

            "rejected_count":
                counts[
                    "REJECTED"
                ],

            "cancelled_count":
                counts[
                    "CANCELLED"
                ],
        },

        "action_state": {
            "has_active_allocation":
                active_allocation
                is not None,

            "has_pending_request":
                has_pending,

            "can_submit_request":
                (
                    profile_active
                    and not has_pending
                ),

            "blocked_reason":
                blocked_reason,
        },
    }

@router.get(
    "/api/vendors/{vendor_id}",
    response_model=VendorProfileResponse,
    tags=[
        "Vendors"
    ],
)
def unified_vendor_profile(
    vendor_id: str,
    request: FastAPIRequest,
):

    require_vendor_owner(
        request,
        vendor_id,
    )


    profile = get_vendor_profile(
        vendor_id,
        database_path=dependencies.DATABASE_PATH,
    )


    if profile is None:

        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            f"Unknown vendor_id: {vendor_id}",
        )

    return profile

@router.patch(
    "/api/vendors/{vendor_id}",
    response_model=VendorProfileResponse,
    tags=[
        "Vendors"
    ],
)
def patch_vendor_profile(
    vendor_id: str,
    payload: VendorProfileUpdateBody,
    request: FastAPIRequest,
):

    require_vendor_owner(
        request,
        vendor_id,
    )


    try:

        return update_vendor_profile(
            vendor_id,
            payload.model_dump(
                exclude_unset=True
            ),
            database_path=dependencies.DATABASE_PATH,
        )


    except (
        OperationalConflictError,
        OperationalNotFoundError,
        OperationalValidationError,
    ) as exc:

        operational_error(
            exc
        )

@router.get(
    "/api/vendors/{vendor_id}/active-allocation",
    response_model=(
        ActiveVendorAllocationResponse
    ),
    tags=[
        "Vendors"
    ],
)
def vendor_active_allocation(
    vendor_id: str,
    request: FastAPIRequest,
):

    require_vendor_owner(
        request,
        vendor_id,
    )


    if (
        get_vendor_profile(
            vendor_id,
            database_path=dependencies.DATABASE_PATH,
        )
        is None
    ):

        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            f"Unknown vendor_id: {vendor_id}",
        )


    row = (
        get_active_allocation_for_vendor(
            vendor_id,
            database_path=dependencies.DATABASE_PATH,
        )
    )


    return {
        "allocation":
            (
                allocation_response(
                    row
                )

                if row
                is not None

                else None
            )
    }

@router.get(
    "/api/vendors/me/eligible-zones",
    tags=[
        "Vendors"
    ],
)
def vendor_eligible_zones(
    request: FastAPIRequest,
):
    """Return map context without duplicating recommendation scoring."""

    session = request_session(
        request,
        "VENDOR",
    )


    profile = get_vendor_profile(
        session[
            "vendor_id"
        ],
        database_path=dependencies.DATABASE_PATH,
    )


    zones = pd.read_csv(
        DATA
        / "candidate_zone_snapshot.csv",
        dtype={
            "zone_id":
                str,
        },
    )


    live = (
        get_live_zone_status_map(
            dependencies.DATABASE_PATH
        )
    )


    zones = zones.loc[zones.zone_type.eq("FREE")].copy()
    if active_mcda_configuration() is None:
        zones = zones.loc[zones.geometry_usable_for_reference_model.astype(str).str.lower().isin(["true","1","yes"])].copy()


    activity = (
        get_vendor_dashboard_activity(
            session[
                "vendor_id"
            ],
            dependencies.DATABASE_PATH,
        )
    )


    pending = next(
        (
            item
            for item
            in activity[
                "requests"
            ]
            if item[
                "status"
            ]
            == "PENDING"
        ),
        None,
    )


    active = activity[
        "active_allocation"
    ]


    current_zone_id = (
        active[
            "zone_id"
        ]
        if active
        else None
    )


    pending_zone_id = (
        pending[
            "zone_id"
        ]
        if pending
        else None
    )


    highlighted_ids = {
        zone_id

        for zone_id
        in (
            current_zone_id,
            pending_zone_id,
        )

        if zone_id
    }


    records = []


    for row in zones.itertuples(
        index=False
    ):

        state = live.get(
            row.zone_id
        )


        if state is None:
            continue


        available = state[
            "available_capacity"
        ]


        eligible = zone_ineligibility_reason(row.zone_type, state) is None


        if (
            not eligible

            and row.zone_id
            not in highlighted_ids
        ):
            continue


        records.append(
            {
                "zone_id":
                    row.zone_id,

                "zone_division":
                    row.zone_division,

                "zone_type":
                    row.zone_type,

                "official_description":
                    row.official_description,

                "official_capacity":
                    state[
                        "official_capacity"
                    ],

                "current_vendor_count":
                    state[
                        "current_vendor_count"
                    ],

                "available_capacity":
                    available,

                "live_status":
                    state[
                        "status"
                    ],

                "eligible":
                    eligible,

                "preferred_division_match":
                    bool(
                        profile
                        and profile.get(
                            "preferred_division"
                        )
                        and row.zone_division
                        == profile[
                            "preferred_division"
                        ]
                    ),
            }
        )


    if active_mcda_configuration() is not None:
        locations = resolve_zone_locations(dependencies.DATABASE_PATH)
        for record in records:
            record["resolved_location"] = locations[record["zone_id"]]
            record["title"] = record["zone_id"]
            record["description"] = record.get("official_description")
    return {
        "zones":
            records,

        "count":
            len(
                records
            ),

        "current_zone_id":
            current_zone_id,

        "pending_zone_id":
            pending_zone_id,
    }

@router.get(
    "/api/vendor/{vendor_id}"
)
def vendor(
    vendor_id: str,
):

    # This remains a model-data lookup,
    # not an identity-verification endpoint.

    vendors = pd.read_csv(
        DATA
        / "vendor_training_registry.csv",
        dtype={
            "vendor_id":
                str,
        },
    )


    found = vendors.loc[
        vendors.vendor_id.eq(
            vendor_id
        )
    ]


    if found.empty:

        raise HTTPException(
            404,
            "Vendor ID not found",
        )


    row = found.iloc[
        0
    ]


    return clean(
        {
            "vendor_id":
                row.vendor_id,

            "business_category":
                row.business_category,

            "division":
                row.vendor_division,

            "historical_proposed_zone_id":
                row.proposed_zone_id,

            "current_zone_verified":
                row.current_zone_verified,

            "notice":
                (
                    "A historical proposed zone is "
                    "not a verified current vending location."
                ),
        }
    )


@router.get("/api/admin/vendors", tags=["Vendors"])
def admin_vendor_directory(request: FastAPIRequest, q: str = "", business_category: str = "", division: str = "",
                           zone_id: str = "", mapping_status: str = "",
                           page: int = Query(1, ge=1),
                           page_size: int = Query(25, ge=1, le=100)):
    request_session(request, "ADMIN")
    from backend.services.vendors import list_admin_vendors
    try:
        return list_admin_vendors(q=q, business_category=business_category, division=division,
                                  zone_id=zone_id, mapping_status=mapping_status,
                                  page=page, page_size=page_size, database_path=dependencies.DATABASE_PATH)
    except OperationalValidationError as exc:
        operational_error(exc)


@router.get("/api/admin/vendors/{vendor_id}/details", tags=["Vendors"])
def admin_vendor_management_details(vendor_id: str, request: FastAPIRequest):
    request_session(request, "ADMIN")
    from backend.services.vendors import admin_vendor_details
    try:
        return admin_vendor_details(vendor_id, dependencies.DATABASE_PATH)
    except (OperationalNotFoundError, OperationalValidationError) as exc:
        operational_error(exc)


@router.get("/api/admin/vendors/{vendor_id}/relocation-recommendations", tags=["Vendors", "Admin"])
def admin_vendor_relocation_recommendations(vendor_id: str, request: FastAPIRequest,
                                             limit: int = Query(5, ge=1, le=20),
                                             include_all: bool = False):
    request_session(request, "ADMIN")
    from backend.services.relocation_recommendations import relocation_recommendations
    try:
        return relocation_recommendations(vendor_id, limit=limit, include_all=include_all,
                                          database_path=dependencies.DATABASE_PATH)
    except (OperationalNotFoundError, OperationalValidationError, ValueError) as exc:
        operational_error(exc)


@router.get("/api/vendors/me/account-settings", tags=["Vendors"])
def vendor_account_settings(request: FastAPIRequest):
    session = request_session(request, "VENDOR")
    from backend.services.vendors import vendor_account_information
    try:
        return vendor_account_information(session["vendor_id"], dependencies.DATABASE_PATH, session.get("mobile_last4"))
    except (OperationalNotFoundError, OperationalValidationError) as exc:
        operational_error(exc)


@router.get("/api/vendors/me/profile-photo", tags=["Vendors"])
def vendor_profile_photo(request: FastAPIRequest):
    session = request_session(request, "VENDOR")
    return get_vendor_profile_photo(session["vendor_id"], dependencies.DATABASE_PATH)


@router.put("/api/vendors/me/profile-photo", tags=["Vendors"])
def update_vendor_profile_photo(payload: VendorProfilePhotoUpdate, request: FastAPIRequest):
    session = request_session(request, "VENDOR")
    try:
        return save_vendor_profile_photo(session["vendor_id"], payload.data_url, dependencies.DATABASE_PATH)
    except (OperationalNotFoundError, OperationalValidationError) as exc:
        operational_error(exc)


@router.get("/api/vendors/me/activity", tags=["Vendors"])
def vendor_notifications_activity(request: FastAPIRequest):
    session = request_session(request, "VENDOR")
    from backend.services.vendors import vendor_activity
    try:
        return vendor_activity(session["vendor_id"], dependencies.DATABASE_PATH)
    except (OperationalNotFoundError, OperationalValidationError) as exc:
        operational_error(exc)
