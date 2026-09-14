import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

process.env.TZ = "Asia/Shanghai";
const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script);
const element = {classList: {remove() {}}, addEventListener() {}};
const context = vm.createContext({
  document: {getElementById: () => element},
  fetch: () => new Promise(() => {}),
  setInterval() {}
});
vm.runInContext(script, context);
const buildWeightCalendar = vm.runInContext("buildWeightCalendar", context);
const weeks = buildWeightCalendar([
  {measured_at: "2026-09-13T12:00:00+08:00", weight_g: 82000},
  {measured_at: "2026-09-14T00:30:00+08:00", weight_g: 78000},
  {measured_at: "2026-09-14T08:00:00+08:00", weight_g: 80000},
  {measured_at: "2026-09-15T08:00:00+08:00", weight_g: 81000}
], new Date("2026-09-16T12:00:00+08:00"));

assert.equal(weeks.length, 53);
assert.equal(weeks.at(-2).average_g, 82000);
assert.equal(weeks.at(-1).days[0].average_g, 79000);
assert.equal(weeks.at(-1).average_g, 80000);
assert.equal(weeks.at(-1).count, 2);
assert.equal(weeks.at(-1).days[4].future, true);
console.log("weekly calendar check passed");
