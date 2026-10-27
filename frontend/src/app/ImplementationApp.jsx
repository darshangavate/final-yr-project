import VendorActivity from "../features/vendor/VendorActivity.jsx";
import {useDialogFocus} from "../shared/utils/dialogFocus.js";
import React, {useCallback, useEffect, useState} from "react";
import {Bell, Building2, CircleHelp, FileText, LayoutDashboard, LogOut, MapPin, ShieldCheck, UserRound, X} from "lucide-react";
const VendorDirectory = React.lazy(() => import("../features/admin/VendorWorkspace.jsx"));
const AdminDashboard = React.lazy(() => import("../features/admin/AdminDashboard.jsx"));
const AuthLanding = React.lazy(() => import("../features/auth/AuthLanding.jsx"));
const VendorPortal = React.lazy(() => import("../features/vendor/VendorPortal.jsx"));
import BrandIdentity from "../shared/ui/BrandIdentity.jsx";
import {getCurrentUser, logout} from "../services/api.js";
import {useI18n} from "../i18n/react.jsx";





const ADMIN_NAV = [
  {id: "admin", title: "Overview", icon: LayoutDashboard},
  {id: "vendors", title: "Vendors", icon: UserRound},
  {id: "zones", title: "Zones & maps", icon: MapPin},
  {id: "requests", title: "Applications", icon: FileText},
  {id: "map", title: "Vending slots", icon: Building2},
  {id: "allocations", title: "Allocations", icon: ShieldCheck},
];
const VENDOR_NAV = [
  {id: "vendor-dashboard", titleKey: "nav.dashboard", icon: LayoutDashboard},
  {id: "vendor-recommendations", titleKey: "nav.recommendations", icon: MapPin},
  {id: "vendor-requests", titleKey: "nav.requests", icon: FileText},
  {id: "vendor-help", titleKey: "help.title", icon: CircleHelp},
  {id: "vendor-profile", titleKey: "nav.profile", icon: UserRound},
];
const VENDOR_PATHS = {
  "vendor-dashboard": "/vendor",
  "vendor-recommendations": "/vendor/recommendations",
  "vendor-requests": "/vendor/requests",
  "vendor-profile": "/vendor/profile",
  "vendor-settings": "/vendor/settings",
  "vendor-notifications": "/vendor/notifications",
  "vendor-help": "/vendor/help",
};

export default function ImplementationApp() {
  const {t, applyProfileLanguage} = useI18n();
  const [session, setSession] = useState(null);
  const [logoutError, setLogoutError] = useState("");
  const [checking, setChecking] = useState(true);
  const [sessionError, setSessionError] = useState("");
  const [sessionAttempt, setSessionAttempt] = useState(0);
  const [page, setPage] = useState("vendor-dashboard");
  const [recommendationZoneId, setRecommendationZoneId] = useState("");
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [focusedRequestId, setFocusedRequestId] = useState(null);
  const [openOwnSlot, setOpenOwnSlot] = useState(0);
  useEffect(() => {window.scrollTo({top: 0, behavior: "auto"});}, [page, recommendationZoneId]);
  useDialogFocus(notificationsOpen, ".vendor-notification-drawer");
  useEffect(() => {
    if (!notificationsOpen) return;
    const close = event => {if (event.key === "Escape") setNotificationsOpen(false);};
    document.addEventListener("keydown", close); return () => document.removeEventListener("keydown", close);
  }, [notificationsOpen]);
  const [slotTarget, setSlotTarget] = useState(null);


  const routeFromPath = next => {
    if (next?.role === "ADMIN") {
      const id = window.location.pathname.split("/")[2] || "admin";
      return {page: ADMIN_NAV.some(item => item.id === id) ? id : "admin"};
    }
    if (next?.role !== "VENDOR") return null;
    const match = window.location.pathname.match(/^\/vendor\/recommendations\/([^/]+)\/?$/);
    if (match) return {page: "vendor-recommendation-detail", zoneId: decodeURIComponent(match[1])};
    const vendorPage = Object.entries(VENDOR_PATHS).find(([, path]) => window.location.pathname === path)?.[0];
    if (vendorPage) return {page: vendorPage, zoneId: "", requestId: vendorPage === "vendor-requests" ? new URLSearchParams(window.location.search).get("request") : null};
    return null;
  };
  const applySession = (next, {preserveRoute = false} = {}) => {
    if (next?.role === "VENDOR") applyProfileLanguage(next.vendor_profile?.preferred_language);
    setSession(next);
    const admin = next?.role === "ADMIN";
    const preserved = preserveRoute ? routeFromPath(next) : null;
    if (preserved) {
      setPage(preserved.page); setRecommendationZoneId(preserved.zoneId || ""); setFocusedRequestId(preserved.requestId || null);
      return;
    }
    const nextPage = admin ? "admin" : "vendor-dashboard";
    setPage(nextPage); setRecommendationZoneId("");
    window.history.replaceState({}, "", admin ? "/admin" : "/vendor");
  };
  useEffect(() => {
    let active = true;
    setChecking(true); setSessionError("");
    getCurrentUser().then(next => {if (active) applySession(next, {preserveRoute: true});}).catch(error => {
      if (!active) return;
      if (error.code === "NETWORK_UNAVAILABLE") return;
      if (error.status === 401) {setSession(null); window.history.replaceState({}, "", "/login");}
      else setSessionError(error.message);
    }).finally(() => {if (active) setChecking(false);});
    return () => {active = false;};
  }, [sessionAttempt]);
  useEffect(() => {
    const expired = () => {
      setSession(null); setSessionError(""); setNotificationsOpen(false);
      window.history.replaceState({}, "", "/login");
    };
    window.addEventListener("vendormap:session-expired", expired);
    return () => window.removeEventListener("vendormap:session-expired", expired);
  }, []);
  useEffect(() => {
    const handlePopState = () => {
      if (!session) return;
      const route = routeFromPath(session);
      if (route) {
        setPage(route.page); setRecommendationZoneId(route.zoneId || ""); setFocusedRequestId(route.requestId || null); setNotificationsOpen(false);
      } else {
        const vendorPage = VENDOR_NAV.find(item => window.location.pathname.endsWith(`/${item.id}`))?.id || "vendor-dashboard";
        setPage(vendorPage); setRecommendationZoneId("");
      }
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [session]);

  const updateVendorProfile = useCallback(profile => setSession(current => current?.role === "VENDOR" && current.vendor_id === profile.vendor_id ? {...current, vendor_profile: profile} : current), []);
  const signOut = async () => {
    setLogoutError("");
    try {await logout();} catch (err) {setLogoutError(err.message); return;}
    setSession(null); setPage("vendor-dashboard"); setRecommendationZoneId(""); window.history.replaceState({}, "", "/login");
  };
  const refreshSession = async () => applySession(await getCurrentUser());
  const navigate = id => {
    const allowed = (session?.role === "ADMIN" ? ADMIN_NAV : VENDOR_NAV).some(item => item.id === id) || (session?.role === "VENDOR" && id === "vendor-settings");
    const safe = allowed ? id : session?.role === "ADMIN" ? "admin" : "vendor-dashboard";
    setPage(safe); setRecommendationZoneId(""); setNotificationsOpen(false);
    if (safe !== "vendor-requests") setFocusedRequestId(null);
    const path = session?.role === "ADMIN" ? `/admin/${safe}` : VENDOR_PATHS[safe] || "/vendor";
    if (window.location.pathname !== path) window.history.pushState({}, "", path);
  };
  const openRecommendation = zoneId => {
    setRecommendationZoneId(zoneId); setPage("vendor-recommendation-detail");
    window.history.pushState({}, "", `/vendor/recommendations/${encodeURIComponent(zoneId)}`);
  };
  const backToRecommendations = () => {
    setRecommendationZoneId(""); setPage("vendor-recommendations");
    window.history.replaceState({}, "", VENDOR_PATHS["vendor-recommendations"]);
  };
  if (checking) return <div className="auth-loading"><Building2 size={28}/><span>{t("common.loading")}</span></div>;
  if (sessionError) return <main className="auth-loading"><p role="alert">{sessionError}</p><button onClick={() => setSessionAttempt(value => value + 1)}>{t("pwa.retry")}</button></main>;
  if (!session) return <AuthLanding onAuthenticated={applySession}/>;

  const onboarding = session.role === "NEW_VENDOR_ONBOARDING";
  if (onboarding) return <VendorPortal onboarding onRegistered={refreshSession}/>;

  const nav = session.role === "ADMIN" ? ADMIN_NAV : VENDOR_NAV;
  const navTitle = item => session.role === "ADMIN" ? item.title : t(item.titleKey);
  return <div className={`implementation-shell service-app vendormap ${session.role === "ADMIN" ? "officer-app" : "vendor-app"}`}>
    <a className="skip-link" href="#main-content">{t("vm.skip")}</a>
    <header className="vm-header">
      <BrandIdentity compact subtitle={session.role === "ADMIN" ? "NMC · Officer workspace" : "Nashik"}/>
      <nav className="vm-nav" aria-label={t("portal.main")}>{nav.filter(item => item.id !== "vendor-help").map(item => {
        const active = page === item.id || (item.id === "vendor-recommendations" && page === "vendor-recommendation-detail") || (item.id === "vendor-profile" && page === "vendor-settings");
        return <button key={item.id} aria-current={active ? "page" : undefined} className={active ? "active" : ""} onClick={() => navigate(item.id)}><item.icon size={18}/><span>{navTitle(item)}</span></button>;
      })}</nav>
      <div className="vm-utilities">
        {session.role === "VENDOR" && <><button aria-label={t("help.title")} title={t("help.title")} onClick={() => navigate("vendor-help")}><CircleHelp size={20}/></button><button aria-label={t("activity.title")} aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(value => !value)}><Bell size={20}/></button></>}
        <button aria-label={t("nav.logout")} title={t("nav.logout")} onClick={signOut}><LogOut size={19}/></button>
      </div>
    </header>
    <div className="implementation-content"><div className="implementation-view" id="main-content" tabIndex={-1}>
    {logoutError && <p role="alert" className="vendor-action-notice">{logoutError}</p>}
    {(session.role === "VENDOR" || onboarding) && <VendorPortal onProfileUpdated={updateVendorProfile} focusRequestId={focusedRequestId} openOwnSlot={openOwnSlot} sessionProfile={session.vendor_profile} onboarding={onboarding} view={page} detailZoneId={recommendationZoneId} onNavigate={navigate} onOpenRecommendation={openRecommendation} onBackRecommendations={backToRecommendations} onRegistered={refreshSession}/>}
    {session.role === "ADMIN" && page !== "vendors" && <AdminDashboard adminId={session.admin_id} slotTarget={slotTarget} section={page === "admin" ? "overview" : page} onSectionChange={id => navigate(id === "overview" ? "admin" : id)}/>}
    {session.role === "ADMIN" && page === "vendors" && <VendorDirectory onViewSlot={slot => {setSlotTarget({...slot}); navigate("map");}}/>}
  </div></div>
  {session.role === "VENDOR" && notificationsOpen && <div className="vendor-notification-backdrop" onClick={() => setNotificationsOpen(false)}><aside id="vendor-notifications-dialog" className="vendor-notification-drawer" role="dialog" aria-modal="true" aria-labelledby="vendor-notification-title" onClick={event => event.stopPropagation()}><button className="vendor-notification-close" aria-label={t("nav.close")} onClick={() => setNotificationsOpen(false)}><X size={18}/></button><VendorActivity compact onApplication={id => {navigate("vendor-requests"); setFocusedRequestId(id); window.history.replaceState({}, "", `/vendor/requests?request=${encodeURIComponent(id)}`);}} onSlot={() => {navigate("vendor-dashboard"); setOpenOwnSlot(value => value + 1);}}/></aside></div>}
  </div>;
}

import "../styles/foundation.css";
import "../styles/vendormap.css";
