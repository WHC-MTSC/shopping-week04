import { makeRequest, inspectHistoryCSV, compareCSVActual, toCSV, HOUR_MS, MAX_FILE_BYTES, parseHour } from "./input.mjs";
import { recordPrediction } from "./feedback.mjs";

const $ = id => document.getElementById(id);
const targetInput = $("target-hour");
const historyInput = $("history");
const form = $("forecast-form");
const sourceInput = $("data-source");
let source = "demo";
let revision = 0;
let busy = false;
let predictionKey = null;
let availableHistory = null;
const stages = [
  { id: "idle", name: "없음", range: "판매전표 없음 단계", action: "정상 예측 결과입니다. 모델이 판매전표 0건 구간으로 판단했으며, 입력 데이터가 부족하다는 뜻이 아닙니다." },
  { id: "normal", name: "보통", range: "판매전표 1–9건", action: "판매전표 1~9건 구간으로 예측했습니다." },
  { id: "busy", name: "혼잡", range: "판매전표 10건 이상", action: "판매전표 10건 이상 구간으로 예측했습니다." },
];

function showError(message = "") {
  $("form-error").textContent = message;
  $("form-error").hidden = !message;
}

function setBusy(value) {
  busy = value;
  for (const control of [$("predict-button"), $("load-example"), $("csv-file"), sourceInput, targetInput, historyInput]) control.disabled = value;
  form.setAttribute("aria-busy", String(value));
  updateNavigation();
}

function invalidateResult() {
  revision += 1;
  predictionKey = null;
  $("result").hidden = true;
  $("result-empty").hidden = false;
  $("result-empty-title").textContent = "예측 대기";
  $("result-empty-message").textContent = "선택한 시각으로 예측 실행을 눌러주세요.";
  showError();
}

const displayHour = hour => hour.slice(0, 16).replace("T", " ");

function updateNavigation() {
  let current = null;
  try { current = parseHour(targetInput.value).milliseconds; } catch { /* The input is incomplete. */ }
  const first = availableHistory ? parseHour(availableHistory.first_target_hour).milliseconds : null;
  const last = availableHistory ? parseHour(availableHistory.last_target_hour).milliseconds : null;
  const valid = current !== null && first !== null && current >= first && current <= last;
  $("previous-hour").disabled = busy || !valid || current <= first;
  $("next-hour").disabled = busy || !valid || current >= last;
  $("latest-hour").disabled = busy || !availableHistory || current === last;
  if (!availableHistory) $("navigation-note").textContent = "CSV를 불러오면 가능한 시간 안에서 이동할 수 있습니다.";
  else if (first === last) $("navigation-note").textContent = "168행 파일은 한 시각만 예측할 수 있습니다. 여러 시간을 보려면 전체 CSV 예시를 불러오세요.";
  else if (current === last) $("navigation-note").textContent = `마지막 예측 가능 시각입니다. 다음 시간에는 ${displayHour(availableHistory.last_target_hour)}의 실제 집계를 CSV에 추가해야 합니다.`;
  else $("navigation-note").textContent = `총 ${availableHistory.history.length - 167}개 시각을 선택할 수 있습니다. 시간을 옮긴 뒤 예측 실행을 누르세요.`;
}

function checkInput({ alignTarget = false } = {}) {
  const status = $("input-status");
  availableHistory = null;
  targetInput.min = "0001-01-08T00:00";
  targetInput.max = "9999-12-31T23:00";
  $("target-help").textContent = "전체 CSV에서 선택한 시각 직전 168시간을 자동으로 추출합니다.";
  try {
    const inferred = inspectHistoryCSV(historyInput.value);
    availableHistory = inferred;
    if (alignTarget) targetInput.value = inferred.first_target_hour.slice(0, 16);
    targetInput.min = inferred.first_target_hour.slice(0, 16);
    targetInput.max = inferred.last_target_hour.slice(0, 16);
    $("target-help").textContent = `전체 ${inferred.history.length.toLocaleString("ko-KR")}행: ${displayHour(inferred.history[0].hour)} ~ ${displayHour(inferred.history.at(-1).hour)} · 예측 가능한 시각: ${displayHour(inferred.first_target_hour)} ~ ${displayHour(inferred.last_target_hour)} (원본 현지 시각).`;
    const request = makeRequest(targetInput.value, historyInput.value);
    status.textContent = `검증 완료 · 전체 ${inferred.history.length.toLocaleString("ko-KR")}행 중 직전 168시간 사용 (${displayHour(request.history[0].hour)} ~ ${displayHour(request.history.at(-1).hour)})`;
    status.className = "input-status valid";
    if ($("result").hidden) {
      $("result-empty-title").textContent = "예측 대기";
      $("result-empty-message").textContent = "선택한 시각으로 예측 실행을 눌러주세요.";
    }
    renderChart(request.history);
    return request;
  } catch (error) {
    status.textContent = historyInput.value ? error.message : "데이터 없음 · 샘플 또는 CSV 선택";
    status.className = `input-status ${historyInput.value ? "invalid" : ""}`;
    $("chart-empty").hidden = false;
    $("history-chart").hidden = true;
    $("history-summary").textContent = "유효한 이력 필요 · 168시간";
    $("result-empty-title").textContent = historyInput.value ? "입력 확인 필요" : "CSV 입력 대기";
    $("result-empty-message").textContent = "아직 예측한 결과가 아닙니다. 왼쪽 입력 안내를 확인하세요.";
    return null;
  } finally {
    updateNavigation();
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
  $("result-stage").textContent = selected.id === "idle" ? "0건" : selected.name;
  $("result-range").textContent = selected.range;
  $("result-action").textContent = selected.action;
  const comparison = compareCSVActual(availableHistory.history, result.target_hour, result.workload_stage);
  if (comparison) {
    const actual = stages.find(stage => stage.id === comparison.actualStage);
    $("actual-comparison-title").textContent = comparison.matches ? "예측 일치" : "예측 불일치";
    $("actual-comparison-value").textContent = `CSV 실제값 ${comparison.count.toLocaleString("ko-KR")}건 · ${actual.name} / 예측 ${selected.name}`;
    $("actual-comparison-note").textContent = "업로드한 CSV에 기록된 값으로 비교했습니다. 정답 로그로 남기려면 아래에서 확인 후 저장하세요.";
  } else {
    $("actual-comparison-title").textContent = "실제값 없음";
    $("actual-comparison-value").textContent = "선택한 시각의 실제 건수가 CSV에 없습니다.";
    $("actual-comparison-note").textContent = "실제 결과를 알게 되면 아래에서 직접 입력해 저장하세요.";
  }
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
  $("result-source").textContent = source === "demo" ? "실습·테스트 예측" : "확인된 실제 집계 예측";
  $("result-version").textContent = `${result.model_version.startsWith("practice-") ? "과거 데이터 실습 모델" : "모델"} · ${result.model_version}`;
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
    sourceInput.value = "demo";
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
  targetInput.value = "";
  source = sourceInput.value;
  $("source-badge").textContent = source === "demo" ? "CSV · 실습" : "CSV · 실제 집계";
  $("file-name").textContent = file.name;
  checkInput();
  try {
    if (file.size > MAX_FILE_BYTES) throw new Error("CSV 파일은 1 MB 이하여야 합니다.");
    historyInput.value = await file.text();
    checkInput({ alignTarget: true });
  } catch (error) { showError(error.message); }
  finally { event.target.value = ""; setBusy(false); }
});

function targetChanged() {
  invalidateResult();
  source = sourceInput.value;
  checkInput();
}

targetInput.addEventListener("input", targetChanged);
historyInput.addEventListener("input", () => {
  invalidateResult();
  source = sourceInput.value;
  $("source-badge").textContent = source === "demo" ? "직접 입력 · 실습" : "직접 입력 · 실제 집계";
  $("file-name").textContent = "직접 수정한 데이터";
  checkInput();
});

for (const [id, direction] of [["previous-hour", -1], ["next-hour", 1], ["latest-hour", 0]]) {
  $(id).addEventListener("click", () => {
    if (busy || !availableHistory || $(id).disabled) return;
    const next = direction === 0 ? availableHistory.last_target_hour : new Date(parseHour(targetInput.value).milliseconds + direction * HOUR_MS).toISOString().slice(0, 19);
    targetInput.value = next.slice(0, 16);
    targetChanged();
  });
}

sourceInput.addEventListener("change", () => {
  invalidateResult();
  source = sourceInput.value;
  $("source-badge").textContent = source === "demo" ? "실습·테스트" : "확인된 실제 집계";
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  $("result").hidden = true;
  $("result-empty").hidden = false;
  showError();
  const submittedRevision = revision;
  try {
    const payload = checkInput();
    if (!payload) return;
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
  const anchor = document.createElement("a");
  anchor.href = "/example/history.csv";
  anchor.download = "hourahead-full-history.csv";
  anchor.click();
});

async function checkServer() {
  try {
    const [health, metadata] = await Promise.all([fetchJSON("/health"), fetchJSON("/metadata")]);
    if (health.status !== "ready") throw new Error("모델 준비 중");
    $("server-status").textContent = metadata.model_purpose === "historical_replay" ? "과거 데이터 실습 모델 연결됨" : "예측 모델 연결됨";
    $("server-status").className = "connection ready";
    if (metadata.model_purpose === "historical_replay") {
      const notice = document.createElement("div");
      notice.className = "context-strip";
      notice.setAttribute("role", "status");
      const label = document.createElement("span");
      label.className = "context-label";
      label.textContent = "재학습 실습";
      const description = document.createElement("p");
      description.textContent = `${metadata.model_notice} 모델 버전: ${metadata.model_version}`;
      notice.append(label, description);
      document.querySelector(".page-heading").after(notice);
    }
  } catch {
    $("server-status").textContent = "연결 실패 · 예측 시 재시도";
    $("server-status").className = "connection failed";
  }
}
await checkServer();
