const STAGES = { idle: "없음", normal: "보통", busy: "혼잡" };
const STORAGE = { local: "로컬 보관", pending: "저장 대기", sent: "실제 S3 저장 완료", simulated: "로컬 S3 모사 저장 완료" };

export function storageLabel(status) {
  return STORAGE[status] || "알 수 없는 저장 상태";
}

export function eventSummary(event) {
  if (event.event_type === "prediction") {
    const stage = STAGES[event.response?.workload_stage] || event.response?.workload_stage || "미확인";
    return `작업량 예측: ${stage} · ${event.request?.target_hour?.slice(0, 16).replace("T", " ") || "시각 없음"}`;
  }
  const answer = event.submission?.feedback;
  if (answer?.kind === "actual_count") return `사용자 정답: 실제 ${answer.value}건 · ${STAGES[event.actual_stage] || "단계 미확인"}`;
  if (answer?.kind === "actual_stage") return `사용자 정답: 실제 ${STAGES[answer.value] || answer.value}`;
  if (answer?.kind === "correctness") return `사용자 판정: ${answer.value === true ? "맞음 (T)" : "틀림 (F)"} · 실제값 미입력`;
  return "사용자 정답 이벤트";
}

export function buildLogQuery(filters = {}, before = null) {
  const params = new URLSearchParams({ limit: "50" });
  if (filters.event_type) params.set("event_type", filters.event_type);
  if (filters.prediction_id?.trim()) params.set("prediction_id", filters.prediction_id.trim());
  if (filters.query?.trim()) params.set("query", filters.query.trim());
  if (before !== null) params.set("before", String(before));
  return `/logs?${params.toString()}`;
}

export function formatEventTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value || "시각 없음") : date.toISOString().replace("T", " ").replace("Z", "");
}

async function readJSON(path) {
  let response;
  try { response = await fetch(path, { signal: AbortSignal.timeout(30000) }); }
  catch { throw new Error("로그 서버의 응답을 확인하지 못했습니다. 서버 상태를 확인한 뒤 새로고침하세요."); }
  if (!response.ok) {
    if (response.status === 422) throw new Error("검색 조건을 확인하세요. 예측 ID는 UUID 형식이어야 합니다.");
    throw new Error(`로그 조회에 실패했습니다 (${response.status}). 다시 시도하세요.`);
  }
  return response.json();
}

function initLogConsole() {
  const $ = id => document.getElementById(id);
  let filters = {};
  let items = [];
  let total = 0;
  let nextCursor = null;
  let loadVersion = 0;
  let statusVersion = 0;
  let selected = null;
  let loading = false;

  const create = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function showError(id, message = "") {
    $(id).textContent = message;
    $(id).hidden = !message;
  }

  function closeDetail() {
    selected = null;
    $("event-detail").hidden = true;
    $("event-workspace").classList.remove("with-detail");
    for (const row of $("events-body").children) row.classList.remove("selected");
  }

  function openDetail(item) {
    selected = item;
    $("event-detail").hidden = false;
    $("event-workspace").classList.add("with-detail");
    $("detail-title").textContent = `이벤트 상세 #${item.sequence}`;
    $("detail-meta").replaceChildren();
    const event = item.event;
    const metadata = [
      ["이벤트 유형", event.event_type], ["이벤트 ID", event.event_id],
      ["예측 ID", event.prediction_id], ["기록 시각 (UTC)", formatEventTime(event.recorded_at)],
      ["데이터 구분", event.data_source === "demo" ? "샘플 데이터" : "사용자 데이터"],
      ["저장 상태", storageLabel(item.log_status)],
    ];
    for (const [label, value] of metadata) $("detail-meta").append(create("dt", "", label), create("dd", "", value));
    // Event bodies may contain user input: never interpolate them into HTML.
    $("event-json").textContent = JSON.stringify(event, null, 2);
    for (const row of $("events-body").children) row.classList.toggle("selected", row.dataset.eventId === event.event_id);
  }

  function renderRows() {
    $("events-body").replaceChildren();
    for (const item of items) {
      const event = item.event;
      const row = create("tr");
      row.dataset.eventId = event.event_id;
      const time = create("td", "event-time", formatEventTime(event.recorded_at));
      time.append(create("span", "event-seq", `#${item.sequence}`));
      const type = create("td");
      type.append(create("span", `type-pill ${event.event_type === "feedback" ? "feedback" : "prediction"}`, event.event_type));
      const summary = create("td", "event-summary", eventSummary(event));
      const source = event.data_source === "demo" ? "샘플 데이터" : "사용자 데이터";
      const extra = event.event_type === "prediction" ? `모델 ${event.response?.model_version || "미확인"}` : (event.is_correct ? "예측 일치" : "예측 불일치");
      summary.append(create("small", "", `${source} · ${extra}`));
      const correlation = create("td");
      const link = create("a", "correlation-link", event.prediction_id);
      link.href = `/log-console?prediction_id=${encodeURIComponent(event.prediction_id)}`;
      link.title = `${event.prediction_id} 관련 이벤트만 조회`;
      link.addEventListener("click", action => {
        action.preventDefault();
        $("correlation-id").value = event.prediction_id;
        applyFilters();
      });
      correlation.append(link);
      const storage = create("td");
      const statusClass = Object.hasOwn(STORAGE, item.log_status) ? item.log_status : "unknown";
      storage.append(create("span", `storage-status ${statusClass}`, storageLabel(item.log_status)));
      const action = create("td");
      const view = create("button", "event-open", "JSON 보기");
      view.type = "button";
      view.setAttribute("aria-label", `이벤트 ${item.sequence} 상세 JSON 보기`);
      view.addEventListener("click", () => openDetail(item));
      action.append(view);
      row.append(time, type, summary, correlation, storage, action);
      $("events-body").append(row);
    }
    $("empty-events").hidden = items.length > 0;
    if (!items.length) {
      $("empty-events").replaceChildren(
        create("span", "", "▤"), create("strong", "", "표시할 이벤트가 없습니다"),
        create("p", "", Object.values(filters).some(Boolean) ? "검색 조건을 변경하거나 초기화해 보세요." : "예측을 실행하고 사용자 정답을 등록하면 실제 기록이 여기에 표시됩니다."),
      );
    }
    $("matching-total").textContent = total.toLocaleString("ko-KR");
    $("page-summary").textContent = `${items.length.toLocaleString("ko-KR")}개 표시 / 검색 결과 ${total.toLocaleString("ko-KR")}개${nextCursor === null && items.length ? " · 마지막 이벤트" : ""}`;
    if (selected) {
      const current = items.find(item => item.event.event_id === selected.event.event_id);
      if (current) openDetail(current);
      else closeDetail();
    }
  }

  function updateLoading(value) {
    loading = value;
    $("log-panel")?.setAttribute("aria-busy", String(value));
    $("events-body").setAttribute("aria-busy", String(value));
    $("load-more").disabled = value || nextCursor === null;
    $("load-more").textContent = value ? "불러오는 중…" : "이전 이벤트 더 보기";
  }

  async function loadEvents(reset = false) {
    if (!reset && (loading || nextCursor === null)) return;
    const version = ++loadVersion;
    const cursor = reset ? null : nextCursor;
    if (reset) {
      items = [];
      nextCursor = null;
      closeDetail();
      $("events-body").replaceChildren();
      $("empty-events").hidden = false;
      $("empty-events").replaceChildren(create("strong", "", "로그를 불러오고 있습니다"));
      $("matching-total").textContent = "—";
      $("page-summary").textContent = "조회 중";
    }
    updateLoading(true);
    showError("events-error");
    try {
      const data = await readJSON(buildLogQuery(filters, cursor));
      if (version !== loadVersion) return;
      const seen = new Set(items.map(item => item.event.event_id));
      items.push(...data.items.filter(item => !seen.has(item.event.event_id)));
      nextCursor = data.next_cursor;
      total = data.total;
      renderRows();
      $("last-updated").textContent = `최근 조회 ${new Date().toLocaleTimeString("ko-KR")}`;
    } catch (error) {
      if (version !== loadVersion) return;
      showError("events-error", error.message);
      if (!items.length) {
        $("empty-events").replaceChildren(create("strong", "", "이벤트를 불러오지 못했습니다"), create("p", "", "새로고침으로 다시 시도할 수 있습니다."));
        $("page-summary").textContent = "조회 실패";
      }
    } finally { if (version === loadVersion) updateLoading(false); }
  }

  async function loadStatus() {
    const version = ++statusVersion;
    try {
      const status = await readJSON("/logging");
      if (version !== statusVersion) return;
      const localS3 = status.destination === "local-s3";
      $("environment-title").textContent = status.destination === "s3"
        ? "실제 S3 전송 모드 · CloudWatch 스타일 로컬 뷰"
        : "로컬 모사 환경 · AWS 미연결";
      $("environment-pill").textContent = status.destination === "s3" ? "AWS S3 · LOCAL VIEW" : "LOCAL SIMULATION";
      $("destination").textContent = localS3 ? "로컬 S3 모사" : status.destination === "s3" ? "실제 AWS S3" : "로컬 보관";
      $("destination-note").textContent = localS3 ? "로컬 객체 파일 저장 · AWS 미연결" : status.destination === "s3" ? "서버의 AWS 설정 사용" : "파일 전송 대상 미설정";
      $("header-region").textContent = `리전 설정 · ${status.region}`;
      $("total-events").textContent = (status.pending_events + status.sent_events).toLocaleString("ko-KR");
      $("sent-events").textContent = status.sent_events.toLocaleString("ko-KR");
      $("pending-events").textContent = status.pending_events.toLocaleString("ko-KR");
      $("failed-events").textContent = status.failed_events.toLocaleString("ko-KR");
      $("failed-events").classList.toggle("problem", status.failed_events > 0);
      $("delivered-label").textContent = status.destination === "s3" ? "실제 S3 저장 완료" : localS3 ? "모사 파일 저장 완료" : "전송 완료";
      $("status-note").textContent = status.failed_events ? `재시도 중 · ${status.last_error || "저장 오류"}` : "저장 오류 없음";
      showError("status-error");
    } catch (error) { if (version === statusVersion) showError("status-error", error.message); }
  }

  function applyFilters() {
    if (!$("filter-form").reportValidity()) return;
    filters = { event_type: $("event-type").value, prediction_id: $("correlation-id").value.trim(), query: $("search-query").value.trim() };
    for (const button of document.querySelectorAll("[data-stream]")) button.classList.toggle("active", button.dataset.stream === filters.event_type);
    void loadEvents(true);
  }

  $("filter-form").addEventListener("submit", event => { event.preventDefault(); applyFilters(); });
  $("clear-filters").addEventListener("click", () => { $("filter-form").reset(); applyFilters(); });
  $("refresh-events").addEventListener("click", () => { void loadEvents(true); void loadStatus(); });
  $("load-more").addEventListener("click", () => { void loadEvents(); });
  $("close-detail").addEventListener("click", closeDetail);
  for (const button of document.querySelectorAll("[data-stream]")) button.addEventListener("click", () => { $("event-type").value = button.dataset.stream; applyFilters(); });
  $("download-event").addEventListener("click", () => {
    if (!selected) return;
    const event = selected.event;
    const url = URL.createObjectURL(new Blob([JSON.stringify(event, null, 2)], { type: "application/json" }));
    const anchor = create("a");
    anchor.href = url;
    anchor.download = `${event.event_type}-${event.event_id}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const initialId = new URLSearchParams(location.search).get("prediction_id");
  const validInitialId = initialId && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(initialId);
  if (validInitialId) $("correlation-id").value = initialId;
  applyFilters();
  if (initialId && !validInitialId) showError("events-error", "URL의 예측 ID 형식이 잘못되어 전체 이벤트를 조회합니다.");
  void loadStatus();
}

if (typeof document !== "undefined" && document.getElementById("events-body")) initLogConsole();
