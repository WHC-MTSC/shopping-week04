import { makeFeedback } from "./input.mjs";

const $ = id => document.getElementById(id);
const stageNames = { idle: "없음", normal: "보통", busy: "혼잡" };
const deliveryNames = { local: "로컬 저장 완료 · S3 미연결", pending: "로컬 저장 완료 · S3 전송 대기", sent: "실제 S3 저장 완료", simulated: "로컬 S3 모사 저장 완료" };
let activeId = null;
let submissionId = null;
let lookupVersion = 0;
let feedbackVersion = 0;
let logVersion = 0;
let saving = false;

async function request(path, options = {}) {
  let response;
  try { response = await fetch(path, { ...options, signal: AbortSignal.timeout(30000) }); }
  catch { throw new Error("서버 응답을 확인하지 못했습니다. 같은 입력으로 다시 저장하면 중복 기록되지 않습니다."); }
  if (!response.ok) {
    const messages = { 404: "해당 예측 ID를 찾지 못했습니다.", 409: "같은 요청 ID에 다른 내용이 있습니다. 입력을 수정한 뒤 다시 저장하세요.", 422: "정답 형식이나 예측 ID를 확인하세요.", 503: "로컬 저장소에 기록하지 못했습니다. 서버 저장 공간을 확인하고 다시 시도하세요." };
    throw new Error(messages[response.status] || `요청에 실패했습니다 (${response.status}).`);
  }
  return response.json();
}

function showError(message = "") {
  $("feedback-error").textContent = message;
  $("feedback-error").hidden = !message;
}

function remember(id) {
  try { localStorage.setItem("hourahead.latestPrediction", id); } catch { /* ID can be copied manually. */ }
}

function selectPrediction(id, result, source, status) {
  activeId = id;
  submissionId = null;
  feedbackVersion += 1;
  $("prediction-id").value = id;
  $("feedback-context").textContent = `${result.target_hour.slice(0, 16).replace("T", " ")} · 예측 ${stageNames[result.workload_stage]} · ${source === "demo" ? "과거 샘플" : "사용자 데이터"}`;
  $("prediction-delivery").textContent = deliveryNames[status];
  $("feedback-form").hidden = false;
  $("feedback-result").textContent = "정답을 아직 입력하지 않았습니다.";
  $("feedback-count").value = "";
  $("feedback-correctness").value = "";
  $("feedback-stage").value = "";
  showError();
  remember(id);
}

function showReceipt(receipt) {
  const { event, log_status: status } = receipt;
  if (event.prediction_id !== activeId) return;
  const feedback = event.submission.feedback;
  let answer;
  if (feedback.kind === "actual_count") answer = `실제 ${feedback.value}건 (${stageNames[event.actual_stage]})`;
  else if (feedback.kind === "actual_stage") answer = `실제 단계 ${stageNames[feedback.value]}`;
  else answer = `사용자 판정 ${feedback.value ? "맞음(T)" : "틀림(F)"} · 실제값 미입력`;
  $("feedback-result").textContent = `${answer} · ${event.is_correct ? "예측 일치" : "예측 불일치"} · ${deliveryNames[status]} · ${new Date(event.recorded_at).toLocaleString("ko-KR")}`;
}

function clearSelection() {
  lookupVersion += 1;
  feedbackVersion += 1;
  activeId = null;
  submissionId = null;
  $("feedback-form").hidden = true;
  $("feedback-result").textContent = "";
  $("prediction-delivery").textContent = "";
  $("feedback-context").textContent = "입력한 예측 ID를 조회한 뒤 정답을 저장하세요.";
  showError();
}

async function refreshPrediction(id, version) {
  const receiptVersion = feedbackVersion;
  const detail = await request(`/predictions/${encodeURIComponent(id)}`);
  if (version !== lookupVersion || activeId !== id) return;
  $("prediction-delivery").textContent = deliveryNames[detail.log_status];
  if (receiptVersion === feedbackVersion && detail.latest_feedback) showReceipt(detail.latest_feedback);
}

export function recordPrediction(result, source) {
  if (activeId !== result.prediction_id) {
    lookupVersion += 1;
    selectPrediction(result.prediction_id, result, source, result.log_status);
  } else {
    // Keep the selection version so an in-flight feedback save can still update it.
    $("prediction-delivery").textContent = deliveryNames[result.log_status];
  }
  const version = lookupVersion;
  void refreshPrediction(result.prediction_id, version).catch(error => {
    if (version === lookupVersion) showError(error.message);
  });
  void refreshLogStatus();
}

async function lookup(id) {
  clearSelection();
  const version = lookupVersion;
  try {
    const detail = await request(`/predictions/${encodeURIComponent(id)}`);
    if (version !== lookupVersion) return;
    selectPrediction(detail.event.prediction_id, detail.event.response, detail.event.data_source, detail.log_status);
    if (detail.latest_feedback) showReceipt(detail.latest_feedback);
  } catch (error) {
    if (version === lookupVersion) showError(error.message);
  }
}

$("prediction-id").addEventListener("input", clearSelection);
$("prediction-lookup").addEventListener("submit", async event => {
  event.preventDefault();
  $("lookup-button").disabled = true;
  try { await lookup($("prediction-id").value.trim()); }
  finally { $("lookup-button").disabled = false; }
});

function updateKind() {
  const kind = $("feedback-kind").value;
  for (const [type, input] of [["actual_count", "feedback-count"], ["correctness", "feedback-correctness"], ["actual_stage", "feedback-stage"]]) {
    $(input).disabled = type !== kind;
    $(input).parentElement.hidden = type !== kind;
  }
}
$("feedback-kind").addEventListener("change", updateKind);
$("feedback-form").addEventListener("input", () => { submissionId = null; showError(); });

$("feedback-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!activeId || saving) return;
  const predictionId = activeId;
  const version = lookupVersion;
  const kind = $("feedback-kind").value;
  const values = { actual_count: $("feedback-count").value, correctness: $("feedback-correctness").value, actual_stage: $("feedback-stage").value };
  showError();
  try {
    const feedback = makeFeedback(kind, values[kind]);
    submissionId ||= crypto.randomUUID();
    const payload = { submission_id: submissionId, feedback };
    saving = true;
    feedbackVersion += 1;
    $("save-feedback").disabled = true;
    $("feedback-fields").disabled = true;
    const receipt = await request(`/predictions/${predictionId}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (activeId === predictionId && version === lookupVersion) {
      feedbackVersion += 1;
      showReceipt(receipt);
    }
    await refreshLogStatus();
  } catch (error) { if (activeId === predictionId && version === lookupVersion) showError(error.message); }
  finally { saving = false; $("save-feedback").disabled = false; $("feedback-fields").disabled = false; updateKind(); }
});

export async function refreshLogStatus() {
  const version = ++logVersion;
  try {
    const status = await request("/logging");
    if (version !== logVersion) return;
    $("log-summary").textContent = status.destination === "local-s3"
      ? `로컬 S3 모사 · AWS 미연결 · 파일 저장 ${status.sent_events}건 / 대기 ${status.pending_events}건${status.failed_events ? ` / 재시도 ${status.failed_events}건` : ""}`
      : status.destination === "s3"
      ? `S3 · ${status.region} · 전송 ${status.sent_events}건 / 대기 ${status.pending_events}건${status.failed_events ? ` / 실패 후 재시도 ${status.failed_events}건` : ""}`
      : `로컬 보관 ${status.pending_events}건 · AWS S3 연결 전입니다.`;
  } catch { if (version === logVersion) $("log-summary").textContent = "로그 저장 상태를 확인하지 못했습니다. 다시 새로고침하세요."; }
}
$("refresh-logs").addEventListener("click", async () => {
  await refreshLogStatus();
  if (!activeId) return;
  const version = lookupVersion;
  try { await refreshPrediction(activeId, version); }
  catch (error) { if (version === lookupVersion) showError(error.message); }
});
updateKind();
void refreshLogStatus();
try {
  const remembered = localStorage.getItem("hourahead.latestPrediction");
  if (remembered) {
    $("prediction-id").value = remembered;
    void lookup(remembered);
  }
} catch { /* Lookup by an explicitly entered ID remains available. */ }
