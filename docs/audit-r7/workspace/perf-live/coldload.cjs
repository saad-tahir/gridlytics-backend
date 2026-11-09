const { chromium } = require("playwright");
(async () => {
  const web = process.argv[2], cpu = Number(process.argv[3] || 1), net = process.argv[4] || "";
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  for (let i = 0; i < 2; i++) {
    const ctx = await browser.newContext({ viewport: { width: 375, height: 812 }, timezoneId: "Asia/Karachi" });
    const page = await ctx.newPage();
    const cdp = await ctx.newCDPSession(page);
    if (cpu > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: cpu });
    if (net === "slow4g") { await cdp.send("Network.enable"); await cdp.send("Network.emulateNetworkConditions", { offline: false, latency: 150, downloadThroughput: 200 * 1024, uploadThroughput: 90 * 1024 }); }
    let bytes = 0, n = 0;
    page.on("requestfinished", async (r) => { try { const s = await r.sizes(); bytes += s.responseBodySize + s.responseHeadersSize; n++; } catch (e) {} });
    const t0 = Date.now();
    await page.goto(`${web}/login`, { waitUntil: "load", timeout: 120000 });
    const tl = Date.now() - t0;
    await page.locator('input[type="email"]').first().waitFor({ timeout: 120000 });
    const tf = Date.now() - t0;
    await page.waitForLoadState("networkidle").catch(() => {});
    console.log(JSON.stringify({ web, cpu, net, run: i, loadEvent: tl, formVisible: tf, idle: Date.now() - t0, requests: n, transferKB: Math.round(bytes / 1024) }));
    await ctx.close();
  }
  await browser.close();
})();
