// Browser requests use the website's origin. Vite proxies /api to FastAPI,
// so LAN devices do not need access to the backend's localhost-only port.
// A deployment with a separate API may explicitly set VITE_API_BASE.
const LOCAL_API_BASE = typeof window === "undefined" ? "http://127.0.0.1:8000" : "";
const API_BASE = (import.meta.env?.VITE_API_BASE?.trim() || LOCAL_API_BASE).replace(/\/$/, "");

export async function apiRequest(path, options = {}) {
  const unavailable = () => {
    if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") window.dispatchEvent(new Event("vendormap:connection-lost"));
    const error = new Error("Unable to reach the service. Check that the backend is running and try again.");
    error.code = "NETWORK_UNAVAILABLE";
    return error;
  };
  if (typeof navigator !== "undefined" && navigator.onLine === false) throw unavailable();
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {credentials: "include", ...options, cache: "no-store"});
  } catch (cause) {
    if (cause?.name === "AbortError") throw cause;
    throw unavailable();
  }
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") window.dispatchEvent(new Event("vendormap:session-expired"));
  }
  if (response.status === 204) return null;
  let body;
  try {
    body = await response.json();
  } catch {
    const message = [502, 503, 504].includes(response.status)
      ? "The service is starting or temporarily unavailable. Wait a moment and try again."
      : response.status === 401 ? "Your session has expired. Please sign in again."
      : response.status === 403 ? "You do not have permission to perform this action."
      : `The service returned an unreadable response (${response.status}).`;
    const error = new Error(message); error.status = response.status; throw error;
  }
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "The request could not be completed.";
    const error = new Error(detail); error.status = response.status; throw error;
  }
  return body;
}

const jsonOptions = (method, body) => ({
  method,
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body),
});

export const getOptions = () => apiRequest("/api/options");
const pilotPath = (path, zoneId) => `${path}${zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : ""}`;
export const getPilotLayout = zoneId => apiRequest(pilotPath("/api/pilot/layout",zoneId));
export const previewPilotGeometry = (config, obstructions, zoneId) => apiRequest(pilotPath("/api/pilot/preview",zoneId), jsonOptions("POST", {config, obstructions}));
export const validatePilotGeometry = (layout, poses = {}, zoneId) => apiRequest(pilotPath("/api/pilot/validate",zoneId), jsonOptions("POST", {layout, poses}));
export const savePilotGeometry = (layout,zoneId) => apiRequest(pilotPath("/api/pilot/layout",zoneId), jsonOptions("PATCH", {layout, review_confirmed: true}));
export const getPilotDestination = zoneId => apiRequest(pilotPath("/api/pilot/navigation",zoneId));
export const getPilotRoute = p => apiRequest("/api/pilot/route", jsonOptions("POST", {longitude: p.longitude, latitude: p.latitude}));
export const initializePilotLayout = zoneId => apiRequest(pilotPath("/api/pilot/initialize",zoneId), {method: "POST"});
export const updatePilotSlot = (slotId, body, zoneId) =>
  apiRequest(pilotPath(`/api/pilot/slots/${encodeURIComponent(slotId)}`,zoneId), jsonOptions("PATCH", body));
export const createPilotSlot = (body, zoneId) =>
  apiRequest(pilotPath("/api/pilot/slots", zoneId), jsonOptions("POST", body));
export const deletePilotSlot = (slotId, expectedUpdatedAt, zoneId) =>
  apiRequest(`${pilotPath(`/api/pilot/slots/${encodeURIComponent(slotId)}`, zoneId)}&expected_updated_at=${encodeURIComponent(expectedUpdatedAt)}`, {method: "DELETE"});
export const getPilotEligibleVendors = (zoneId, q = "", page = 1) =>
  apiRequest(`/api/pilot/eligible-vendors?zone_id=${encodeURIComponent(zoneId)}&q=${encodeURIComponent(q)}&page=${page}`);
export const movePilotSlot = (slotId, body, zoneId) =>
  apiRequest(pilotPath(`/api/pilot/slots/${encodeURIComponent(slotId)}/geometry`, zoneId), jsonOptions("PATCH", body));
export const registerVendor = profile => apiRequest("/api/vendors", jsonOptions("POST", profile));
export const getVendorProfile = vendorId => apiRequest(`/api/vendors/${encodeURIComponent(vendorId)}`);
export const updateVendorProfile = (vendorId, updates) =>
  apiRequest(`/api/vendors/${encodeURIComponent(vendorId)}`, jsonOptions("PATCH", updates));
export const getActiveAllocation = vendorId =>
  apiRequest(`/api/vendors/${encodeURIComponent(vendorId)}/active-allocation`);
export const getVendorDashboard = () => apiRequest("/api/vendors/me/dashboard");
export const getVendorEligibleZones = () => apiRequest("/api/vendors/me/eligible-zones");
export const getRecommendations = (vendorId, excludeZone = "", preferences = {}) =>
  apiRequest("/api/recommendations", jsonOptions("POST", {
    vendor_id: vendorId,
    exclude_zone: excludeZone,
    business: preferences.business || "",
    division: preferences.division || "",
    top_n: 3,
  }));
export const getRecommendationAiExplanation = zoneId =>
  apiRequest(`/api/recommendations/${encodeURIComponent(zoneId)}/ai-explanation`);
export const createAllocationRequest = payload =>
  apiRequest("/api/allocation-requests", jsonOptions("POST", payload));
export const getAllocationRequest = requestId =>
  apiRequest(`/api/allocation-requests/${encodeURIComponent(requestId)}`);
export const cancelAllocationRequest = requestId =>
  apiRequest(`/api/allocation-requests/${encodeURIComponent(requestId)}/cancel`, {method: "POST"});
export const getZoneGeometry = signal => apiRequest("/api/zones/geometry", {signal});
export const requestVendorOtp = mobile =>
  apiRequest("/api/auth/vendor/request-otp", jsonOptions("POST", {mobile}));
// Confirm that the browser sends the session cookie back before entering a
// protected workspace. A 200 login response alone cannot establish this.
const establishSession = async (path, body) => {
  await apiRequest(path, jsonOptions("POST", body));
  try {return await apiRequest("/api/auth/me");}
  catch (error) {
    if (error.status === 401) error.message = "The browser could not retain your session. Enable cookies and try again.";
    throw error;
  }
};
export const verifyVendorOtp = (challengeId, otp) =>
  establishSession("/api/auth/vendor/verify-otp", {challenge_id: challengeId, otp});
export const loginAdmin = (username, password) =>
  establishSession("/api/auth/admin/login", {username, password});
let currentUserRequest;
export const getCurrentUser = () => {
  // Share only an in-flight check (including StrictMode's development mount).
  // Never cache an identity across logout, reconnect or subsequent refreshes.
  if (!currentUserRequest) currentUserRequest = apiRequest("/api/auth/me").finally(() => {currentUserRequest = null;});
  return currentUserRequest;
};
export const logout = () => apiRequest("/api/auth/logout", {method: "POST"});
export const getAdminOverview = signal => apiRequest("/api/admin/overview", {signal});
export const getAllocationRequests = (status = "", signal) =>
  apiRequest(`/api/allocation-requests${status ? `?status=${encodeURIComponent(status)}` : ""}`, {signal});
export const approveAllocationRequest = (requestId, notes = null) =>
  apiRequest(`/api/allocation-requests/${encodeURIComponent(requestId)}/approve`,
    jsonOptions("POST", {notes}));
export const rejectAllocationRequest = (requestId, rejectionReason, notes = null) =>
  apiRequest(`/api/allocation-requests/${encodeURIComponent(requestId)}/reject`,
    jsonOptions("POST", {rejection_reason: rejectionReason, notes}));
export const getAllocations = (status = "", signal, {limit, offset} = {}) => {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (limit !== undefined) params.set("limit", String(limit));
  if (offset !== undefined) params.set("offset", String(offset));
  const serialized = params.toString();
  const query = serialized ? `?${serialized}` : "";
  return apiRequest(`/api/allocations${query}`, {signal});
};
export const getZoneLiveStatus = zoneId =>
  apiRequest(`/api/zones/${encodeURIComponent(zoneId)}/live-status`);
export const updateZoneLiveStatus = (zoneId, status) =>
  apiRequest(`/api/zones/${encodeURIComponent(zoneId)}/live-status`,
    jsonOptions("PATCH", {status}));
export const getZoneCatalog = (filters, signal) => {
  const params = new URLSearchParams();
  Object.entries(filters || {}).forEach(([key, value]) => value && params.set(key, value));
  const query = params.toString();
  return apiRequest(`/api/admin/zones${query ? `?${query}` : ""}`, {signal});
};

export const getAdminVendors = (filters = {}, signal) => {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => value !== "" && value != null && params.set(key, value));
  return apiRequest(`/api/admin/vendors?${params}`, {signal});
};

export const getAdminVendorDetails = (vendorId, signal) => apiRequest(`/api/admin/vendors/${encodeURIComponent(vendorId)}/details`, {signal});
export const getVendorRelocationRecommendations = (vendorId, includeAll = false) =>
  apiRequest(`/api/admin/vendors/${encodeURIComponent(vendorId)}/relocation-recommendations?include_all=${includeAll}`);
export const getZoneHistoricalVendors = zoneId =>
  apiRequest(`/api/admin/zones/${encodeURIComponent(zoneId)}/historical-vendors`);
export const getCapacityPressure = () => apiRequest("/api/admin/capacity-pressure");

export const getVendorAccountSettings = signal => apiRequest("/api/vendors/me/account-settings", {signal});
export const getVendorProfilePhoto = signal => apiRequest("/api/vendors/me/profile-photo", {signal});
export const saveVendorProfilePhoto = dataUrl =>
  apiRequest("/api/vendors/me/profile-photo", jsonOptions("PUT", {data_url: dataUrl}));

export const getVendorActivity = signal => apiRequest("/api/vendors/me/activity", {signal});

export const getZoneDisplayGeometry = () => apiRequest("/api/zones/display-geometry");
