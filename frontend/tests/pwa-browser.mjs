import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {readFile, writeFile, mkdir} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {preview} from "vite";
import {launchBrowser, delay} from "../tools/browser.mjs";

const frontend=path.resolve(path.dirname(fileURLToPath(import.meta.url)),"..");
const root=path.dirname(frontend), origin="http://127.0.0.1:5196";
const swPath=path.join(frontend,"dist/sw.js");
const originalWorker=await readFile(swPath,"utf8");
const backend=spawn(path.join(root,".venv/Scripts/python.exe"),["-X","utf8",path.join(frontend,"tests/vendormap-server.py")],{cwd:root,windowsHide:true,stdio:["ignore","pipe","pipe"]});
let backendLog="";backend.stdout.on("data",data=>backendLog+=data);backend.stderr.on("data",data=>backendLog+=data);
let server,browser;
const report={checks:[],errors:[]};
try {
  for(let i=0;i<150;i++){try{if((await fetch("http://127.0.0.1:8093/api/health")).ok)break;}catch{}if(i===149)throw new Error(backendLog);await delay(300);}
  server=await preview({root:frontend,preview:{host:"127.0.0.1",port:5196,strictPort:true,proxy:{"/api":{target:"http://127.0.0.1:8093",changeOrigin:true}}}});
  browser=await launchBrowser();const {command:c,evaluate:ev,waitFor:wait}=browser;
  const go=async route=>{await c("Page.navigate",{url:origin+route});await wait("document.readyState==='complete'");};
  const api=async(route,body)=>ev(`(async()=>{const r=await fetch(${JSON.stringify(route)},{method:${JSON.stringify(body?"POST":"GET")},credentials:'include',cache:'no-store',headers:{'Content-Type':'application/json'},${body?"body:"+JSON.stringify(JSON.stringify(body)):""}});return {status:r.status,body:await r.json()}})()`);
  const size=width=>c("Emulation.setDeviceMetricsOverride",{width,height:850,deviceScaleFactor:1,mobile:width<768});
  const cacheUrls=()=>ev("(async()=>{const all=[];for(const name of await caches.keys()){if(name.startsWith('vendormap-static-'))for(const req of await(await caches.open(name)).keys())all.push(req.url);}return all})()");
  await c("Network.enable");await size(390);await go("/");await wait("!!document.querySelector('.auth-card')");
  await wait("'serviceWorker' in navigator");
  await ev("navigator.serviceWorker.ready.then(()=>true)");await go("/");
  await wait("!!navigator.serviceWorker.controller && !!document.querySelector('.auth-card')");
  report.checks.push("Production service worker registers and controls the app on a secure loopback origin");
  const manifest=await c("Page.getAppManifest");assert.equal(manifest.errors.length,0,JSON.stringify(manifest.errors));
  const metadata=JSON.parse(manifest.data);assert.equal(metadata.name,"VendorMap");assert.equal(metadata.display,"standalone");assert.equal(metadata.start_url,"/");
  for(const icon of [...metadata.icons,{src:"/icons/vendormap-apple-180.png",sizes:"180x180"}]) {
    const dimensions=await ev(`(async()=>{const r=await fetch(${JSON.stringify(icon.src)});if(!r.ok)throw Error('Missing icon');const b=await createImageBitmap(await r.blob());return b.width+'x'+b.height})()`);
    assert.equal(dimensions,icon.sizes);
  }
  const installability=await c("Page.getInstallabilityErrors");assert.deepEqual(installability.installabilityErrors,[]);
  report.checks.push("Chromium installability criteria pass; Android, maskable and Apple PNG icon dimensions verified");
  // Browser-native installation UI cannot be clicked in headless mode. Exercise its event contract explicitly.
  await ev("window.__nativeInstallCalls=0;const e=new Event('beforeinstallprompt',{cancelable:true});e.prompt=async()=>{window.__nativeInstallCalls++};e.userChoice=Promise.resolve({outcome:'accepted'});window.dispatchEvent(e);true");
  await wait("!!document.querySelector('.pwa-action')");await ev("document.querySelector('.pwa-action').click()");
  assert.equal(await ev("window.__nativeInstallCalls"),1);await ev("window.dispatchEvent(new Event('appinstalled'));true");
  await wait("!document.querySelector('.pwa-strip')");report.checks.push("Supported-browser install event is deferred until click; installed event hides the prompt (synthetic events)");
  await c("Network.setUserAgentOverride",{userAgent:"Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"});
  await go("/");await wait("!!document.querySelector('.pwa-action')");await ev("document.querySelector('.pwa-action').click()");await wait("!!document.querySelector('.pwa-ios-guide')");
  for(const width of [320,390,768,1440]){await size(width);assert.equal(await ev("document.documentElement.scrollWidth<=innerWidth"),true);}
  await ev("document.querySelector('.pwa-dismiss').click()");await go("/");await wait("!!document.querySelector('.auth-card')");assert.equal(await ev("!!document.querySelector('.pwa-strip')"),false);
  report.checks.push("iPhone guidance and session dismissal work at mobile and desktop widths (emulated user agent)");
  await c("Network.setUserAgentOverride",{userAgent:"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"});
  await go("/");
  const challenge=await api("/api/auth/vendor/request-otp",{mobile:"9"+String(Date.now()).slice(-9)});assert.equal(challenge.status,200);
  assert.equal((await api("/api/auth/vendor/verify-otp",{challenge_id:challenge.body.challenge_id,otp:"123456"})).status,200);
  const created=await api("/api/vendors",{full_name:"PWA Private Test Vendor",business_name:"Private PWA Stall",business_category:"food",preferred_language:"en",priority_profile:"BALANCED",market_preference:false,transport_preference:false,parking_preference:false});assert.equal(created.status,201);
  const vendor=created.body;
  await go("/vendor/profile");await wait("!!document.querySelector('#vendor-profile-edit')");
  assert.equal(await ev("document.cookie.includes('nmc_session')"),false);
  const cookieBefore=(await c("Network.getCookies",{urls:[origin]})).cookies.find(cookie=>cookie.name==="nmc_session");assert.equal(cookieBefore.httpOnly,true);
  const languageBefore=await ev("localStorage.getItem('nmc_vendor_language')");
  const urls=await cacheUrls();assert.ok(urls.length>5);for(const url of urls)assert.match(url,/\/assets\/[^/]+-[A-Za-z0-9_-]{8,}\.(js|css|svg|png|webp|woff2|html)$/);
  assert.equal(await ev(`(async()=>{for(const name of await caches.keys()){if(!name.startsWith('vendormap-static-'))continue;const cache=await caches.open(name);for(const key of await cache.keys()){const text=await(await cache.match(key)).text();if(text.includes(${JSON.stringify(vendor.vendor_id)})||text.includes('PWA Private Test Vendor'))return true}}return false})()`),false);
  report.checks.push("Login keeps an HttpOnly session cookie; Cache Storage contains only versioned build assets, without profile data or API URLs");
  const workerTarget=(await c("Target.getTargets")).targetInfos.find(target=>target.type==="service_worker"&&target.url===origin+"/sw.js");
  assert.ok(workerTarget,"The active worker has a debugging target");
  const workerSession=(await c("Target.attachToTarget",{targetId:workerTarget.targetId,flatten:true})).sessionId;
  await c("Network.enable",{},workerSession);
  await c("Network.emulateNetworkConditions",{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0},workerSession);
  await c("Network.emulateNetworkConditions",{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});await wait("!!document.querySelector('.pwa-offline')");
  assert.equal(await ev("!!document.querySelector('#vendor-profile-edit') || document.body.innerText.includes('PWA Private Test Vendor')"),false);
  await go("/vendor/requests?test=private-navigation");await wait("document.title.includes('Connection needed')");assert.equal(await ev("document.body.innerText.includes('PWA Private Test Vendor')"),false);
  assert.ok((await cacheUrls()).every(url=>!url.includes("/api/")&&!url.includes("private-navigation")));
  report.checks.push("Offline transition removes authenticated UI; offline deep links show a generic page without cached operational data");
  await c("Network.emulateNetworkConditions",{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1},workerSession);
  await c("Network.emulateNetworkConditions",{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});await go("/vendor/profile");await wait("!!document.querySelector('#vendor-profile-edit')");
  assert.equal((await api("/api/auth/me")).body.vendor_id,vendor.vendor_id);report.checks.push("Reconnect revalidates the session and refetches the vendor workspace");
  // Serve a second worker revision with identical static assets to exercise the real update lifecycle.
  await ev("window.__updateMarker='editing';const i=document.querySelector('#vendor-profile-edit input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,'Unsaved edit');i.dispatchEvent(new Event('input',{bubbles:true}));true");
  await writeFile(swPath,originalWorker.replace(/const VERSION = "([^"]+)";/,'const VERSION = "$1-update-test";')+"\n// update test revision\n");
  await ev("navigator.serviceWorker.getRegistration().then(r=>r.update()).then(()=>true)");await wait("!!document.querySelector('.pwa-strip') && document.querySelector('.pwa-action')?.textContent==='Reload'");
  assert.equal(await ev("window.__updateMarker"),"editing");assert.equal(await ev("document.querySelector('#vendor-profile-edit input').value"),"Unsaved edit");
  await ev("document.querySelector('.pwa-action').click()");await wait("typeof window.__updateMarker==='undefined' && !!document.querySelector('#vendor-profile-edit')");
  const cookieAfter=(await c("Network.getCookies",{urls:[origin]})).cookies.find(cookie=>cookie.name==="nmc_session");assert.equal(cookieAfter.value,cookieBefore.value);
  assert.equal(await ev("localStorage.getItem('nmc_vendor_language')"),languageBefore);
  await wait("caches.keys().then(names=>names.filter(name=>name.startsWith('vendormap-static-')).length===1)");
  report.checks.push("A waiting update preserves unsaved form state until explicit Reload; activation and reload preserve the authenticated session");
  await api("/api/auth/logout",{});await go("/vendor/profile");await wait("!!document.querySelector('.auth-card')");assert.equal(await ev("document.body.innerText.includes('PWA Private Test Vendor')"),false);
  report.checks.push("Logout cannot restore a cached vendor profile");
  // Submit the actual officer form against the same first-party proxy used by
  // the production bundle. No credentials or writes target deployed services.
  await ev("document.querySelectorAll('.auth-choice button')[1].click();true");
  await wait("!!document.querySelector('input[autocomplete=username]')");
  await ev("(()=>{const set=(selector,value)=>{const i=document.querySelector(selector);Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,value);i.dispatchEvent(new Event('input',{bubbles:true}));};set('input[autocomplete=username]','review-admin');set('input[type=password]','review-only-password');return true;})()");
  await ev("document.querySelector('.auth-card form').requestSubmit();true");
  await wait("!!document.querySelector('.service-action')");
  assert.equal((await api('/api/auth/me')).body.role,'ADMIN');
  assert.equal((await api('/api/admin/overview')).status,200);
  assert.equal((await api('/api/allocation-requests?status=PENDING')).status,200);
  assert.equal((await api('/api/admin/vendors')).status,200);
  assert.equal((await api('/api/admin/zones')).status,200);
  await go('/admin/requests');await wait("!!document.querySelector('.admin-pending-requests')");
  assert.equal((await api('/api/auth/me')).body.role,'ADMIN');
  await ev("document.querySelector('.vm-utilities button[aria-label]').click();true");
  await wait("!!document.querySelector('.auth-card')");
  await go('/admin');await wait("!!document.querySelector('.auth-card')");
  assert.equal((await api('/api/admin/overview')).status,401);
  report.checks.push("Officer form login confirms the cookie; admin overview, pending applications, vendor directory and zones authorize; deep-link refresh persists and logout revokes the session");
  assert.deepEqual(browser.errors,[]);report.checks.push("No browser runtime exceptions");
  console.log(JSON.stringify(report,null,2));
} catch(error) {report.errors.push(String(error));throw error;}
finally {
  await writeFile(swPath,originalWorker);
  await mkdir(path.join(root,"reports/vendormap"),{recursive:true});await writeFile(path.join(root,"reports/vendormap/pwa-results.json"),JSON.stringify(report,null,2));
  await browser?.close();await new Promise(resolve=>server?.httpServer ? server.httpServer.close(resolve) : resolve());backend.kill();
}
