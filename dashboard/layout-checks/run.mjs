// Layout checks: every checks/*.check.mjs × viewport profile, against the built
// bundle and a stub daemon. Usage (from dashboard/ via npm):
//   npm -w dashboard run check:layout -- [--only a,b] [--profiles p,q] [--out dir]
// Build first: npm -w dashboard run build. Chrome: $CHROME or /usr/bin/google-chrome.
import { existsSync, mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { PROFILES, ALL } from "./profiles.mjs";
import { loadFixtures, startStubServer } from "./server.mjs";
import { watchSockets } from "./probes.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const only = opt("--only")?.split(",").filter(Boolean);
const onlyProfiles = opt("--profiles")?.split(",").filter(Boolean);
const out = opt("--out") ?? join(here, ".out");
const dist = join(here, "..", "dist");

if (!existsSync(join(dist, "index.html"))) {
  console.error("dashboard/dist is missing — run `npm -w dashboard run build` first.");
  process.exit(2);
}
mkdirSync(out, { recursive: true });

const fixtures = await loadFixtures(join(here, "fixtures"));
const checks = [];
for (const file of readdirSync(join(here, "checks")).filter((f) => f.endsWith(".check.mjs")).sort()) {
  const check = await import(pathToFileURL(join(here, "checks", file)).href);
  if (!only || only.includes(check.name)) checks.push(check);
}
if (only && checks.length !== only.length) {
  console.error(`unknown check in --only: ${only.filter((n) => !checks.some((c) => c.name === n)).join(", ")}`);
  process.exit(2);
}

const { default: puppeteer } = await import("puppeteer-core");
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME ?? "/usr/bin/google-chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
const results = [];
try {
  for (const check of checks) {
    for (const profile of check.profiles ?? ALL) {
      if (onlyProfiles && !onlyProfiles.includes(profile)) continue;
      const spec = PROFILES[profile];
      const stub = await startStubServer({ distDir: dist, fixtures });
      const context = await browser.createBrowserContext();
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.setViewport(spec.viewport);
      await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: spec.reducedMotion ? "reduce" : "no-preference" }]);
      const sockets = await watchSockets(page);
      const shot = (label) => page.screenshot({ path: join(out, `${check.name}.${profile}.${label}.png`) });
      const t = { page, stub, profile, isPhone: spec.phone, url: (path) => stub.url + path, sockets, shot };
      const started = Date.now();
      try {
        await check.run(t);
        if (stub.unhandled.length) throw new Error(`API requests with no fixture: ${[...new Set(stub.unhandled)].join(", ")}`);
        if (errors.length) throw new Error(`page errors: ${errors.join(" | ")}`);
        await shot("final");
        results.push({ check: check.name, profile, ok: true, ms: Date.now() - started });
        console.log(`ok   ${check.name} @ ${profile}`);
      } catch (error) {
        await shot("failure").catch(() => {});
        results.push({ check: check.name, profile, ok: false, ms: Date.now() - started, error: String(error?.stack ?? error) });
        console.log(`FAIL ${check.name} @ ${profile}\n     ${String(error?.message ?? error)}`);
      } finally {
        await context.close();
        await stub.close();
      }
    }
  }
  writeFileSync(join(out, "report.json"), JSON.stringify({ chrome: await browser.version(), results }, null, 2));
} finally {
  await browser.close();
}
const failed = results.filter((r) => !r.ok).length;
console.log(`${results.length - failed}/${results.length} passed; screenshots and report.json in ${out}`);
process.exit(failed ? 1 : 0);
