import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import {readFileSync} from "node:fs";

const origin = "https://vendormap.test";
const js = "/assets/index-12345678.js", offline = "/assets/offline-12345678.html";
const template = readFileSync(new URL("./sw.js", import.meta.url), "utf8").replace("__VM_VERSION__", '"test"').replace("__VM_ASSETS__", JSON.stringify([js, offline])).replace("__VM_OFFLINE__", JSON.stringify(offline));
function worker({windows = [], fail = false} = {}) {
  const handlers = {}, stores = new Map(), network = [];
  let skipped = 0;
  const caches = {
    keys: async () => [...stores.keys()], delete: async key => stores.delete(key),
    open: async name => {if (!stores.has(name)) stores.set(name, new Map()); const store = stores.get(name); return {put: async (key, response) => store.set(new URL(typeof key === "string" ? key : key.url, origin).pathname, response), match: async key => store.get(new URL(typeof key === "string" ? key : key.url, origin).pathname)?.clone()};},
  };
  const fetch = async (request, options = {}) => {
    network.push({request, options});
    if (fail) throw new TypeError("offline");
    const pathname = new URL(typeof request === "string" ? request : request.url, origin).pathname;
    return new Response(pathname.startsWith("/api") ? '{"private":"live response"}' : pathname === offline ? "Connection needed" : "static code", {headers:{"content-type":pathname.endsWith(".js") ? "text/javascript" : pathname.endsWith(".html") ? "text/html" : "application/json"}});
  };
  class LocalRequest extends Request {constructor(input, options) {super(new URL(input, origin), options);}}
  const registration={active:{state:"activated"},waiting:null,installing:null};
  vm.runInNewContext(template, {self:{registration,location:{origin}, clients:{matchAll:async()=>windows}, addEventListener:(name, handler)=>handlers[name]=handler, skipWaiting:async()=>{skipped++;}}, caches, fetch, Request:LocalRequest, Response, URL});
  const event = (name, values = {}) => {let promise; handlers[name]({...values, waitUntil:value=>promise=value, respondWith:value=>promise=value}); return promise;};
  const request = (pathname, values = {}) => event("fetch", {request:{url:new URL(pathname, origin).href, method:"GET", mode:"cors", headers:new Headers(), ...values}});
  return {stores, network, caches, event, request, registration, skipped:()=>skipped};
}

test("precache contains only explicit versioned assets, fetched without cookies", async () => {
  const sw = worker();await sw.event("install");
  assert.deepEqual([...sw.stores.get("vendormap-static-test").keys()], [js, offline]);
  for (const {request} of sw.network) {assert.equal(request.credentials,"omit");assert.equal(request.cache,"no-store");}
  assert.equal(sw.skipped(),0,"installation must not activate an update during editing");
});
test("authenticated API reads and writes always go to the network and never enter caches", async () => {
  const sw=worker();await sw.event("install");
  for (const url of ["/api/auth/me","/api/vendors/me/profile-photo","/api/allocation-requests?status=ACTIVE","https://api.other.test/api/vendors/me/dashboard"]) {
    assert.equal(sw.request(url),undefined,"API requests must bypass the worker");
  }
  assert.equal(sw.request("/api/allocation-requests",{method:"POST"}),undefined);
  assert.equal(sw.network.length,2,"only precached assets may initiate worker fetches");
  assert.equal(sw.stores.get("vendormap-static-test").size,2);
});
test("offline navigation gets a generic static page rather than a cached private screen", async () => {
  const sw=worker({fail:true});await (await sw.caches.open("vendormap-static-test")).put(offline,new Response("Connection needed"));
  const response=await sw.request("/vendor/profile?request=private",{mode:"navigate"});
  assert.equal(await response.text(),"Connection needed");assert.deepEqual([...sw.stores.get("vendormap-static-test").keys()],[offline]);
  assert.equal(sw.request("/api/vendors/me/dashboard"),undefined);
});
test("unknown assets, map tiles, profile images and authorized requests are not cached", async () => {
  const sw=worker();await sw.event("install");
  for (const path of ["/icons/vendormap-192.png","https://tile.openstreetmap.org/1/2/3.png","/vendor/photo.png"]) assert.equal(sw.request(path),undefined);
  assert.equal(sw.request(js,{headers:new Headers({Authorization:"Bearer example"})}),undefined);
  await sw.request("/assets/unknown-87654321.js");assert.equal(sw.stores.get("vendormap-static-test").size,2);
});
test("updates retain old immutable chunks for open windows, and activation needs an explicit same-origin message", async () => {
  const sw=worker({windows:[{id:"editing-window"}]});const old=await sw.caches.open("vendormap-static-old");await old.put("/assets/old-87654321.js",new Response("old immutable code"));
  await sw.event("install");await sw.event("activate");assert.ok(sw.stores.has("vendormap-static-old"));
  assert.equal(await (await sw.request("/assets/old-87654321.js")).text(),"old immutable code");
  await sw.event("message",{data:{type:"ACTIVATE_UPDATE"},source:{url:origin+"/vendor"}});assert.equal(sw.skipped(),1);
  await sw.event("message",{data:{type:"ACTIVATE_UPDATE"},source:{url:"https://other.test"}});assert.equal(sw.skipped(),1);
});
test("safe activation cleans only this app's obsolete caches; failed installs leave no partial cache", async () => {
  const sw=worker();await sw.caches.open("another-app");await sw.caches.open("vendormap-static-old");await sw.event("install");await sw.event("activate");
  assert.ok(sw.stores.has("another-app"));assert.ok(!sw.stores.has("vendormap-static-old"));
  const failed=worker({fail:true});await assert.rejects(failed.event("install"));assert.equal(failed.stores.size,0);
});

test("only a sole current-build window may clean old caches, never a pending build or older client", async () => {
  const sw=worker({windows:[{id:"current"}]});await sw.event("install");await sw.caches.open("vendormap-static-old");
  const source={id:"current",url:origin+"/vendor"};
  await sw.event("message",{source,data:{type:"CLIENT_READY",entry:"/assets/old-87654321.js"}});assert.ok(sw.stores.has("vendormap-static-old"));
  sw.registration.waiting={state:"installed"};
  await sw.event("message",{source,data:{type:"CLIENT_READY",entry:js}});assert.ok(sw.stores.has("vendormap-static-old"));
  sw.registration.waiting=null;
  await sw.event("message",{source,data:{type:"CLIENT_READY",entry:js}});assert.ok(!sw.stores.has("vendormap-static-old"));
});
