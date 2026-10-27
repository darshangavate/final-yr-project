"""Zones HTTP endpoints; existing contracts retained."""
from __future__ import annotations
from backend.core.data_paths import dataset_path, CATALOG
from backend.core import context as dependencies
from backend.schemas.api import AdminZoneStatusBody
from backend.mcda_engine import CATEGORIES
from backend.mcda_engine import DATA
from fastapi import Request as FastAPIRequest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from backend.repositories.database import OperationalConflictError
from backend.repositories.database import OperationalNotFoundError
from backend.repositories.database import OperationalValidationError
from fastapi import Query
from backend.mcda_engine import ROOT
from backend.schemas.response_fields import ZONE_LIVE_STATUS_RESPONSE_FIELDS
from backend.schemas.api import ZoneLiveStatusResponse
from backend.core.serialization import clean
from backend.repositories.database import get_live_zone_status_map
from backend.repositories.database import get_zone_live_status
from fastapi import status as http_status
import json
import csv
import math
from backend.decision_policy import load_base_weight_policy, active_mcda_configuration
from backend.services.zone_references import resolve_zone_locations
from backend.core.security import operational_error
import pandas as pd
from backend.core.security import request_session
from backend.repositories.database import update_zone_live_status
from fastapi import APIRouter
router = APIRouter()

@router.get(
    "/api/zones/{zone_id}/live-status",
    response_model=(
        ZoneLiveStatusResponse
    ),
    tags=[
        "Zone Live Status"
    ],
)
def zone_live_status(
    zone_id: str,
):

    row = get_zone_live_status(
        zone_id,
        database_path=dependencies.DATABASE_PATH,
    )


    if row is None:

        raise HTTPException(
            http_status.HTTP_404_NOT_FOUND,
            f"Unknown zone_id: {zone_id}",
        )


    return {
        field:
            row[
                field
            ]

        for field
        in ZONE_LIVE_STATUS_RESPONSE_FIELDS
    }

@router.patch(
    "/api/zones/{zone_id}/live-status",
    response_model=(
        ZoneLiveStatusResponse
    ),
    tags=[
        "Admin",
        "Zone Live Status",
    ],
)
def patch_zone_live_status(
    zone_id: str,
    payload: AdminZoneStatusBody,
    request: FastAPIRequest,
):

    session = request_session(
        request,
        "ADMIN",
    )


    verified_by = (
        payload.verified_by

        if (
            dependencies.AUTH_TEST_BYPASS
            and payload.verified_by
        )

        else session[
            "admin_id"
        ]
    )


    try:

        row = update_zone_live_status(
            zone_id,
            payload.status,
            verified_by,
            database_path=dependencies.DATABASE_PATH,
        )


    except (
        OperationalNotFoundError,
        OperationalConflictError,
        OperationalValidationError,
    ) as exc:

        operational_error(
            exc
        )


    return {
        field:
            row[
                field
            ]

        for field
        in ZONE_LIVE_STATUS_RESPONSE_FIELDS
    }

@router.get(
    "/api/options"
)
def options():

    zones = pd.read_csv(
        DATA
        / "candidate_zone_snapshot.csv",
        usecols=[
            "zone_division"
        ],
    )


    divisions = sorted(
        value

        for value
        in zones[
            "zone_division"
        ]
        .dropna()
        .unique()

        if value
        != "Citywide"
    )


    policy = (
        load_base_weight_policy()
    )


    return {
        "categories":
            CATEGORIES,

        "divisions":
            divisions,

        "mode":
            "research",

        "weight_source":
            policy[
                "source"
            ],

        "notice":
            (
                "Recommendations use operational "
                "zone status when available. "
                "Historical vendor evidence is not "
                "treated as current occupancy, "
                "vacancy, demand, or business success."
            ),
    }

@router.get(
    "/api/zones/geometry"
)
def geometry():
    """Return analytical reference points only."""

    path = (
        ROOT
        / "reports"
        / "data_quality"
        / "geometry_duplicates"
        / "reference_points.geojson"
    )


    if not path.exists():

        raise HTTPException(
            503,
            (
                "Run geometry_duplicate_audit.py "
                "first."
            ),
        )


    return JSONResponse(
        json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    )

@router.get(
    "/api/zone/{zone_id}"
)
def zone(
    zone_id: str,
):

    zones = pd.read_csv(
        DATA
        / "candidate_zone_snapshot.csv",
        dtype={
            "zone_id":
                str,
        },
    )


    result = zones.loc[
        zones.zone_id.eq(
            zone_id
        )
    ]


    if result.empty:

        raise HTTPException(
            404,
            "Zone not found",
        )


    return clean(
        result.iloc[
            0
        ].to_dict()
    )

@router.get(
    "/api/admin/zones",
    tags=[
        "Admin"
    ],
)
def admin_zones(
    request: FastAPIRequest,
    division: str = "",
    zone_type: str = "",
    live_status: str = "",
    q: str = "",
):

    request_session(
        request,
        "ADMIN",
    )


    zones = pd.read_csv(
        ROOT
        / "data"
        / "raw"
        / "nmc"
        / "official_zones.csv",
        dtype={
            "zone_id":
                str,
        },
    )


    live = pd.DataFrame(
        get_live_zone_status_map(
            dependencies.DATABASE_PATH
        ).values()
    )


    if live.empty:

        live = pd.DataFrame(
            columns=[
                "zone_id",
                "official_capacity",
                "current_vendor_count",
                "available_capacity",
                "status",
                "verified_at",
                "verified_by",
                "updated_at",
            ]
        )


    live = live.rename(
        columns={
            "status":
                "live_status",
        }
    )


    zones = zones.merge(
        live,
        on="zone_id",
        how="left",
        suffixes=(
            "_canonical",
            "",
        ),
        validate="one_to_one",
    )


    audit_path = (
        dataset_path("reference_audit")
    )


    if audit_path.exists():

        audit = pd.read_csv(
            audit_path,
            dtype={
                "zone_id":
                    str,
            },
        )


        audit_columns = [
            column

            for column
            in [
                "zone_id",
                "reference_group_id",
                "shared_reference",
                "reference_audit_status",
            ]

            if column
            in audit.columns
        ]


        zones = zones.merge(
            audit[
                audit_columns
            ],
            on="zone_id",
            how="left",
            validate="one_to_one",
        )


    if division:

        zones = zones.loc[
            zones.division.eq(
                division
            )
        ]


    if zone_type:

        zones = zones.loc[
            zones.zone_type.eq(
                zone_type
            )
        ]


    if live_status:

        zones = zones.loc[
            zones.live_status.eq(
                live_status
            )
        ]


    if q:

        term = q.strip()


        zones = zones.loc[
            zones.zone_id.str.contains(
                term,
                case=False,
                na=False,
                regex=False,
            )
            | zones[
                "official_description"
            ].str.contains(
                term,
                case=False,
                na=False,
                regex=False,
            )
        ]


    fields = [
        column

        for column
        in [
            "zone_id",
            "division",
            "zone_type",
            "official_description",
            "official_capacity",
            "current_vendor_count",
            "available_capacity",
            "live_status",
            "verified_at",
            "verified_by",
            "updated_at",
            "reference_group_id",
            "shared_reference",
            "reference_audit_status",
        ]

        if column
        in zones.columns
    ]


    records = [
        clean(
            row
        )

        for row
        in (
            zones[
                fields
            ]
            .sort_values(
                "zone_id"
            )
            .to_dict(
                "records"
            )
        )
    ]

    from backend.services.capacity_analysis import zone_capacity_analysis
    capacity_analysis = zone_capacity_analysis(dependencies.DATABASE_PATH)
    for record in records:
        capacity = capacity_analysis.get(record["zone_id"], {})
        record.update(capacity)
        # Existing names remain as read-only compatibility aliases. Their
        # values now follow the authoritative, de-duplicated planning metric.
        record["historical_vendor_count"] = capacity.get("historical_planning_count", 0)
        record["current_vendor_count"] = capacity.get("operational_occupancy", 0)
        record["available_capacity"] = capacity.get("operational_available")


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
    }


@router.get("/api/admin/zones/{zone_id}/historical-vendors", tags=["Admin"])
def admin_zone_historical_vendors(zone_id: str, request: FastAPIRequest):
    request_session(request, "ADMIN")
    from backend.services.capacity_analysis import zone_historical_vendors
    return zone_historical_vendors(zone_id, dependencies.DATABASE_PATH)


@router.get("/api/admin/capacity-pressure", tags=["Admin"])
def admin_capacity_pressure(request: FastAPIRequest):
    request_session(request, "ADMIN")
    from backend.services.capacity_analysis import capacity_pressure_summary
    return capacity_pressure_summary(dependencies.DATABASE_PATH)

@router.get(
    "/api/official-zones"
)
def official_zones(
    division: str = "",
    zone_type: str = "",
    limit: int = Query(
        308,
        ge=1,
        le=500,
    ),
):

    zones = pd.read_csv(
        ROOT
        / "data"
        / "raw"
        / "nmc"
        / "official_zones.csv",
        dtype={
            "zone_id":
                str,
        },
    )


    if division:

        zones = zones.loc[
            zones.division.eq(
                division
            )
        ]


    if zone_type:

        zones = zones.loc[
            zones.zone_type.eq(
                zone_type
            )
        ]


    audit_path = (
        dataset_path("reference_audit")
    )


    if audit_path.exists():

        audit = pd.read_csv(
            audit_path,
            dtype={
                "zone_id":
                    str,
            },
        )


        columns = [
            column

            for column
            in [
                "zone_id",
                "reference_group_id",
                "shared_reference_group_size",
                "shared_reference",
                "reference_audit_status",
            ]

            if column
            in audit.columns
        ]


        zones = zones.merge(
            audit[
                columns
            ],
            on="zone_id",
            how="left",
            validate="one_to_one",
        )


    cluster_path = (
        dataset_path("kmeans_output")
        / "zones_with_clusters.csv"
    )


    if cluster_path.exists():

        clusters = pd.read_csv(
            cluster_path,
            dtype={
                "zone_id":
                    str,
            },
        )


        if (
            "cluster"
            in clusters.columns
        ):

            zones = zones.merge(
                clusters[
                    [
                        "zone_id",
                        "cluster",
                    ]
                ].rename(
                    columns={
                        "cluster":
                            "environment_cluster",
                    }
                ),
                on="zone_id",
                how="left",
                validate="one_to_one",
            )


    fields = [
        column

        for column
        in [
            "zone_id",
            "division",
            "zone_type",
            "official_description",
            "capacity",
            "category",
            "environment_cluster",
            "reference_group_id",
            "shared_reference_group_size",
            "shared_reference",
            "reference_audit_status",
        ]

        if column
        in zones.columns
    ]


    records = [
        clean(
            row
        )

        for row
        in (
            zones[
                fields
            ]
            .head(
                limit
            )
            .to_dict(
                "records"
            )
        )
    ]


    return {
        "zones":
            records,

        "count":
            int(
                min(
                    len(
                        zones
                    ),
                    limit,
                )
            ),

        "total_matching":
            int(
                len(
                    zones
                )
            ),
    }

@router.get(
    "/api/catalog"
)
def catalog(
    zone_type: str = "",
    division: str = "",
    q: str = "",
    limit: int = 308,
):
    """Official records only; geometry/live state remain separate."""

    path = (
        ROOT
        / "data"
        / "raw"
        / "nmc"
        / "official_zones.csv"
    )


    data = pd.read_csv(
        path,
        dtype={
            "zone_id":
                str,
        },
    )


    if zone_type:

        data = data.loc[
            data.zone_type.eq(
                zone_type
            )
        ]


    if division:

        data = data.loc[
            data.division.eq(
                division
            )
        ]


    if q:

        words = q.strip()


        data = data.loc[
            data.zone_id.str.contains(
                words,
                case=False,
                na=False,
                regex=False,
            )
            | data[
                "official_description"
            ].str.contains(
                words,
                case=False,
                na=False,
                regex=False,
            )
        ]


    columns = [
        "zone_id",
        "division",
        "zone_type",
        "official_description",
        "capacity",
        "category",
    ]


    safe_limit = max(
        1,
        min(
            limit,
            308,
        ),
    )


    return {
        "total":
            len(
                data
            ),

        "zones":
            clean(
                data[
                    columns
                ]
                .head(
                    safe_limit
                )
                .to_dict(
                    "records"
                )
            ),
    }


def display_zone_geometry():
    """Display-only projection keyed by official IDs; never a model input."""
    if active_mcda_configuration() is not None:
        locations = resolve_zone_locations(dependencies.DATABASE_PATH)
        features = [{"type":"Feature", "properties":{**row, "model_version":CATALOG["active_model_version"],
                      "model_reference_source":row["location_source"], "location_status":row["location_source"],
                      "official_boundary_verified":False, "model_features_recalculated":True},
                     "geometry":{"type":"Point", "coordinates":row["coordinates"]} if row["coordinates"] else None}
                    for row in locations.values()]
        return {"type":"FeatureCollection", "features":features,
                "notice":"Saved provisional pilot areas take precedence over manual pins; remaining references are manual or explicitly unverified analytical fallbacks."}
    def read(path):
        with path.open(encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))
    def indexed(rows, label):
        result = {}
        for row in rows:
            zone_id = row["zone_id"].strip()
            if zone_id in result:
                raise ValueError(f"Duplicate zone_id in {label}")
            result[zone_id] = row
        return result
    def pair(lat, lon):
        if not str(lat or "").strip() or not str(lon or "").strip():
            return None
        latitude, longitude = float(lat), float(lon)
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("Invalid display coordinate range")
        return [longitude, latitude]
    official = indexed(read(dataset_path("official_zones")), "official registry")
    points = indexed(read(dataset_path("manual_points")), "manual points")
    statuses = indexed(read(dataset_path("manual_status")), "mapping status")
    previous = indexed(read(dataset_path("display_candidates")), "display candidates")
    if not (points.keys() | statuses.keys()) <= official.keys():
        raise ValueError("Manual mapping contains unknown official zone IDs")
    accepted_ids = {zone_id for zone_id, row in statuses.items() if row["manual_coordinate_status"] == "USER_PINNED_UNVERIFIED"}
    if accepted_ids != points.keys() or len(points) != 142 or len(statuses) != 146:
        raise ValueError("Manual mapping counts/statuses do not match the handoff")
    if any(official[zone_id]["zone_type"] != "FREE" for zone_id in statuses):
        raise ValueError("Manual checklist contains a non-FREE zone")
    for zone_id, row in points.items():
        status = statuses.get(zone_id, {})
        if official[zone_id]["zone_type"] != "FREE" or status.get("manual_coordinate_status") != "USER_PINNED_UNVERIFIED" or row.get("coordinate_status") != "USER_PINNED_UNVERIFIED" or row.get("pin_number") != status.get("pin_number"):
            raise ValueError("Manual point conflicts with mapping status")
        if pair(row["latitude"], row["longitude"]) != pair(status.get("manual_latitude"), status.get("manual_longitude")):
            raise ValueError("Manual point differs from status coordinates")
    features = []
    for zone_id, zone in official.items():
        manual, status, old = points.get(zone_id), statuses.get(zone_id), previous.get(zone_id, {})
        if manual:
            coordinates = pair(manual["latitude"], manual["longitude"])
            label, source = "USER_PINNED_UNVERIFIED", manual["coordinate_source"]
        elif status:
            coordinates = None
            label, source = status["manual_coordinate_status"], "Manual mapping status"
        else:
            coordinates = pair(old.get("display_latitude"), old.get("display_longitude")) if old.get("map_visibility") != "HIDE" else None
            label, source = old.get("display_status", "LOCATION_NOT_VERIFIED"), old.get("source_type", "")
        features.append({"type": "Feature", "properties": {"zone_id": zone_id, "division": zone["division"],
            "zone_type": zone["zone_type"], "location_status": label, "coordinate_source": source,
            "manual_pin_number": manual.get("pin_number") if manual else status.get("pin_number") if status else None,
            "official_boundary_verified": False, "model_features_recalculated": bool(CATALOG.get("active_model_version")), "model_version": CATALOG.get("active_model_version", "baseline"), "model_reference_source": ("MANUAL_PROVISIONAL" if manual else "ORIGINAL_ANALYTICAL_FALLBACK_UNRESOLVED" if status else "ORIGINAL_ANALYTICAL") if CATALOG.get("active_model_version") else "BASELINE_ANALYTICAL"},
            "geometry": {"type": "Point", "coordinates": coordinates} if coordinates else None})
    return {"type": "FeatureCollection", "features": features,
            "notice": "User-plotted points remain unverified. Scores use the manual-142-v1 recalculated GIS features; four unresolved manual zones retain explicitly labelled original analytical fallbacks."}


@router.get("/api/zones/display-geometry")
def zone_display_locations():
    try:
        return JSONResponse(display_zone_geometry())
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(503, f"Display coordinate data is unavailable: {exc}") from exc
