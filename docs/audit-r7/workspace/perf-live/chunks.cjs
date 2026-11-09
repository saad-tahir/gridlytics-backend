const fs = require("node:fs");
const { chromium } = require("playwright");
const accounts = fs.readFileSync("/home/user/saad-tahir/gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md", "utf8");
function pw(email) { const row = accounts.split("\n").find((l) => l.includes(`\`${email}\``)); const c = row.split("|").map((x) => x.trim()); return c[c.findIndex((x) => x === `\`${email}\``) + 1].replace(/`/g, ""); }
(async () => {
  const WEB = "http://127.0.0.1:5399";
  const b = await chromium.launch({ args: ["--no-sandbox"] });
  const paths = process.argv.slice(2);
  for (const p of paths) {
    const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await ctx.newPage();
    await page.goto(`${WEB}/login`, { waitUntil: "networkidle" });
    await page.locator('input[type="email"]').first().fill("analyst@onyxleaf.demo");
    await page.locator('input[type="password"]').first().fill(pw("analyst@onyxleaf.demo"));
    await page.getByRole("button", { name: /sign in/i }).first().click();
    await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 30000 }).catch(() => {});
    await page.waitForLoadState("networkidle");
    const seen = [];
    page.on("requestfinished", async (r) => { if (r.resourceType() === "script") { let s = 0; try { s = (await r.sizes()).responseBodySize; } catch (e) {} seen.push([r.url().split("/").pop(), s]); } });
    await page.goto(`${WEB}${p}`, { waitUntil: "networkidle" });
    // a fresh context: what this page needs beyond the sign-in page's scripts
    console.log(p, JSON.stringify(seen.map(([n, s]) => `${n.replace(/-[A-Za-z0-9_-]{8}\.js$/, "")}:${Math.round(s / 1024)}KB`)));
    await ctx.close();
  }
  await b.close();
})();
