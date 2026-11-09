#!/usr/bin/env node
/*
 * shot.cjs — sign in to the scratch Gridlytics web app as a role, open pages, screenshot, and report problems.
 *
 *   node shot.cjs --role owner --paths /fleet,/sites/<id>/plan [--width 1440|375] [--out DIR] [--a11y] [--html] [--actions "click:text=Hold;fill:#x=1"]
 *
 * Roles: owner | operator | analyst | viewer | founder (founder opens /admin with the console's login and code secret).
 * Writes DIR/<role>-<width>-<slug>.png and DIR/<role>-<width>-<slug>.json ({url, title, console, pageErrors, failedRequests, api4xx, api5xx,
 * overflowX, headings, buttons, landmarks, a11yTree?, textSample}). Passwords are read from DEMO_ACCOUNTS.local.md and never printed.
 */
const fs = require("node:fs");
const path = require("node:path");
const { createHmac } = require("node:crypto");
const { chromium } = require("playwright");

const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i > 0 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : d; };
const flag = (n) => process.argv.includes(`--${n}`);
const ROLE = arg("role", "owner");
const WIDTH = Number(arg("width", "1440"));
const HEIGHT = WIDTH <= 500 ? 812 : 1000;
const WEB = arg("web", "http://127.0.0.1:5295");
const OUT = arg("out", path.join(__dirname, "out"));
const PATHS = (arg("paths", "/fleet")).split(",").map((s) => s.trim()).filter(Boolean);
const ACCOUNTS = arg("accounts", "/home/user/saad-tahir/gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md");
const ACTIONS = arg("actions", "");
fs.mkdirSync(OUT, { recursive: true });

const accounts = fs.readFileSync(ACCOUNTS, "utf8");
function passwordFor(email) {
  const row = accounts.split("\n").find((l) => l.includes(`\`${email}\``));
  if (!row) throw new Error(`no row for ${email}`);
  const cells = row.split("|").map((c) => c.trim());
  const i = cells.findIndex((c) => c === `\`${email}\``);
  return cells[i + 1].replace(/`/g, "");
}
const founderLine = (label) => (accounts.match(new RegExp(`\\*\\*${label}:\\*\\* \`([^\`]+)\``)) ?? [])[1];
const slug = (p) => p.replace(/^\//, "").replace(/[^a-z0-9]+/gi, "_").replace(/^_+|_+$/g, "") || "root";

(async () => {
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 1, timezoneId: "Asia/Karachi", locale: "en-GB" });
  const page = await ctx.newPage();
  const events = { console: [], pageErrors: [], failedRequests: [], api4xx: [], api5xx: [] };
  page.on("console", (m) => { if (["error", "warning"].includes(m.type())) events.console.push(`${m.type()}: ${m.text()}`.slice(0, 400)); });
  page.on("pageerror", (e) => events.pageErrors.push(String(e).slice(0, 400)));
  page.on("requestfailed", (r) => events.failedRequests.push(`${r.method()} ${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => { const s = r.status(); const u = r.url(); if (u.includes("/api/")) { if (s >= 500) events.api5xx.push(`${s} ${r.request().method()} ${u}`); else if (s >= 400) events.api4xx.push(`${s} ${r.request().method()} ${u}`); } });
  const reset = () => { for (const k of Object.keys(events)) events[k] = []; };

  // sign in
  if (ROLE === "founder") {
    const username = founderLine("Founder console login") || founderLine("Login") || founderLine("Username");
    const pass = founderLine("Founder console password") || founderLine("Password");
    const secret = founderLine("Code secret") || founderLine("Founder console code secret");
    await page.goto(`${WEB}/admin`, { waitUntil: "networkidle" });
    const u = page.locator('input[name="username"], input[autocomplete="username"], input[type="text"]').first();
    await u.fill(username || "founder");
    await page.locator('input[type="password"]').first().fill(pass || "");
    await page.getByRole("button", { name: /sign in|continue|log in/i }).first().click();
    await page.waitForTimeout(800);
    const codeBox = page.locator('input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]').first();
    if (await codeBox.count()) {
      // TOTP-like code: 6 digits from HMAC-SHA1 of the 30 s counter, as the repo's own check does (fallback: ask page for hint)
      const counter = Math.floor(Date.now() / 30000);
      const buf = Buffer.alloc(8); buf.writeBigUInt64BE(BigInt(counter));
      const h = createHmac("sha1", Buffer.from(secret || "", "utf8")).update(buf).digest();
      const o = h[h.length - 1] & 0xf;
      const code = String(((h.readUInt32BE(o) & 0x7fffffff) % 1000000)).padStart(6, "0");
      await codeBox.fill(code);
      await page.getByRole("button", { name: /sign in|continue|verify/i }).first().click();
      await page.waitForTimeout(800);
    }
  } else {
    const email = `${ROLE}@onyxleaf.demo`;
    await page.goto(`${WEB}/login`, { waitUntil: "networkidle" });
    await page.locator('input[type="email"], input[name="email"]').first().fill(email);
    await page.locator('input[type="password"]').first().fill(passwordFor(email));
    await page.getByRole("button", { name: /sign in/i }).first().click();
    await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 20000 }).catch(() => {});
  }
  const signedIn = !page.url().includes("/login");
  const results = [];
  for (const p of PATHS) {
    reset();
    const t0 = Date.now();
    await page.goto(`${WEB}${p}`, { waitUntil: "networkidle", timeout: 45000 }).catch((e) => events.pageErrors.push(`goto: ${e.message}`));
    await page.waitForTimeout(1200);
    if (ACTIONS) {
      for (const act of ACTIONS.split(";").map((s) => s.trim()).filter(Boolean)) {
        const [kind, rest] = [act.slice(0, act.indexOf(":")), act.slice(act.indexOf(":") + 1)];
        try {
          if (kind === "click") await page.locator(rest).first().click({ timeout: 5000 });
          else if (kind === "fill") { const [sel, val] = rest.split("="); await page.locator(sel).first().fill(val); }
          else if (kind === "wait") await page.waitForTimeout(Number(rest));
          else if (kind === "key") await page.keyboard.press(rest);
          await page.waitForTimeout(600);
        } catch (e) { events.pageErrors.push(`action ${act}: ${e.message}`.slice(0, 300)); }
      }
    }
    const ms = Date.now() - t0;
    const base = path.join(OUT, `${ROLE}-${WIDTH}-${slug(p)}`);
    await page.screenshot({ path: `${base}.png`, fullPage: true }).catch(() => {});
    const info = await page.evaluate(() => {
      const txt = (el) => (el.innerText || el.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 80);
      return {
        title: document.title,
        overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        scrollWidth: document.documentElement.scrollWidth,
        headings: [...document.querySelectorAll("h1,h2,h3")].map((h) => `${h.tagName}: ${txt(h)}`).slice(0, 60),
        buttons: [...document.querySelectorAll("button,a[role=button]")].map((b) => txt(b) || "(no accessible name)").slice(0, 80),
        unlabeledInputs: [...document.querySelectorAll("input,select,textarea")].filter((i) => { const id = i.id; const lab = id && document.querySelector(`label[for="${id}"]`); return !(lab || i.closest("label") || i.getAttribute("aria-label") || i.getAttribute("aria-labelledby")); }).map((i) => `${i.tagName} ${i.type || ""} ${i.name || i.placeholder || ""}`).slice(0, 30),
        landmarks: [...document.querySelectorAll("main,nav,header,aside,footer,[role=main],[role=navigation]")].map((e) => e.tagName + (e.getAttribute("aria-label") ? `[${e.getAttribute("aria-label")}]` : "")),
        moneyWords: [...document.body.innerText.matchAll(/\bRs\.?\s?[\d,]+|PKR|rupee/gi)].map((m) => m[0]).slice(0, 20),
        bannedWords: [...document.body.innerText.matchAll(/\boptimal\b|guaranteed|maximum savings/gi)].map((m) => m[0]),
        textSample: document.body.innerText.replace(/\s+/g, " ").slice(0, 3000),
      };
    }).catch((e) => ({ evalError: String(e) }));
    let a11y = null;
    if (flag("a11y")) a11y = await page.accessibility.snapshot().catch(() => null);
    if (flag("html")) fs.writeFileSync(`${base}.html`, await page.content());
    const rec = { role: ROLE, width: WIDTH, path: p, url: page.url(), signedIn, loadMs: ms, ...events, ...info, a11yTree: a11y };
    fs.writeFileSync(`${base}.json`, JSON.stringify(rec, null, 2));
    results.push({ path: p, url: page.url(), loadMs: ms, pageErrors: events.pageErrors.length, consoleErrors: events.console.length, api4xx: events.api4xx.length, api5xx: events.api5xx.length, overflowX: info.overflowX, screenshot: `${base}.png`, json: `${base}.json` });
  }
  await browser.close();
  console.log(JSON.stringify({ role: ROLE, width: WIDTH, signedIn, results }, null, 2));
})().catch((e) => { console.error("shot failed:", e.message); process.exit(1); });
