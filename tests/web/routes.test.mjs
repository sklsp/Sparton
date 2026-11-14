// The route table: unique ids, every load points at a view file that exists, nav flags as designed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { ROUTES } from "../../app/web/routes.js";

// The view paths exactly as written in the table. Importing the views themselves would need a full DOM,
// so the check is on the file names instead.
const routesSource = await readFile(new URL("../../app/web/routes.js", import.meta.url), "utf8");
const viewPaths = [...routesSource.matchAll(/import\("\.(\/views\/[^"]+)"\)/g)].map((m) => m[1]);

test("route ids are unique", () => {
  const ids = ROUTES.map((r) => r.id);
  assert.equal(new Set(ids).size, ids.length, `duplicate ids among ${ids.join(", ")}`);
});

test("every load points at a view file that exists", () => {
  // One import() per route: the table is data, so the counts must agree.
  assert.equal(viewPaths.length, ROUTES.length, "route count and import() count differ");
  for (const r of ROUTES) assert.equal(typeof r.load, "function", `${r.id} has no load`);
  const missing = viewPaths.filter((p) => !existsSync(new URL(`../../app/web${p}`, import.meta.url)));
  assert.deepEqual(missing, [], `missing view files: ${missing.join(", ")}`);
});

test("the nav-hidden routes are exactly start and product", () => {
  const hidden = ROUTES.filter((r) => r.nav === false).map((r) => r.id);
  assert.deepEqual(hidden, ["start", "product"]);
});