const assert = require("node:assert/strict");
const filters = require("../src/wanyard/static/detection-wall-filters.js");

assert.equal(filters.parse(null), null);
assert.equal(filters.parse("all"), null);
assert.deepEqual([...filters.parse("garden,front,garden")], ["garden", "front"]);
assert.equal(filters.serialize(new Set(["garden", "front"])), "front,garden");

let selected = filters.toggle(null, "front", ["front", "garden", "desk"]);
assert.deepEqual([...selected], ["front"]);
selected = filters.toggle(selected, "garden", ["front", "garden", "desk"]);
assert.deepEqual([...selected], ["front", "garden"]);
selected = filters.toggle(selected, "front", ["front", "garden", "desk"]);
assert.deepEqual([...selected], ["garden"]);

// The last selected camera cannot be toggled into an empty wall.
selected = filters.toggle(selected, "garden", ["front", "garden", "desk"]);
assert.deepEqual([...selected], ["garden"]);

// Selecting every individual camera canonicalizes back to All.
selected = filters.toggle(selected, "front", ["front", "garden"]);
assert.equal(selected, null);
assert.equal(filters.active(selected, "all"), true);
assert.equal(filters.active(selected, "front"), false);

console.log("detection wall camera filter tests passed");
