import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { makeRequest, requestFromCSV, makeFeedback, parseHistoryCSV, parseHour, toCSV } from "../workload/static/input.mjs";
import { buildLogQuery, eventSummary, formatEventTime, storageLabel } from "../workload/static/logs.mjs";

const example = JSON.parse(await readFile(new URL("../examples/request.json", import.meta.url), "utf8"));
const csv = toCSV(example.history);
const historyFor = target => Array.from({ length: 168 }, (_, index) => ({
  hour: new Date(parseHour(target).milliseconds - (168 - index) * 3600000).toISOString().slice(0, 19),
  count: index % 20,
}));

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

test("continuous CSV infers the next hour across year, leap-day, and minimum-year boundaries", () => {
  for (const target of ["2025-01-01T00:00:00", "2024-03-01T00:00:00", "0001-01-08T00:00:00"]) {
    const history = historyFor(target);
    assert.deepEqual(requestFromCSV(toCSV(history)), { target_hour: target, history });
  }
});

test("target inference rejects missing, duplicate, gapped, reversed and overflowing histories", () => {
  const history = example.history;
  for (const invalid of [history.slice(1), [history[0], ...history.slice(0, -1)], [...history].reverse(),
    [...history.slice(0, 50), ...history.slice(51), { hour: example.target_hour, count: 0 }]]) {
    assert.throws(() => requestFromCSV(toCSV(invalid)));
  }
  const last = historyFor("9999-12-31T23:00:00");
  assert.throws(() => requestFromCSV(toCSV([...last.slice(1), { hour: "9999-12-31T23:00:00", count: 0 }])));
});

test("a target-only mismatch explains the compatible hour instead of blaming CSV row 2", () => {
  assert.throws(() => makeRequest("2011-09-27T07:00:00", csv), error => {
    assert.match(error.message, /현재 이력에 맞는 예측 시각은 2011-09-27 06:00/);
    assert.doesNotMatch(error.message, /2행/);
    return true;
  });
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
  const receipt = (id, count, revision = count + 1, status = "local") => ({
    event: { prediction_id: id, revision, submission: { feedback: { kind: "actual_count", value: count } },
      actual_stage: count === 0 ? "idle" : "normal", is_correct: count === 0, recorded_at: "2026-10-26T08:00:00Z" },
    log_status: status,
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

    for (const succeeds of [true, false]) {
      await t.test(`lookup stays locked until feedback save ${succeeds ? "succeeds" : "fails"}`, async () => {
        element("feedback-count").value = "8";
        await element("feedback-form").emit("input");
        const pendingSave = element("feedback-form").emit("submit");
        const submission = take(`/predictions/${c}/feedback`);
        assert.equal(element("prediction-id").disabled, true);
        assert.equal(element("lookup-button").disabled, true);
        const beforeLookup = requests.length;
        await element("prediction-lookup").emit("submit");
        assert.equal(requests.length, beforeLookup, "A same-ID lookup must not replace the selection during a save");
        assert.equal(element("prediction-id").value, c);
        assert.equal(element("feedback-form").hidden, false);
        if (succeeds) submission.resolve(response(receipt(c, 8)));
        else submission.reject(new Error("network"));
        await pendingSave;
        assert.equal(element("prediction-id").disabled, false);
        assert.equal(element("lookup-button").disabled, false);
        if (succeeds) assert.match(element("feedback-result").textContent, /실제 8건/);
        else assert.equal(element("feedback-error").hidden, false);
      });
    }

    await t.test("late refresh cannot overwrite a newer revision or completed delivery", async () => {
      const oldRefresh = element("refresh-logs").emit("click");
      await settle();
      const oldRead = take(`/predictions/${c}`);
      const newRefresh = element("refresh-logs").emit("click");
      await settle();
      const newRead = take(`/predictions/${c}`);
      newRead.resolve(response({ ...detail(c, receipt(c, 13, 21, "sent")), log_status: "sent" }));
      await newRefresh;
      oldRead.resolve(response({ ...detail(c, receipt(c, 12, 20, "pending")), log_status: "pending" }));
      await oldRefresh;
      assert.match(element("feedback-result").textContent, /실제 13건.*실제 S3 저장 완료/);
      assert.equal(element("prediction-delivery").textContent, "실제 S3 저장 완료");
    });

    await t.test("a newer correction retires only the completed submission token", async () => {
      element("feedback-count").value = "14";
      await element("feedback-form").emit("input");
      const save = element("feedback-form").emit("submit");
      const first = take(`/predictions/${c}/feedback`);
      const firstId = JSON.parse(first.options.body).submission_id;
      first.resolve(response(receipt(c, 14, 22)));
      await save;
      const duplicateSave = element("feedback-form").emit("submit");
      const duplicate = take(`/predictions/${c}/feedback`);
      const duplicateId = JSON.parse(duplicate.options.body).submission_id;
      duplicate.resolve(response(receipt(c, 14, 22)));
      await duplicateSave;
      assert.equal(duplicateId, firstId, "An immediate unchanged resubmission remains idempotent");
      const refresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).resolve(response(detail(c, receipt(c, 15, 23))));
      await refresh;
      assert.match(element("feedback-result").textContent, /실제 15건/);
      assert.equal(element("feedback-count").value, "14", "Keep the user's unsaved form value");
      const correctionSave = element("feedback-form").emit("submit");
      const correction = take(`/predictions/${c}/feedback`);
      const correctionId = JSON.parse(correction.options.body).submission_id;
      correction.resolve(response(receipt(c, 14, correctionId === firstId ? 22 : 24)));
      await correctionSave;
      assert.notEqual(correctionId, firstId, "Saving after observing a newer revision must create a correction");
      assert.match(element("feedback-result").textContent, /실제 14건/);
    });

    await t.test("uncertain save retries keep their token without replacing a newer correction", async () => {
      element("feedback-count").value = "16";
      await element("feedback-form").emit("input");
      const lostSave = element("feedback-form").emit("submit");
      const lost = take(`/predictions/${c}/feedback`);
      const lostId = JSON.parse(lost.options.body).submission_id;
      lost.reject(new Error("response lost after server commit"));
      await lostSave;
      const refresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).resolve(response(detail(c, receipt(c, 17, 26))));
      await refresh;
      const retrySave = element("feedback-form").emit("submit");
      const retry = take(`/predictions/${c}/feedback`);
      const retryId = JSON.parse(retry.options.body).submission_id;
      retry.resolve(response(receipt(c, 16, 25)));
      await retrySave;
      assert.equal(retryId, lostId, "Unknown outcomes must retry the same request, even after another correction");
      assert.match(element("feedback-result").textContent, /실제 17건/, "The old idempotency receipt is not the latest truth");
      const correctionSave = element("feedback-form").emit("submit");
      const correction = take(`/predictions/${c}/feedback`);
      const correctionId = JSON.parse(correction.options.body).submission_id;
      correction.resolve(response(receipt(c, 16, 27)));
      await correctionSave;
      assert.notEqual(correctionId, lostId, "Once acknowledged, the obsolete request can be replaced");
      assert.match(element("feedback-result").textContent, /실제 16건/);
    });

    await t.test("a superseded refresh failure cannot cover a successful latest refresh", async () => {
      const oldRefresh = element("refresh-logs").emit("click");
      await settle();
      const oldRead = take(`/predictions/${c}`);
      const newRefresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).resolve(response(detail(c, receipt(c, 16, 27))));
      await newRefresh;
      oldRead.reject(new Error("late failure"));
      await oldRefresh;
      assert.equal(element("feedback-error").hidden, true);
      assert.match(element("feedback-result").textContent, /실제 16건/);
    });

    await t.test("a successful current refresh clears its preceding refresh error", async () => {
      const failedRefresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).reject(new Error("temporary read failure"));
      await failedRefresh;
      assert.equal(element("feedback-error").hidden, false);
      const recoveredRefresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).resolve(response(detail(c, receipt(c, 16, 27))));
      await recoveredRefresh;
      assert.equal(element("feedback-error").hidden, true);
      assert.match(element("feedback-result").textContent, /실제 16건/);
    });

    await t.test("refresh recovery preserves an uncertain save error and its retry token", async () => {
      element("feedback-count").value = "18";
      await element("feedback-form").emit("input");
      const failedSave = element("feedback-form").emit("submit");
      const lost = take(`/predictions/${c}/feedback`);
      const lostId = JSON.parse(lost.options.body).submission_id;
      lost.reject(new Error("unknown save outcome"));
      await failedSave;
      const saveError = element("feedback-error").textContent;
      const failedRefresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).resolve({ ok: false, status: 503 });
      await failedRefresh;
      assert.equal(element("feedback-error").textContent, saveError);
      const recoveredRefresh = element("refresh-logs").emit("click");
      await settle();
      take(`/predictions/${c}`).resolve(response(detail(c, receipt(c, 16, 27))));
      await recoveredRefresh;
      assert.equal(element("feedback-error").hidden, false);
      assert.equal(element("feedback-error").textContent, saveError);
      const retrySave = element("feedback-form").emit("submit");
      const retry = take(`/predictions/${c}/feedback`);
      const retryId = JSON.parse(retry.options.body).submission_id;
      retry.resolve(response(receipt(c, 18, 28)));
      await retrySave;
      assert.equal(retryId, lostId);
      assert.equal(element("feedback-error").hidden, true);
    });

    element("feedback-count").value = "2";
    await element("feedback-form").emit("input");
    const oldSave = element("feedback-form").emit("submit");
    const oldSubmission = take(`/predictions/${c}/feedback`);
    recordPrediction(forecast(a), "user");
    take(`/predictions/${a}`).resolve(response(detail(a, receipt(a, 0, 1))));
    oldSubmission.resolve(response(receipt(c, 2)));
    await oldSave;
    await settle();
    assert.equal(element("prediction-id").value, a);
    assert.doesNotMatch(element("feedback-result").textContent, /실제 2건/);
    assert.match(element("feedback-result").textContent, /실제 0건/, "A different prediction starts its own revision sequence");
    assert.equal(element("save-feedback").disabled, false);
    assert.equal(element("prediction-id").disabled, false);
    assert.equal(element("lookup-button").disabled, false);
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

test("forecast source requires an explicit real-data choice and sample loading resets it", async () => {
  // Exercise the actual event handlers with DOM/fetch doubles, without a browser or network.
  const previous = { document: globalThis.document, fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  const elements = new Map();
  const makeElement = () => ({
    value: "", textContent: "", hidden: false, disabled: false, style: {}, parentElement: {},
    listeners: new Map(),
    addEventListener(name, listener) { this.listeners.set(name, listener); },
    async emit(name, extra = {}) { await this.listeners.get(name)?.({ preventDefault() {}, target: this, ...extra }); },
    setAttribute() {}, append() {}, replaceChildren() {}, after() {},
  });
  const element = id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const requests = [];
  let lastResult;
  let pendingPrediction = null;
  const response = data => ({ ok: true, async json() { return data; } });
  try {
    globalThis.document = { getElementById: element, createElement: makeElement, createElementNS: makeElement };
    globalThis.localStorage = { getItem() { return null; }, setItem() {} };
    globalThis.fetch = async (path, options = {}) => {
      if (path === "/example") return response(example);
      if (path === "/health") return response({ status: "ready" });
      if (path === "/metadata") return response({ model_purpose: "historical_baseline" });
      if (path === "/logging") return response({ destination: "local", pending_events: 0 });
      if (path === "/predict") {
        requests.push(options);
        lastResult = { target_hour: JSON.parse(options.body).target_hour, workload_stage: "idle", probabilities: [1, 0, 0], model_version: "test", prediction_id: options.headers["Idempotency-Key"], log_status: "local" };
        if (pendingPrediction) await pendingPrediction;
        return response(lastResult);
      }
      if (path.startsWith("/predictions/")) return response({ event: { response: lastResult }, log_status: "local", latest_feedback: null });
      throw new Error(`Unexpected request: ${path}`);
    };
    element("data-source").value = "demo";
    element("feedback-kind").value = "actual_count";
    await import("../workload/static/app.mjs?unit-source");
    await element("load-example").emit("click");
    await element("history").emit("input");
    await element("forecast-form").emit("submit");
    assert.equal(requests.at(-1).headers["X-Data-Source"], "demo", "Editing a sample must not promote it to real data");

    const sampleKey = requests.at(-1).headers["Idempotency-Key"];
    element("data-source").value = "user";
    await element("data-source").emit("change");
    assert.equal(element("result").hidden, true);
    await element("forecast-form").emit("submit");
    assert.equal(requests.at(-1).headers["X-Data-Source"], "user");
    assert.notEqual(requests.at(-1).headers["Idempotency-Key"], sampleKey);

    await element("load-example").emit("click");
    assert.equal(element("data-source").value, "demo");
    element("target-hour").value = "2026-11-16T00:00";
    element("csv-file").files = [{ name: "downloaded-example.csv", size: csv.length, async text() { return csv; } }];
    await element("csv-file").emit("change");
    assert.equal(element("target-hour").value, "2011-09-27T06:00");
    assert.equal(element("history").value, csv, "Automatic target selection never rewrites the history");
    assert.match(element("target-help").textContent, /2011-09-20 06:00.*2011-09-27 05:00.*2011-09-27 06:00/);
    await element("forecast-form").emit("submit");
    assert.equal(requests.at(-1).headers["X-Data-Source"], "demo", "Reuploading the sample preserves the explicit demo choice");
    assert.match(element("result-source").textContent, /실습/);
    element("target-hour").value = "2011-09-27T07:00";
    await element("target-hour").emit("input");
    assert.match(element("input-status").textContent, /현재 이력에 맞는 예측 시각은 2011-09-27 06:00/);
    const beforeInvalid = requests.length;
    await element("forecast-form").emit("submit");
    assert.equal(requests.length, beforeInvalid);
    assert.equal(element("form-error").hidden, true, "Local validation uses one error location");
    element("data-source").value = "user";
    await element("data-source").emit("change");
    for (const invalid of [toCSV(example.history.slice(1)), toCSV([example.history[0], ...example.history.slice(0, -1)])]) {
      element("csv-file").files = [{ name: "invalid.csv", size: invalid.length, async text() { return invalid; } }];
      await element("csv-file").emit("change");
      assert.equal(element("target-hour").value, "");
      assert.equal(element("input-status").className, "input-status invalid");
      assert.doesNotMatch(element("target-help").textContent, /예측 가능한 시각/);
      await element("forecast-form").emit("submit");
      assert.equal(requests.length, beforeInvalid);
    }
    const shifted = toCSV(historyFor("2025-01-01T00:00:00"));
    element("csv-file").files = [{ name: "new-period.csv", size: shifted.length, async text() { return shifted; } }];
    await element("csv-file").emit("change");
    assert.equal(element("target-hour").value, "2025-01-01T00:00");
    assert.equal(element("history").value, shifted);
    assert.equal(element("data-source").value, "user");
    await element("forecast-form").emit("submit");
    assert.equal(requests.at(-1).headers["X-Data-Source"], "user");
    assert.equal(JSON.parse(requests.at(-1).body).target_hour, "2025-01-01T00:00:00");

    const extendedHistory = [...example.history,
      { hour: example.target_hour, count: 666 },
      { hour: "2011-09-27T07:00:00", count: 777 }];
    const extendedCSV = toCSV(extendedHistory);
    element("data-source").value = "demo";
    await element("data-source").emit("change");
    element("csv-file").files = [{ name: "full-history.csv", size: extendedCSV.length, async text() { return extendedCSV; } }];
    await element("csv-file").emit("change");
    assert.equal(element("target-hour").value, "2011-09-27T06:00", "A large file starts at its first usable target so next-hour browsing is available");
    assert.equal(element("target-hour").min, "2011-09-27T06:00");
    assert.equal(element("target-hour").max, "2011-09-27T08:00");
    assert.equal(element("previous-hour").disabled, true);
    assert.equal(element("next-hour").disabled, false);
    assert.equal(element("result-empty-title").textContent, "예측 대기");
    await element("forecast-form").emit("submit");
    assert.deepEqual(JSON.parse(requests.at(-1).body).history, example.history, "Target/future rows in the full CSV never leak into this prediction");
    assert.equal(element("result-stage").textContent, "0건");
    assert.match(element("result-action").textContent, /정상 예측.*입력 데이터가 부족하다는 뜻이 아닙니다/);
    const firstWindowKey = requests.at(-1).headers["Idempotency-Key"];
    await element("next-hour").emit("click");
    assert.equal(element("target-hour").value, "2011-09-27T07:00");
    assert.equal(element("history").value, extendedCSV);
    assert.equal(element("result").hidden, true);
    assert.equal(element("result-empty-title").textContent, "예측 대기");
    await element("forecast-form").emit("submit");
    assert.deepEqual(JSON.parse(requests.at(-1).body).history, extendedHistory.slice(1, 169));
    assert.notEqual(requests.at(-1).headers["Idempotency-Key"], firstWindowKey);
    assert.equal(requests.at(-1).headers["X-Data-Source"], "demo");
    await element("latest-hour").emit("click");
    assert.equal(element("target-hour").value, "2011-09-27T08:00");
    assert.equal(element("next-hour").disabled, true);
    assert.equal(element("latest-hour").disabled, true);
    assert.match(element("navigation-note").textContent, /2011-09-27 08:00.*실제 집계/);
    await element("next-hour").emit("click");
    assert.equal(element("target-hour").value, "2011-09-27T08:00", "Boundary buttons cannot manufacture the next observation");
    await element("forecast-form").emit("submit");
    assert.deepEqual(JSON.parse(requests.at(-1).body).history, extendedHistory.slice(2));
    await element("previous-hour").emit("click");
    assert.equal(element("target-hour").value, "2011-09-27T07:00");
    assert.equal(element("history").value, extendedCSV, "Navigation must preserve every uploaded row");
    let releasePrediction;
    pendingPrediction = new Promise(resolve => { releasePrediction = resolve; });
    const pendingSubmit = element("forecast-form").emit("submit");
    assert.equal(element("previous-hour").disabled, true);
    assert.equal(element("next-hour").disabled, true);
    assert.equal(element("latest-hour").disabled, true);
    await element("next-hour").emit("click");
    assert.equal(element("target-hour").value, "2011-09-27T07:00", "A pending forecast cannot be retargeted by navigation");
    releasePrediction();
    await pendingSubmit;
    pendingPrediction = null;
    assert.equal(element("previous-hour").disabled, false);
    assert.equal(element("next-hour").disabled, false);
    element("target-hour").value = "2011-09-27T09:00";
    await element("target-hour").emit("input");
    assert.equal(element("result-empty-title").textContent, "입력 확인 필요");
    assert.match(element("input-status").textContent, /데이터 부족/);
    const beforeOutsideRange = requests.length;
    await element("forecast-form").emit("submit");
    assert.equal(requests.length, beforeOutsideRange);
    await element("load-example").emit("click");
    assert.equal(element("previous-hour").disabled, true);
    assert.equal(element("next-hour").disabled, true);
    assert.match(element("navigation-note").textContent, /168행 파일은 한 시각만/);
  } finally {
    await new Promise(resolve => setImmediate(resolve));
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
});
