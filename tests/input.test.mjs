import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { makeRequest, makeFeedback, parseHistoryCSV, parseHour, toCSV } from "../workload/static/input.mjs";
import { buildLogQuery, eventSummary, formatEventTime, storageLabel } from "../workload/static/logs.mjs";

const example = JSON.parse(await readFile(new URL("../examples/request.json", import.meta.url), "utf8"));
const csv = toCSV(example.history);

test("sample CSV round-trip preserves every timestamp and count", () => {
  assert.deepEqual(makeRequest(example.target_hour, csv), example);
  assert.deepEqual(makeRequest(example.target_hour.slice(0, 16), "\uFEFF" + csv.replaceAll("\n", "\r\n")), example);
});

test("timezone-free arithmetic preserves midnight and year boundaries", () => {
  for (const value of ["0001-01-08T00:00:00", "2011-12-31T23:00:00", "2024-02-29T00:00:00"]) {
    assert.equal(parseHour(value).normalized, value);
  }
  assert.equal(parseHour("2011-09-27 06:00:00").normalized, example.target_hour);
});

for (const invalid of ["2011-09-27T06:00:00Z", "2011-09-27T06:00:00+09:00", "2011-09-27T06:30:00", "2011-02-29T00:00:00", "2011-02-30T00:00:00", "2011-09-27T24:00:00", "0000-01-08T00:00:00", "invalid"]) {
  test(`reject invalid or timezone-bearing timestamp: ${invalid}`, () => {
    assert.throws(() => parseHour(invalid));
  });
}

for (const count of ["-1", "1.5", "NaN", "Infinity", "true", "100001", '"2"', "", "1e2"]) {
  test(`reject malformed count: ${count}`, () => {
    assert.throws(() => parseHistoryCSV(csv.replace(",0\n", `,${count}\n`)));
  });
}

test("accept count boundaries without coercing nonnumeric values", () => {
  const rows = parseHistoryCSV(csv.replace(",0\n", ",100000\n"));
  assert.equal(rows[0].count, 100000);
});

test("reject missing, duplicate, reversed and target-hour history", () => {
  assert.throws(() => makeRequest(example.target_hour, toCSV(example.history.slice(1))));
  assert.throws(() => makeRequest(example.target_hour, toCSV([...example.history, example.history[0]])));
  assert.throws(() => makeRequest(example.target_hour, toCSV([...example.history].reverse())));
  assert.throws(() => makeRequest(example.target_hour, toCSV([example.history[1], ...example.history.slice(1)])));
  assert.throws(() => makeRequest(example.target_hour, toCSV([...example.history.slice(1), { hour: example.target_hour, count: 1 }])));
});

test("reject wrong columns, extra fields, blank rows and oversized input", () => {
  assert.throws(() => parseHistoryCSV(csv.replace("hour,count", "date,count")));
  assert.throws(() => parseHistoryCSV(csv.replace(",0\n", ",0,extra\n")));
  assert.throws(() => parseHistoryCSV(csv.replace(",0\n", ",0\n\n")));
  assert.throws(() => parseHistoryCSV("x".repeat(1024 * 1024 + 1)));
});

test("feedback preserves false separately from zero and actual stage", () => {
  assert.deepEqual(makeFeedback("correctness", "false"), { kind: "correctness", value: false });
  assert.deepEqual(makeFeedback("correctness", "true"), { kind: "correctness", value: true });
  assert.deepEqual(makeFeedback("actual_count", "0"), { kind: "actual_count", value: 0 });
  assert.deepEqual(makeFeedback("actual_stage", "busy"), { kind: "actual_stage", value: "busy" });
  for (const [kind, value] of [["correctness", "0"], ["actual_count", "false"], ["actual_count", "-1"], ["actual_count", "1.5"], ["actual_count", "100001"], ["actual_stage", "unknown"], ["actual_count", ""]]) {
    assert.throws(() => makeFeedback(kind, value));
  }
});

test("feedback selection and delayed responses preserve the active prediction's truth", async t => {
  // Unit-level DOM/event doubles only: no browser, server, or network is involved.
  const previous = { document: globalThis.document, fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", hidden: false, disabled: false, parentElement: { hidden: false },
      listeners: new Map(),
      addEventListener(name, listener) { this.listeners.set(name, listener); },
      async emit(name) { await this.listeners.get(name)?.({ preventDefault() {} }); },
    });
    return elements.get(id);
  };
  const pending = [];
  const requests = [];
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const take = path => {
    const index = pending.findIndex(item => item.path === path);
    assert.notEqual(index, -1, `Expected a pending request for ${path}`);
    return pending.splice(index, 1)[0];
  };
  const response = data => ({ ok: true, async json() { return data; } });
  const a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const c = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const forecast = id => ({ prediction_id: id, target_hour: "2011-09-27T06:00:00", workload_stage: "idle", log_status: "local" });
  const receipt = (id, count) => ({
    event: { prediction_id: id, submission: { feedback: { kind: "actual_count", value: count } },
      actual_stage: count === 0 ? "idle" : "normal", is_correct: count === 0, recorded_at: "2026-10-26T08:00:00Z" },
    log_status: "local",
  });
  const detail = (id, latest = null) => ({
    event: { prediction_id: id, response: forecast(id), data_source: "user" },
    log_status: "local", latest_feedback: latest,
  });
  try {
    globalThis.document = { getElementById: element };
    globalThis.localStorage = { getItem() { return null; }, setItem() {} };
    globalThis.fetch = (path, options = {}) => {
      requests.push({ path, options });
      if (path === "/logging") return Promise.resolve(response({ destination: "local", pending_events: 1 }));
      return new Promise((resolve, reject) => pending.push({ path, options, resolve, reject }));
    };
    element("feedback-kind").value = "actual_count";
    const { recordPrediction } = await import("../workload/static/feedback.mjs?unit-selection");

    recordPrediction(forecast(a), "user");
    const oldRead = take(`/predictions/${a}`);
    element("prediction-id").value = b;
    await element("prediction-id").emit("input");
    await element("feedback-form").emit("submit");
    assert.equal(requests.some(item => item.options.method === "POST"), false);
    assert.equal(element("feedback-form").hidden, true);
    oldRead.resolve(response(detail(a, receipt(a, 0))));
    await settle();
    assert.equal(element("feedback-form").hidden, true);
    assert.equal(element("prediction-id").value, b);

    const oldLookup = element("prediction-lookup").emit("submit");
    const lookupRead = take(`/predictions/${b}`);
    recordPrediction(forecast(c), "user");
    take(`/predictions/${c}`).resolve(response(detail(c)));
    lookupRead.reject(new Error("network"));
    await oldLookup;
    await settle();
    assert.equal(element("prediction-id").value, c);
    assert.equal(element("feedback-error").hidden, true);

    element("feedback-count").value = "5";
    recordPrediction(forecast(c), "user");
    const retryRead = take(`/predictions/${c}`);
    assert.equal(element("feedback-count").value, "5", "Prediction retry must preserve an unsaved answer");
    const save = element("feedback-form").emit("submit");
    const savedRequest = take(`/predictions/${c}/feedback`);
    savedRequest.resolve(response(receipt(c, 5)));
    await save;
    retryRead.resolve(response(detail(c, receipt(c, 0))));
    await settle();
    assert.match(element("feedback-result").textContent, /실제 5건/);
    assert.doesNotMatch(element("feedback-result").textContent, /실제 0건/);

    for (const [readFirst, count] of [[true, 6], [false, 7]]) {
      await t.test(`same prediction retry during feedback save, read ${readFirst ? "before" : "after"} receipt`, async () => {
        element("feedback-count").value = String(count);
        await element("feedback-form").emit("input");
        const pendingSave = element("feedback-form").emit("submit");
        const submission = take(`/predictions/${c}/feedback`);
        recordPrediction(forecast(c), "user");
        const repeatedRead = take(`/predictions/${c}`);
        if (readFirst) {
          repeatedRead.resolve(response(detail(c, receipt(c, 5))));
          await settle();
        }
        submission.resolve(response(receipt(c, count)));
        await pendingSave;
        if (!readFirst) repeatedRead.resolve(response(detail(c, receipt(c, 5))));
        await settle();
        assert.match(element("feedback-result").textContent, new RegExp(`실제 ${count}건`));
        assert.equal(element("feedback-count").value, String(count));
        assert.equal(element("save-feedback").disabled, false);
      });
    }

    element("feedback-count").value = "2";
    await element("feedback-form").emit("input");
    const oldSave = element("feedback-form").emit("submit");
    const oldSubmission = take(`/predictions/${c}/feedback`);
    recordPrediction(forecast(a), "user");
    take(`/predictions/${a}`).resolve(response(detail(a)));
    oldSubmission.resolve(response(receipt(c, 2)));
    await oldSave;
    await settle();
    assert.equal(element("prediction-id").value, a);
    assert.doesNotMatch(element("feedback-result").textContent, /실제 2건/);
    assert.equal(element("save-feedback").disabled, false);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});

test("log console distinguishes simulated and actual storage", () => {
  assert.equal(storageLabel("simulated"), "로컬 S3 모사 저장 완료");
  assert.equal(storageLabel("sent"), "실제 S3 저장 완료");
  assert.equal(storageLabel("pending"), "저장 대기");
  assert.equal(formatEventTime("2026-10-26T17:00:00+09:00"), "2026-10-26 08:00:00.000");
});

test("log summaries preserve false versus zero and stage feedback", () => {
  assert.match(eventSummary({ event_type: "prediction", response: { workload_stage: "busy" }, request: { target_hour: "2011-09-27T06:00:00" } }), /혼잡/);
  assert.match(eventSummary({ event_type: "feedback", submission: { feedback: { kind: "correctness", value: false } } }), /틀림 \(F\).*실제값 미입력/);
  assert.match(eventSummary({ event_type: "feedback", submission: { feedback: { kind: "actual_count", value: 0 } }, actual_stage: "idle" }), /실제 0건.*없음/);
  assert.match(eventSummary({ event_type: "feedback", submission: { feedback: { kind: "actual_stage", value: "normal" } } }), /실제 보통/);
});

test("log filters encode user text and preserve the pagination cursor", () => {
  const url = new URL(buildLogQuery({ event_type: "feedback", prediction_id: " id ", query: " actual_count & value=0 " }, 42), "http://test.invalid");
  assert.equal(url.pathname, "/logs");
  assert.equal(url.searchParams.get("query"), "actual_count & value=0");
  assert.equal(url.searchParams.get("event_type"), "feedback");
  assert.equal(url.searchParams.get("prediction_id"), "id");
  assert.equal(url.searchParams.get("before"), "42");
  assert.equal(url.searchParams.get("limit"), "50");
  assert.equal(new URL(buildLogQuery(), "http://test.invalid").searchParams.has("before"), false);
});
