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
  if (rows.length !== HISTORY_HOURS) throw new Error(`이력은 168행이어야 합니다. 현재 ${rows.length}행입니다.`);
  return rows.map((line, index) => {
    const columns = line.split(",").map(part => part.trim());
    if (columns.length !== 2) throw new Error(`${index + 2}행: hour,count 두 열만 입력하세요. 따옴표 없이 입력하세요.`);
    const hour = parseHour(columns[0], `${index + 2}행 시각`).normalized;
    if (!/^\d+$/.test(columns[1]) || Number(columns[1]) > 100000) {
      throw new Error(`${index + 2}행: 건수는 0~100,000의 정수여야 합니다.`);
    }
    return { hour, count: Number(columns[1]) };
  });
}

export function makeRequest(targetText, csvText) {
  const target = parseHour(targetText, "예측 시작 시각");
  const history = parseHistoryCSV(csvText);
  history.forEach((row, index) => {
    const expected = target.milliseconds - (HISTORY_HOURS - index) * HOUR_MS;
    if (parseHour(row.hour).milliseconds !== expected) {
      throw new Error(`${index + 2}행: 예측 시작 168시간 전부터 1시간 전까지 오름차순으로 입력하세요. 빠진 시간·중복·예측 대상 시간은 포함할 수 없습니다.`);
    }
  });
  return { target_hour: target.normalized, history };
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
