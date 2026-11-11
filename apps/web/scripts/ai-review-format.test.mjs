import assert from "node:assert/strict";
import { reviewNumber, reviewPercent } from "../src/pages/AiReviewPage/format.mjs";

assert.equal(reviewNumber(null), "—");
assert.equal(reviewNumber(undefined), "—");
assert.equal(reviewNumber(Number.NaN), "—");
assert.equal(reviewNumber(Number.POSITIVE_INFINITY), "—");
assert.notEqual(reviewNumber(null), "0");
assert.notEqual(reviewPercent(null), "0%");
assert.equal(reviewPercent(0), "0%");
assert.match(reviewPercent(0.012), /1\.2%/);
assert.match(reviewNumber(1.5, 2), /1\.5/);
console.log("ai-review format OK");
