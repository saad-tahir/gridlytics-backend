#!/usr/bin/env node
/* pageperf.cjs — sign in as a role and time page loads: navigation, DOMContentLoaded, last API answer before idle, idle, requests, bytes,
 * long tasks, JS heap. node pageperf.cjs --role owner --web http://127.0.0.1:5295 --paths a,b --out file.json [--width 1440] [--repeat 2]
 * Passwords are read from DEMO_ACCOUNTS.local.md and never printed. */
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i > 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : d; };
const ROLE = arg("role", "owner");
const WIDTH = Number(arg("width", "1440"));
const WEB = arg("web", "http://127.0.0.1:5295");
const OUT = arg("out", "pageperf.json");
const SHOTS = arg("shots", "");
const REPEAT = Number(arg("repeat", "1"));
const PATHS = arg("paths", "/fleet").split(",").map((s) => s.trim()).filter(Boolean);
const accounts = fs.readFileSync("/home/user/saad-tahir/gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md", "utf8");
function passwordFor(email) {
  const row = accounts.split("\n").find((l) => l.includes(`\`${email}\``));
  const cells = row.split("|").map((c) => c.trim());
  const i = cells.findIndex((c) => c === `\`${email}\``);
  return cells[i + 1].replace(/`/g, "");
}
const slug = (p) => p.replace(/^\//, "").replace(/[^a-z0-9]+/gi, "_").replace(/^_+|_+$/g, "") || "root";

(async () => {
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const ctx = await browser.newContext({ viewport: { width: WIDTH, height: WIDTH <= 500 ? 812 : 1000 }, timezoneId: "Asia/Karachi", locale: "en-GB" });
  await ctx.addInitScript(() => {
    window.__lt = [];
    try { new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__lt.push(e.duration); }).observe({ type: "longtask", buffered: true }); } catch (e) {}
  });
  const page = await ctx.newPage();
  const CPU = Number(arg("cpu", "1"));
  const NET = arg("net", "");
  if (CPU > 1 || NET) {
    const cdp = await ctx.newCDPSession(page);
    if (CPU > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU });
    if (NET === "slow4g") {
      await cdp.send("Network.enable");
      await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 150, downloadThroughput: 200 * 1024, uploadThroughput: 90 * 1024 });
    }
  }
  const email = `${ROLE}@onyxleaf.demo`;
  await page.goto(`${WEB}/login`, { waitUntil: "networkidle" });
  await page.locator('input[type="email"], input[name="email"]').first().fill(email);
  await page.locator('input[type="password"]').first().fill(passwordFor(email));
  await page.getByRole("button", { name: /sign in/i }).first().click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 30000 }).catch(() => {});
  await page.waitForLoadState("networkidle").catch(() => {});
  const results = [];
  for (let rep = 0; rep < REPEAT; rep++) {
    for (const p of PATHS) {
      const reqs = [];
      let t0 = 0;
      const onReq = (r) => { r.__t = Date.now(); };
      const onFin = async (r) => {
        const u = r.url();
        const timing = r.timing();
        let size = 0;
        try { size = (await r.sizes()).responseBodySize; } catch (e) {}
        reqs.push({ u: u.replace(WEB, ""), api: u.includes("/api/"), t: (r.__t || 0) - t0, dur: timing.responseEnd > 0 ? timing.responseEnd : null, end: Date.now() - t0, size,
          type: r.resourceType(), status: (await r.response())?.status() });
      };
      page.on("request", onReq);
      page.on("requestfinished", onFin);
      t0 = Date.now();
      let err = null;
      await page.goto(`${WEB}${p}`, { waitUntil: "networkidle", timeout: 60000 }).catch((e) => { err = e.message.slice(0, 200); });
      const idle = Date.now() - t0;
      await page.waitForTimeout(300);
      page.off("request", onReq);
      page.off("requestfinished", onFin);
      const m = await page.evaluate(() => {
        const n = performance.getEntriesByType("navigation")[0] || {};
        const loading = /Loading|Fetching/i.test(document.body.innerText) || document.querySelectorAll('[aria-busy="true"], .animate-pulse').length;
        return { dcl: Math.round(n.domContentLoadedEventEnd || 0), load: Math.round(n.loadEventEnd || 0), longTasks: window.__lt.length,
          longTaskMs: Math.round(window.__lt.reduce((a, b) => a + b, 0)), maxLongTask: Math.round(Math.max(0, ...window.__lt)),
          heapMB: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1e5) / 10 : null, stillLoading: Boolean(loading),
          h1: (document.querySelector("h1") || {}).innerText || null, domNodes: document.getElementsByTagName("*").length };
      }).catch((e) => ({ evalErr: String(e).slice(0, 200) }));
      const api = reqs.filter((r) => r.api);
      const lastApiEnd = api.length ? Math.max(...api.map((r) => r.end)) : null;
      const row = { rep, path: p, err, idleMs: idle, ...m, lastApiEnd, requests: reqs.length, apiCalls: api.length,
        apiBytes: api.reduce((a, r) => a + (r.size || 0), 0), scriptBytes: reqs.filter((r) => r.type === "script").reduce((a, r) => a + (r.size || 0), 0),
        scripts: reqs.filter((r) => r.type === "script").length,
        slowApi: api.filter((r) => r.dur && r.dur > 300).map((r) => `${Math.round(r.dur)}ms ${r.u.slice(0, 90)}`),
        apiList: api.map((r) => `${r.status} ${Math.round(r.dur || 0)}ms ${r.size}B ${r.u.slice(0, 100)}`) };
      results.push(row);
      if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, `${ROLE}-${WIDTH}-${slug(p)}-r${rep}.png`) }).catch(() => {}); }
      console.log(`${String(idle).padStart(6)} dcl=${m.dcl} lastApi=${lastApiEnd} req=${reqs.length} api=${api.length} apiKB=${Math.round(row.apiBytes / 1024)} lt=${m.longTaskMs}/${m.maxLongTask} heap=${m.heapMB} loading=${m.stillLoading} ${p}`);
    }
  }
  fs.writeFileSync(OUT, JSON.stringify(results, null, 1));
  await browser.close();
})();
