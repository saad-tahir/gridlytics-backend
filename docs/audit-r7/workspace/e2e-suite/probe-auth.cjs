const fs = require("node:fs");
const { chromium } = require("playwright");
const ACCOUNTS = "/home/user/saad-tahir/gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md";
const accounts = fs.readFileSync(ACCOUNTS, "utf8");
function passwordFor(email) {
  const row = accounts.split("\n").find((l) => l.includes(`\`${email}\``));
  const cells = row.split("|").map((c) => c.trim());
  return cells[cells.findIndex((c) => c === `\`${email}\``) + 1].replace(/`/g, "");
}
(async () => {
  const browser = await chromium.launch({ args: ["--no-sandbox"] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: "Asia/Karachi" });
  const page = await ctx.newPage();
  const t0 = Date.now();
  await page.goto("http://127.0.0.1:5295/login");
  await page.getByLabel("Email").fill("viewer@onyxleaf.demo");
  await page.getByLabel("Password").fill(passwordFor("viewer@onyxleaf.demo"));
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/fleet/, { timeout: 20000 });
  console.log("signed in in", Date.now() - t0, "ms; h1:", await page.locator("h1").first().innerText());
  const state = await ctx.storageState();
  console.log("storageState origins:", state.origins.map((o) => ({ origin: o.origin, keys: o.localStorage.map((k) => k.name) })));
  await ctx.storageState({ path: ".auth/probe-viewer.json" });
  await browser.close();
  const b2 = await chromium.launch({ args: ["--no-sandbox"] });
  const c2 = await b2.newContext({ storageState: ".auth/probe-viewer.json", viewport: { width: 1440, height: 1000 } });
  const p2 = await c2.newPage();
  await p2.goto("http://127.0.0.1:5295/fleet");
  await p2.waitForSelector("h1", { timeout: 15000 });
  console.log("reused state:", p2.url(), (await p2.locator("h1").first().innerText()));
  await p2.goto("http://127.0.0.1:5295/sites/6d4db49bf87e/savings");
  await p2.waitForTimeout(2500);
  const txt = await p2.locator("body").innerText();
  console.log("viewer savings text sample:", txt.replace(/\s+/g, " ").slice(0, 600));
  console.log("last-data stamps:", await p2.locator("[data-last-data]").count(), "money words:", (txt.match(/\bRs\.?\s?[\d,]+|PKR|rupee/gi) || []).slice(0, 5));
  await b2.close();
})().catch((e) => { console.error("probe failed:", e.message); process.exit(1); });
