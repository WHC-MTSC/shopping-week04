export const HISTORY_HOURS = 168;
export const HOUR_MS = 60 * 60 * 1000;
export const MAX_FILE_BYTES = 1024 * 1024;

// UTC is only an arithmetic container for timezone-free dataset timestamps.
// Do not convert these values to the browser's local timezone.
export function parseHour(value, label = "시각") {
  const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):00(?::00)?$/.exec(value);
  if (!match) throw new Error(`${label}: YYYY-MM-DDTHH:00:00 형식의 정각을 입력하세요. 시간대(Z, +09:00)는 넣지 마세요.`);
  const normalized = `${match[1]}T${match[2]}:00:00`;
  const milliseconds = Date.parse(`${normalized}Z`);
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString().slice(0, 19) !== normalized || value.startsWith("0000")) {
    throw new Error(`${label}: 존재하는 날짜와 시각을 입력하세요.`);
  }
  return { normalized, milliseconds };
}

export function parseHistoryCSV(text) {
  if (new TextEncoder().encode(text).length > MAX_FILE_BYTES) throw new Error("CSV는 1 MB 이하로 입력하세요.");
  const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
  if (lines[0]?.split(",").map(part => part.trim()).join(",") !== "hour,count") {
    throw new Error("첫 줄은 hour,count여야 합니다. CSV 예시를 다운로드해 형식을 확인하세요.");
  }
  const rows = lines.slice(1);
  if (rows.length < HISTORY_HOURS) throw new Error(`데이터 부족: 최소 168시간의 이력이 필요합니다. 현재 ${rows.length}행입니다.`);
  let previousHour = null;
  return rows.map((line, index) => {
    const columns = line.split(",").map(part => part.trim());
    if (columns.length !== 2) throw new Error(`${index + 2}행: hour,count 두 열만 입력하세요. 따옴표 없이 입력하세요.`);
    const parsed = parseHour(columns[0], `${index + 2}행 시각`);
    if (previousHour !== null && parsed.milliseconds !== previousHour + HOUR_MS) {
      throw new Error(`${index + 2}행: 이력 시각은 바로 앞 행보다 1시간 뒤여야 합니다. 누락·중복·역순을 확인하세요.`);
    }
    previousHour = parsed.milliseconds;
    if (!/^\d+$/.test(columns[1]) || Number(columns[1]) > 100000) {
      throw new Error(`${index + 2}행: 건수는 0~100,000의 정수여야 합니다.`);
    }
    return { hour: parsed.normalized, count: Number(columns[1]) };
  });
}

export function inspectHistoryCSV(csvText) {
  const history = parseHistoryCSV(csvText);
  const firstTarget = new Date(parseHour(history[0].hour).milliseconds + HISTORY_HOURS * HOUR_MS).toISOString();
  const nextHour = new Date(parseHour(history.at(-1).hour).milliseconds + HOUR_MS).toISOString();
  if (!/^\d{4}-/.test(nextHour)) throw new Error("이력 다음 예측 시각은 9999년 이내여야 합니다.");
  return { history, first_target_hour: firstTarget.slice(0, 19), last_target_hour: nextHour.slice(0, 19) };
}

export function requestFromCSV(csvText) {
  const inspected = inspectHistoryCSV(csvText);
  return { target_hour: inspected.last_target_hour, history: inspected.history.slice(-HISTORY_HOURS) };
}

export function makeRequest(targetText, csvText) {
  const { history, first_target_hour: first, last_target_hour: last } = inspectHistoryCSV(csvText);
  const target = parseHour(targetText, "예측 시작 시각");
  if (target.milliseconds < parseHour(first).milliseconds || target.milliseconds > parseHour(last).milliseconds) {
    const display = hour => hour.slice(0, 16).replace("T", " ");
    const range = first === last
      ? `현재 이력에 맞는 예측 시각은 ${display(first)}입니다.`
      : `현재 이력으로 예측 가능한 시각은 ${display(first)} ~ ${display(last)}입니다.`;
    throw new Error(`데이터 부족: ${range} 선택한 시각 직전 168시간의 실제 이력을 불러오세요.`);
  }
  const end = (target.milliseconds - parseHour(history[0].hour).milliseconds) / HOUR_MS;
  return { target_hour: target.normalized, history: history.slice(end - HISTORY_HOURS, end) };
}

// This is for display after predicting only; the target row never enters makeRequest.
export function compareCSVActual(history, targetText, predictedStage) {
  const target = parseHour(targetText, "예측 시작 시각");
  const row = history.find(item => item.hour === target.normalized);
  if (!row) return null;
  if (!["idle", "normal", "busy"].includes(predictedStage)) throw new Error("예측 단계가 올바르지 않습니다.");
  const actualStage = row.count === 0 ? "idle" : row.count < 10 ? "normal" : "busy";
  return { count: row.count, actualStage, matches: actualStage === predictedStage };
}

export function toCSV(history) {
  return `hour,count\n${history.map(row => `${row.hour},${row.count}`).join("\n")}\n`;
}

export function makeFeedback(kind, text) {
  if (kind === "correctness" && (text === "true" || text === "false")) {
    return { kind, value: text === "true" };
  }
  if (kind === "actual_stage" && ["idle", "normal", "busy"].includes(text)) {
    return { kind, value: text };
  }
  if (kind === "actual_count" && /^\d+$/.test(text) && Number(text) <= 100000) {
    return { kind, value: Number(text) };
  }
  throw new Error("정답을 선택하거나 0~100,000의 정수 건수를 입력하세요.");
}
