import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import config from "../vite.config.js";

const vercel = JSON.parse(readFileSync(new URL("../vercel.json", import.meta.url), "utf8"));

test("Vercel proxies the complete API path before SPA routes and excludes static URLs", () => {
  assert.deepEqual(vercel.rewrites[0], {source:"/api/:path*",destination:"https://final-yr-project-ik72.onrender.com/api/:path*"});
  const spa = new RegExp(`^${vercel.rewrites[1].source}$`);
  for (const path of ["/", "/login", "/admin", "/admin/vendors", "/vendor/recommendations/NE_F_001"]) assert.ok(spa.test(path),path);
  for (const path of ["/api", "/api/auth/me", "/sw.js", "/manifest.webmanifest", "/assets/missing.js", "/icons/missing.png"]) assert.ok(!spa.test(path),path);
  assert.equal(vercel.headers.find(rule=>rule.source==="/api/:path*").headers[0].value,"private, no-store");
});

test("Vercel builds refuse a cross-site API override", () => {
  const previous = {VERCEL:process.env.VERCEL,VITE_API_BASE:process.env.VITE_API_BASE};
  try {
    process.env.VERCEL="1";
    process.env.VITE_API_BASE="https://final-yr-project-ik72.onrender.com";
    assert.throws(()=>config({mode:"production"}),/remove VITE_API_BASE/);
    process.env.VITE_API_BASE="";
    assert.ok(config({mode:"production"}));
    process.env.VITE_API_BASE="/";
    assert.ok(config({mode:"production"}));
  } finally {
    for(const [key,value] of Object.entries(previous)) {if(value===undefined)delete process.env[key];else process.env[key]=value;}
  }
});
