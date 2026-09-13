import assert from "node:assert/strict";
import test from "node:test";
import {
  HANDLED_REOPENABLE,
  groupHandled,
  handledAlsoLine,
  handledHeadline,
  handledKey,
  handledProofHref,
  joinClauses,
} from "../src/lib/handled.ts";

// Rows arrive newest-first from /today (brief_state.handled_since sorts by
// `at` descending); `at` values below are chosen to match that order.
const row = (kind, alertId, extra = {}) => ({
  kind,
  summary: `${kind} ${alertId}`,
  at: extra.at ?? "2026-09-13T06:00:00+00:00",
  alert_id: alertId,
  status: extra.status ?? "open",
  ...extra,
});
const never = () => false;

test("a newer close keeps the visible slot over an older escalation, with Undo intact", () => {
  const rows = groupHandled([
    row("closed", 7, { at: "2026-09-13T07:00:00+00:00", status: "resolved" }),
    row("escalated", 7, { at: "2026-09-13T01:00:00+00:00" }),
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].item.kind, "closed");
  assert.deepEqual(rows[0].also.map((a) => a.kind), ["escalated"]);
  assert.equal(handledAlsoLine(rows[0]), "also raised");
});

test("an escalation beats a newer route or nudge on the same alert", () => {
  const rows = groupHandled([
    row("nudged", 3, { at: "2026-09-13T07:00:00+00:00" }),
    row("escalated", 3, { at: "2026-09-13T03:00:00+00:00" }),
    row("routed", 3, { at: "2026-09-13T01:00:00+00:00" }),
  ]);
  assert.equal(rows[0].item.kind, "escalated");
  assert.deepEqual(rows[0].also.map((a) => a.kind), ["nudged", "routed"]);
});

test("a merge into a listed survivor folds into that survivor's row", () => {
  const rows = groupHandled([
    row("closed", 9, { status: "resolved" }),
    row("merged", 10, { superseded_by_alert_id: 9, status: "merged" }),
    row("merged", 11, { superseded_by_alert_id: 9, status: "merged" }),
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].foldedIn, 2);
  assert.equal(handledAlsoLine(rows[0]), "2 duplicates folded in");
});

test("a merge whose survivor is not listed stays as its own row", () => {
  const rows = groupHandled([row("merged", 10, { superseded_by_alert_id: 99, status: "merged" })]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].item.kind, "merged");
});

test("a self-referencing merge produces no rows (the panel renders nothing)", () => {
  assert.deepEqual(groupHandled([row("merged", 7, { superseded_by_alert_id: 7 })]), []);
});

test("rows sort escalations first, then closes, then routed/nudged, then transparency", () => {
  const rows = groupHandled([
    row("watching", null, { alert_id: null, headline: "stock-acme" }),
    row("routed", 1),
    row("closed", 2, { status: "resolved" }),
    row("escalated", 3),
  ]);
  assert.deepEqual(rows.map((r) => r.item.kind), ["escalated", "closed", "routed", "watching"]);
});

test("headline buckets every kind and pluralises", () => {
  const rows = groupHandled([
    row("closed", 1, { status: "resolved" }),
    row("merged", 2, { superseded_by_alert_id: 50, status: "merged" }),
    row("routed", 3),
    row("nudged", 4),
    row("escalated", 5),
    row("drafted", 6),
    row("suggested_workflow", 7),
    row("watching", null, { alert_id: null, at: "2026-09-13T05:00:00+00:00" }),
    row("stopped_watching", null, { alert_id: null, at: "2026-09-13T04:00:00+00:00" }),
    row("snoozed", 8),
  ]);
  assert.equal(
    handledHeadline(rows, never),
    "Since your last brief: 2 off your plate, 2 in others' hands, 1 waiting on you, " +
      "1 draft ready, 1 workflow suggested, 2 watch changes, and 1 other move.",
  );
});

test("a reverted close is back on the plate, never a fresh move", () => {
  const rows = groupHandled([row("closed", 1, { status: "open" })]);
  const reverted = (h) => h.status === "open";
  assert.equal(handledHeadline(rows, reverted), "Since your last brief: 1 back on your plate.");
});

test("joinClauses reads naturally at one, two and many parts", () => {
  assert.equal(joinClauses(["a"]), "a");
  assert.equal(joinClauses(["a", "b"]), "a and b");
  assert.equal(joinClauses(["a", "b", "c", "d"]), "a, b, c, and d");
});

test("proof link encodes both filters and is absent without an event type", () => {
  assert.equal(
    handledProofHref(row("closed", 1, { event_type: "alert_review_closed", headline: "Q3 pricing & copy" })),
    "/audit?event_type=alert_review_closed&q=Q3%20pricing%20%26%20copy",
  );
  assert.equal(handledProofHref(row("closed", 1)), null);
});

test("row keys distinguish two moves on one alert", () => {
  const a = row("routed", 1, { at: "2026-09-13T01:00:00+00:00" });
  const b = row("closed", 1, { at: "2026-09-13T07:00:00+00:00" });
  assert.notEqual(handledKey(a), handledKey(b));
});

test("reopenable statuses match what the server accepts, plus unknown", () => {
  assert.deepEqual([...HANDLED_REOPENABLE].sort(), ["", "dismissed", "expired", "merged", "resolved"]);
});
