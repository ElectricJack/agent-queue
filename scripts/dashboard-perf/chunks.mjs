// Initial JS per route of a built dashboard: entry + its static imports + the
// route chunk and its static imports. Usage: node chunks.mjs <dist dir>.
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
const [dir] = process.argv.slice(2);
const html = readFileSync(dir + "/index.html", "utf8");
const entry = html.match(/src="\/(assets\/[^"]+\.js)"/)[1];
const preloads = [...html.matchAll(/modulepreload" crossorigin href="\/(assets\/[^"]+\.js)"/g)].map((m) => m[1]);
const size = (f) => { const b = readFileSync(dir + "/" + f); return { raw: b.length, gz: gzipSync(b).length }; };
const staticImports = (f, seen = new Set()) => {
  if (seen.has(f)) return seen; seen.add(f);
  const src = readFileSync(dir + "/" + f, "utf8");
  for (const m of src.matchAll(/(?:^|[;\n])\s*import\s*(?:[^"';]*?from\s*)?"\.\/([^"]+\.js)"/g)) staticImports("assets/" + m[1], seen);
  for (const m of src.matchAll(/import\{[^}]*\}from"\.\/([^"]+\.js)"/g)) staticImports("assets/" + m[1], seen);
  for (const m of src.matchAll(/import"\.\/([^"]+\.js)"/g)) staticImports("assets/" + m[1], seen);
  return seen;
};
const sum = (files) => [...files].reduce((a, f) => { const s = size(f); return { raw: a.raw + s.raw, gz: a.gz + s.gz }; }, { raw: 0, gz: 0 });
const initial = staticImports(entry);
const e = size(entry);
console.log("entry", entry, (e.raw / 1024).toFixed(0) + "KB", (e.gz / 1024).toFixed(0) + "KB gz");
const init = sum(initial);
console.log("entry+static imports", initial.size, "files", (init.raw / 1024).toFixed(0) + "KB", (init.gz / 1024).toFixed(0) + "KB gz");
const routes = { shell: "AppShellV2", graph: "Graph", tasks: "Tasks", reviews: "ReviewsInbox", metrics: "Metrics", agents: "AgentWorkspace", sessions: "Sessions", overview: "Overview", taskdetail: "TaskDetail" };
const files = (await import("node:fs")).readdirSync(dir + "/assets");
const shellFile = "assets/" + files.find((f) => f.startsWith("AppShellV2-") && f.endsWith(".js"));
const shellSet = staticImports(shellFile, new Set(initial));
for (const [k, name] of Object.entries(routes)) {
  const f = files.find((x) => x.startsWith(name + "-") && x.endsWith(".js"));
  if (!f) { console.log(k, "no chunk"); continue; }
  const set = staticImports("assets/" + f, new Set(shellSet));
  const t = sum(set);
  console.log(k.padEnd(10), "initial JS incl. shell:", (t.raw / 1024).toFixed(0) + "KB", (t.gz / 1024).toFixed(0) + "KB gz");
}
