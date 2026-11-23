import test from "node:test";
import assert from "node:assert/strict";
import { HISTORY_HOURS, HOUR_MS, MAX_FILE_BYTES, inspectHistoryCSV, makeRequest, parseHistoryCSV, parseHour, requestFromCSV, toCSV } from "../workload/static/input.mjs";

function historyFrom(start, length) {
  const milliseconds = parseHour(start).milliseconds;
  return Array.from({ length }, (_, index) => ({
    hour: new Date(milliseconds + index * HOUR_MS).toISOString().slice(0, 19),
    count: index,
  }));
}

test("209-hour upload retains every row and exposes 42 inclusive target hours", () => {
  const history = historyFrom("2024-12-24T00:00:00", 209);
  const text = toCSV(history);
  const original = structuredClone(history);
  const inspected = inspectHistoryCSV(text);
  assert.deepEqual(inspected.history, original);
  assert.deepEqual(parseHistoryCSV(text), original);
  assert.equal(inspected.first_target_hour, "2024-12-31T00:00:00");
  assert.equal(inspected.last_target_hour, "2025-01-01T17:00:00");
  const hours = (parseHour(inspected.last_target_hour).milliseconds - parseHour(inspected.first_target_hour).milliseconds) / HOUR_MS + 1;
  assert.equal(hours, 42);
  assert.deepEqual(requestFromCSV(text), { target_hour: inspected.last_target_hour, history: original.slice(-168) });
  assert.deepEqual(history, original);
  assert.equal(toCSV(inspected.history), text);
});

test("each selected target includes only its exact prior 168 hours, never its own or future rows", () => {
  const history = historyFrom("2024-02-20T00:00:00", 240);
  const text = toCSV(history);
  for (let end = HISTORY_HOURS; end <= history.length; end += 1) {
    const target = new Date(parseHour(history[0].hour).milliseconds + end * HOUR_MS).toISOString().slice(0, 19);
    const request = makeRequest(target, text);
    assert.equal(request.target_hour, target);
    assert.deepEqual(request.history, history.slice(end - HISTORY_HOURS, end));
    assert.equal(request.history.length, 168);
    assert.equal(request.history.at(-1).count, end - 1);
    assert.ok(request.history.every(row => parseHour(row.hour).milliseconds < parseHour(target).milliseconds));
  }
  assert.equal(text, toCSV(history), "Selecting windows does not rewrite the uploaded history");
});

test("missing history differs from a valid all-zero history", () => {
  const history = historyFrom("2025-01-01T00:00:00", 209).map(row => ({ ...row, count: 0 }));
  const text = toCSV(history);
  const { first_target_hour: first, last_target_hour: last } = inspectHistoryCSV(text);
  assert.ok(makeRequest(first, text).history.every(row => row.count === 0));
  assert.ok(makeRequest(last, text).history.every(row => row.count === 0));
  for (const target of ["2025-01-07T23:00:00", "2025-01-09T18:00:00"]) {
    assert.throws(() => makeRequest(target, text), /데이터 부족:.*2025-01-08 00:00.*2025-01-09 17:00/);
  }
  assert.throws(() => inspectHistoryCSV(toCSV(history.slice(0, 167))), /데이터 부족/);
});

test("even missing, duplicated, or reversed rows outside a chosen window invalidate the full upload", () => {
  const history = historyFrom("2025-01-01T00:00:00", 209);
  for (const rows of [
    [...history.slice(0, 199), ...history.slice(200)],
    [...history.slice(0, 199), history[198], ...history.slice(200)],
    [...history.slice(0, 199), history[200], history[199], ...history.slice(201)],
    [...history].reverse(),
  ]) {
    const text = toCSV(rows);
    assert.throws(() => parseHistoryCSV(text), /누락·중복·역순/);
    assert.throws(() => makeRequest("2025-01-08T00:00:00", text), /누락·중복·역순/);
  }
});

test("naive dates retain year and leap-day boundaries and reject minute or offset inputs", () => {
  for (const [start, target] of [["0001-01-01T00:00:00", "0001-01-08T00:00:00"], ["2024-02-23T00:00:00", "2024-03-01T00:00:00"], ["2024-12-25T00:00:00", "2025-01-01T00:00:00"]]) {
    const text = toCSV(historyFrom(start, 168));
    assert.equal(inspectHistoryCSV(text).first_target_hour, target);
    assert.equal(requestFromCSV(text).target_hour, target);
  }
  const text = toCSV(historyFrom("2025-01-01T00:00:00", 209));
  for (const target of ["2025-01-08T00:00:00.000001", "2025-01-08T00:00:01", "2025-01-08T00:01:00", "2025-01-08T00:00:00Z", "2025-01-08T00:00:00+09:00"]) {
    assert.throws(() => makeRequest(target, text), /정각/);
  }
  assert.throws(() => inspectHistoryCSV(text.replace("2025-01-09T16:00:00", "2025-01-09T16:00:00.000001")), /정각/);
  assert.throws(() => requestFromCSV(toCSV(historyFrom("9999-12-25T00:00:00", 168))), /9999년/);
});

test("full historical-sized uploads fit the unchanged byte limit and retain strict count validation", () => {
  const history = historyFrom("2011-01-01T00:00:00", 8787);
  const text = toCSV(history);
  assert.ok(new TextEncoder().encode(text).length < MAX_FILE_BYTES);
  assert.equal(inspectHistoryCSV(text).history.length, 8787);
  assert.deepEqual(requestFromCSV(text).history, history.slice(-168));
  assert.throws(() => parseHistoryCSV("x".repeat(MAX_FILE_BYTES + 1)), /1 MB/);
  for (const count of ["-1", "1.5", "false", "100001", "NaN", "1e2"]) {
    assert.throws(() => parseHistoryCSV(text.replace(",8786\n", `,${count}\n`)), /건수/);
  }
});
