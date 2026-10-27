"""Translate unavailable saved GIS references into a safe HTTP service error."""
import logging

from fastapi import HTTPException

from backend.geo.zone_locations import resolve_zone_locations as resolve_saved_locations

logger = logging.getLogger(__name__)


def resolve_zone_locations(database_path):
    try:
        return resolve_saved_locations(database_path)
    except (OSError, ValueError) as exc:
        # Never generate replacement geometry or expose paths/SQL in responses.
        logger.error("Saved zone reference data is unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Saved zone reference data is unavailable. Contact the administrator.") from exc
