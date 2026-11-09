// A static server for the existing production build (frontend/dist, read only) with /api proxied to the scratch API: node prodserve.cjs <port>
// Serves gzip when the browser asks (as nginx would with gzip on), SPA fallback to index.html.
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");
const DIST = "/home/user/gridlytics-frontend/frontend/dist";
const PORT = Number(process.argv[2] || 5399);
const TYPES = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml", ".json": "application/json" };
const gz = new Map();
http.createServer((req, res) => {
  if (req.url.startsWith("/api/")) {
    const p = http.request({ host: "127.0.0.1", port: 8151, path: req.url, method: req.method, headers: req.headers }, (r) => {
      res.writeHead(r.statusCode, r.headers);
      r.pipe(res);
    });
    p.on("error", () => { res.writeHead(502); res.end(); });
    req.pipe(p);
    return;
  }
  let f = path.join(DIST, decodeURIComponent(req.url.split("?")[0]));
  if (!f.startsWith(DIST) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  const type = TYPES[path.extname(f)] || "application/octet-stream";
  const body = fs.readFileSync(f);
  const headers = { "content-type": type, "cache-control": f.includes("/assets/") ? "public, max-age=31536000, immutable" : "no-cache" };
  if (/gzip/.test(req.headers["accept-encoding"] || "")) {
    if (!gz.has(f)) gz.set(f, zlib.gzipSync(body, { level: 6 }));
    res.writeHead(200, { ...headers, "content-encoding": "gzip" });
    res.end(gz.get(f));
  } else {
    res.writeHead(200, headers);
    res.end(body);
  }
}).listen(PORT, "127.0.0.1", () => console.log("serving dist on", PORT));
