
CREATE TABLE IF NOT EXISTS operational_vendors (
    vendor_id TEXT PRIMARY KEY,
    application_vendor_id TEXT NULL,
    vendor_type TEXT NOT NULL CHECK (vendor_type IN ('NEW', 'EXISTING_IMPORTED')),
    full_name TEXT NULL,
    business_name TEXT NULL,
    business_category TEXT NOT NULL,
    preferred_division TEXT NULL,
    preferred_locality TEXT NULL,
    priority_profile TEXT NOT NULL DEFAULT 'BALANCED'
        CHECK (priority_profile IN ('BALANCED', 'COMMERCIAL', 'ACCESSIBILITY', 'FACILITIES')),
    parking_preference INTEGER NOT NULL DEFAULT 0 CHECK (parking_preference IN (0, 1)),
    transport_preference INTEGER NOT NULL DEFAULT 0 CHECK (transport_preference IN (0, 1)),
    market_preference INTEGER NOT NULL DEFAULT 0 CHECK (market_preference IN (0, 1)),
    preferred_language TEXT NOT NULL DEFAULT 'en'
        CHECK (preferred_language IN ('en', 'mr', 'hi')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_vendor_id_counters (
    year INTEGER PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0)
);

CREATE TABLE IF NOT EXISTS zone_live_status (
    zone_id TEXT PRIMARY KEY,
    official_capacity INTEGER NULL CHECK (official_capacity IS NULL OR official_capacity >= 0),
    current_vendor_count INTEGER NOT NULL DEFAULT 0 CHECK (current_vendor_count >= 0),
    available_capacity INTEGER NULL CHECK (available_capacity IS NULL OR available_capacity >= 0),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'FULL', 'CLOSED', 'SUSPENDED', 'RESTRICTED', 'NO_VENDING')),
    verified_at TEXT NULL,
    verified_by TEXT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (official_capacity IS NULL AND available_capacity IS NULL)
        OR
        (official_capacity IS NOT NULL
         AND available_capacity = CASE
             WHEN current_vendor_count >= official_capacity THEN 0
             ELSE official_capacity - current_vendor_count
         END)
    ),
    CHECK (official_capacity IS NULL OR current_vendor_count < official_capacity
           OR status IN ('FULL', 'CLOSED', 'SUSPENDED', 'RESTRICTED', 'NO_VENDING'))
);

CREATE TABLE IF NOT EXISTS allocation_requests (
    request_id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    slot_id TEXT NULL,
    request_type TEXT NOT NULL CHECK (request_type IN ('NEW_ALLOCATION', 'RELOCATION')),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
    business_category TEXT NULL,
    preferred_division TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT NULL,
    reviewed_by TEXT NULL,
    rejection_reason TEXT NULL,
    notes TEXT NULL,
    FOREIGN KEY (zone_id) REFERENCES zone_live_status(zone_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_request_per_vendor_zone
    ON allocation_requests(vendor_id, zone_id) WHERE status = 'PENDING';

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_request_per_slot
    ON allocation_requests(zone_id, slot_id)
    WHERE status = 'PENDING' AND slot_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    slot_id TEXT NULL,
    request_id TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED')),
    allocated_at TEXT NOT NULL,
    released_at TEXT NULL,
    approved_by TEXT NULL,
    allocation_source TEXT NOT NULL DEFAULT 'ADMIN_APPROVED'
        CHECK (allocation_source IN ('ADMIN_APPROVED', 'LEGACY_IMPORT')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (zone_id) REFERENCES zone_live_status(zone_id),
    FOREIGN KEY (request_id) REFERENCES allocation_requests(request_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_allocation_per_vendor
    ON allocations(vendor_id) WHERE status = 'ACTIVE';

CREATE UNIQUE INDEX IF NOT EXISTS one_active_allocation_per_slot
    ON allocations(zone_id, slot_id)
    WHERE status = 'ACTIVE' AND slot_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS admin_audit_log (
    audit_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    performed_by TEXT NULL,
    details_json TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendor_auth_identities (
    identity_id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL UNIQUE,
    mobile_last4 TEXT NULL,
    vendor_source TEXT NOT NULL CHECK (vendor_source IN ('HISTORICAL', 'OPERATIONAL')),
    is_verified INTEGER NOT NULL DEFAULT 0 CHECK (is_verified IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vendor_auth_identities_vendor_id
    ON vendor_auth_identities(vendor_id);

CREATE TABLE IF NOT EXISTS auth_otp_challenges (
    challenge_id TEXT PRIMARY KEY,
    phone_hash TEXT NOT NULL,
    mobile_last4 TEXT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('VENDOR_LOGIN', 'NEW_VENDOR_REGISTRATION')),
    otp_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    consumed_at TEXT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_otp_phone_created
    ON auth_otp_challenges(phone_hash, created_at);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    session_token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('VENDOR', 'ADMIN', 'NEW_VENDOR_ONBOARDING')),
    vendor_id TEXT NULL,
    admin_id TEXT NULL,
    new_vendor_phone_hash TEXT NULL,
    mobile_last4 TEXT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NULL,
    CHECK (
        (role = 'VENDOR' AND vendor_id IS NOT NULL AND admin_id IS NULL AND new_vendor_phone_hash IS NULL)
        OR (role = 'ADMIN' AND admin_id IS NOT NULL AND vendor_id IS NULL AND new_vendor_phone_hash IS NULL)
        OR (role = 'NEW_VENDOR_ONBOARDING' AND new_vendor_phone_hash IS NOT NULL AND vendor_id IS NULL AND admin_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_token_active
    ON auth_sessions(session_token_hash, revoked_at, expires_at);
CREATE TABLE IF NOT EXISTS vendor_profile_photos (
    vendor_id TEXT PRIMARY KEY,
    mime_type TEXT NOT NULL,
    image_base64 TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS whatsapp_conversations (
    conversation_key TEXT PRIMARY KEY,
    phone_hash TEXT NOT NULL UNIQUE,
    vendor_id TEXT NULL,
    language TEXT NOT NULL DEFAULT 'en'
        CHECK (language IN ('en', 'mr', 'hi')),
    flow TEXT NOT NULL DEFAULT 'START',
    step TEXT NOT NULL DEFAULT 'START',
    state_json TEXT NOT NULL DEFAULT '{}',
    invalid_count INTEGER NOT NULL DEFAULT 0 CHECK (invalid_count >= 0),
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_conversations_vendor
    ON whatsapp_conversations(vendor_id);

CREATE TABLE IF NOT EXISTS whatsapp_processed_messages (
    message_id TEXT PRIMARY KEY,
    phone_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PROCESSING'
        CHECK (status IN ('PROCESSING', 'PROCESSED', 'IGNORED', 'FAILED')),
    received_at TEXT NOT NULL,
    processed_at TEXT NULL,
    error_code TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_processed_phone_received
    ON whatsapp_processed_messages(phone_hash, received_at);


CREATE TABLE IF NOT EXISTS pilot_slot_layouts (
    zone_id TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_slots (
    zone_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    width_m REAL NOT NULL CHECK(width_m > 0),
    depth_m REAL NOT NULL CHECK(depth_m > 0),
    x_m REAL NOT NULL,
    y_m REAL NOT NULL,
    geometry_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('AVAILABLE','RESERVED','OCCUPIED','BLOCKED')),
    assigned_vendor_id TEXT,
    pending_request_id TEXT NULL,
    active_allocation_id TEXT NULL,
    created_by TEXT NULL,
    created_at TEXT NULL,
    validation_state TEXT NOT NULL CHECK(validation_state = 'ILLUSTRATIVE_UNVERIFIED'),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(zone_id, slot_id),
    FOREIGN KEY(zone_id) REFERENCES pilot_slot_layouts(zone_id),
    CHECK(assigned_vendor_id IS NULL OR status IN ('RESERVED','OCCUPIED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_demo_slot_per_vendor
    ON pilot_slots(assigned_vendor_id) WHERE assigned_vendor_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS pilot_slot_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(zone_id, slot_id) REFERENCES pilot_slots(zone_id, slot_id)
);
