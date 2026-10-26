import { makeRequest, toCSV, HOUR_MS, MAX_FILE_BYTES, parseHour } from "./input.mjs";
import { recordPrediction } from "./feedback.mjs";

const $ = id => document.getElementById(id);
const targetInput = $("target-hour");
const historyInput = $("history");
const form = $("forecast-form");
let source = "user";
let revision = 0;
let busy = false;
let predictionKey = null;
const stages = [
  { id: "idle", name: "없음", range: "판매전표 0건", action: "판매전표가 없는 구간으로 예측했습니다." },
  { id: "normal", name: "보통", range: "판매전표 1–9건", action: "판매전표 1~9건 구간으로 예측했습니다." },
  { id: "busy", name: "혼잡", range: "판매전표 10건 이상", action: "판매전표 10건 이상 구간으로 예측했습니다." },
];

function showError(message = "") {
  $("form-error").textContent = message;
  $("form-error").hidden = !message;
}

function setBusy(value) {
  busy = value;
  for (const control of [$("predict-button"), $("load-example"), $("csv-file"), targetInput, historyInput]) control.disabled = value;
  form.setAttribute("aria-busy", String(value));
}

function invalidateResult() {
  revision += 1;
  predictionKey = null;
  $("result").hidden = true;
  $("result-empty").hidden = false;
  showError();
}

function checkInput() {
  const status = $("input-status");
  try {
    const request = makeRequest(targetInput.value, historyInput.value);
    status.textContent = "검증 완료 · 연속 168시간";
    status.className = "input-status valid";
    renderChart(request.history);
    return request;
  } catch (error) {
    status.textContent = historyInput.value ? error.message : "데이터 없음 · 샘플 또는 CSV 선택";
    status.className = `input-status ${historyInput.value ? "invalid" : ""}`;
    $("chart-empty").hidden = false;
    $("history-chart").hidden = true;
    $("history-summary").textContent = "유효한 이력 필요 · 168시간";
    return null;
  }
}

async function fetchJSON(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, signal: AbortSignal.timeout(30000) });
  } catch {
    throw new Error("서버에 연결하지 못했거나 응답이 지연되고 있습니다. 서버 실행 상태를 확인하고 다시 시도하세요.");
  }
  if (!response.ok) {
    if (response.status === 422) {
      throw new Error("입력 형식을 확인하세요. 168시간의 연속된 실제 건수와 시간대 없는 정각이 필요합니다.");
    }
    throw new Error(`요청을 처리하지 못했습니다 (${response.status}). 잠시 후 다시 시도하세요.`);
  }
  return response.json();
}

function renderResult(result) {
  const selected = stages.find(stage => stage.id === result.workload_stage);
  if (!selected || !Array.isArray(result.probabilities) || result.probabilities.length !== 3) {
    throw new Error("예측 결과 형식이 올바르지 않습니다. API 버전을 확인하세요.");
  }
  const start = parseHour(result.target_hour).milliseconds;
  const end = new Date(start + HOUR_MS).toISOString().slice(0, 16).replace("T", " ");
  $("result-period").textContent = `${result.target_hour.slice(0, 16).replace("T", " ")} → ${end} · 원본 현지 시각`;
  $("result-stage").textContent = selected.name;
  $("result-range").textContent = selected.range;
  $("result-action").textContent = selected.action;
  $("probability-bars").replaceChildren();
  stages.forEach((stage, index) => {
    const row = document.createElement("div");
    row.className = `probability-row${stage.id === selected.id ? " selected" : ""}`;
    const label = document.createElement("span");
    label.textContent = stage.name;
    const track = document.createElement("div");
    track.className = "probability-track";
    track.setAttribute("aria-hidden", "true");
    const fill = document.createElement("span");
    fill.className = "probability-fill";
    const percentage = result.probabilities[index] * 100;
    fill.style.width = `${Math.max(0, Math.min(100, percentage))}%`;
    track.append(fill);
    const value = document.createElement("strong");
    value.textContent = `${percentage.toFixed(1)}%`;
    row.append(label, track, value);
    $("probability-bars").append(row);
  });
  $("result-source").textContent = source === "demo" ? "과거 샘플 예측" : "입력한 데이터 예측";
  $("result-version").textContent = `모델 · ${result.model_version}`;
  $("result-empty").hidden = true;
  $("result").hidden = false;
  recordPrediction(result, source);
}

function renderChart(history) {
  const counts = history.map(row => row.count);
  const width = 960, left = Math.max(...counts) >= 1000 ? 58 : 34, right = 12, top = 12, height = 96;
  const peak = Math.max(1, ...counts);
  const ceiling = peak <= 5 ? 5 : Math.ceil(peak / 5) * 5;
  const step = (width - left - right) / history.length;
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} 138`);
  svg.setAttribute("class", "history-chart-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `직전 168시간 판매전표. 총 ${counts.reduce((a, b) => a + b, 0)}건, 시간당 최대 ${Math.max(...counts)}건.`);
  const add = (type, attributes, text) => {
    const element = document.createElementNS(namespace, type);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    if (text !== undefined) element.textContent = text;
    svg.append(element);
    return element;
  };
  for (const amount of [0, ceiling / 2, ceiling]) {
    const y = top + height - amount / ceiling * height;
    add("line", { x1: left, x2: width - right, y1: y, y2: y, class: "grid" });
    add("text", { x: left - 7, y: y + 3, "text-anchor": "end" }, amount.toLocaleString("ko-KR"));
  }
  history.forEach((row, index) => {
    const barHeight = row.count / ceiling * height;
    const bar = add("rect", { x: left + index * step, y: top + height - barHeight, width: Math.max(1, step - 1.9), height: barHeight, rx: 1.5, class: `bar${index >= 144 ? " recent" : ""}` });
    const title = document.createElementNS(namespace, "title");
    title.textContent = `${row.hour.replace("T", " ")} · ${row.count}건`;
    bar.append(title);
    if (index % 24 === 0) add("text", { x: left + index * step, y: 130 }, row.hour.slice(5, 10).replace("-", "/"));
  });
  const wrapper = document.createElement("div");
  wrapper.className = "chart-scroll";
  wrapper.append(svg);
  const caption = document.createElement("p");
  caption.className = "chart-caption";
  caption.textContent = "단위: 건 / 검정 막대: 최근 24시간 / 좁은 화면에서 가로 스크롤";
  $("history-chart").replaceChildren(wrapper, caption);
  $("history-chart").hidden = false;
  $("chart-empty").hidden = true;
  $("history-summary").textContent = `총 ${counts.reduce((a, b) => a + b, 0).toLocaleString("ko-KR")}건 · 시간당 최대 ${Math.max(...counts)}건`;
}

$("load-example").addEventListener("click", async () => {
  if (busy) return;
  setBusy(true);
  invalidateResult();
  try {
    const example = await fetchJSON("/example");
    targetInput.value = example.target_hour.slice(0, 16);
    historyInput.value = toCSV(example.history);
    source = "demo";
    $("source-badge").textContent = "과거 샘플";
    $("file-name").textContent = "샘플 이력 · 2011년 영국 소매 데이터";
    $("csv-file").value = "";
    checkInput();
  } catch (error) { showError(error.message); }
  finally { setBusy(false); }
});

$("csv-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (!file) return;
  invalidateResult();
  setBusy(true);
  historyInput.value = "";
  source = "user";
  $("source-badge").textContent = "CSV 입력";
  $("file-name").textContent = file.name;
  checkInput();
  try {
    if (file.size > MAX_FILE_BYTES) throw new Error("CSV 파일은 1 MB 이하여야 합니다.");
    historyInput.value = await file.text();
    checkInput();
  } catch (error) { showError(error.message); }
  finally { event.target.value = ""; setBusy(false); }
});

for (const input of [targetInput, historyInput]) input.addEventListener("input", () => {
  invalidateResult();
  source = "user";
  $("source-badge").textContent = "직접 입력";
  $("file-name").textContent = "직접 수정한 데이터";
  checkInput();
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  $("result").hidden = true;
  $("result-empty").hidden = false;
  showError();
  const submittedRevision = revision;
  try {
    const payload = makeRequest(targetInput.value, historyInput.value);
    setBusy(true);
    $("predict-button").textContent = "예측 중…";
    predictionKey ||= crypto.randomUUID();
    const result = await fetchJSON("/predict", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": predictionKey, "X-Data-Source": source }, body: JSON.stringify(payload) });
    $("server-status").textContent = "예측 모델 연결됨";
    $("server-status").className = "connection ready";
    if (revision === submittedRevision) renderResult(result);
  } catch (error) { showError(error.message); }
  finally {
    setBusy(false);
    $("predict-button").textContent = "예측 실행 →";
  }
});

$("download-example").addEventListener("click", async () => {
  const button = $("download-example");
  button.disabled = true;
  try {
    const example = await fetchJSON("/example");
    const url = URL.createObjectURL(new Blob(["\uFEFF", toCSV(example.history)], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `history-for-${example.target_hour.slice(0, 13).replace("T", "-")}.csv`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; }
});

async function checkServer() {
  try {
    const health = await fetchJSON("/health");
    if (health.status !== "ready") throw new Error("모델 준비 중");
    $("server-status").textContent = "예측 모델 연결됨";
    $("server-status").className = "connection ready";
  } catch {
    $("server-status").textContent = "연결 실패 · 예측 시 재시도";
    $("server-status").className = "connection failed";
  }
}
await checkServer();
