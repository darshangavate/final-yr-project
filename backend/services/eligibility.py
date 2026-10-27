"""Official type and operational availability, independent of map/model data."""


def zone_ineligibility_reason(zone_type, live):
    if zone_type != "FREE":
        return "OFFICIAL_ZONE_NOT_FREE"
    if live is None:
        return "OPERATIONAL_STATUS_UNAVAILABLE"
    if live["status"] != "OPEN":
        return "ZONE_" + live["status"]
    available = live["available_capacity"]
    if available is None:
        return "CAPACITY_UNKNOWN"
    if available <= 0:
        return "NO_AVAILABLE_CAPACITY"
    return None
