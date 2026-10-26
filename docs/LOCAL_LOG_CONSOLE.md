# AWS 연결 없이 로그 수집 확인하기

**로컬 S3 모사 저장 + CloudWatch 스타일 로그 화면**으로 입력·예측·정답이 연결되는 흐름을 확인할 수 있습니다. 실제 JSON 파일과 SQLite 기록을 읽는 기능이며, AWS S3·CloudWatch 리소스를 만들거나 AWS API로 전송하지 않습니다. AWS 로그인과 IAM 권한도 필요하지 않습니다.

## 실행

저장소 루트의 PowerShell에서 실행합니다. 이미 실행 중인 서버가 있다면 설정을 적용해 다시 시작합니다.

```powershell
$env:WORKLOAD_LOG_BACKEND = 'local-s3'
$env:WORKLOAD_LOG_DB = 'runtime/events.sqlite3'
$env:WORKLOAD_LOCAL_S3_ROOT = 'runtime/local-s3'
$env:WORKLOAD_S3_PREFIX = 'workload-events'
uv sync --locked
uv run --locked uvicorn workload.api:app --host 127.0.0.1 --port 8000
```

- 예측·정답 화면: `http://127.0.0.1:8000/`
- 로컬 로그 콘솔: `http://127.0.0.1:8000/log-console`
- JSON 기록 목록: `GET /logs`
- 저장 상태: `GET /logging`

`.env.example`은 변수 예시 파일입니다. 서버가 `.env`를 자동 로드하지 않으므로 위와 같이 실행 환경에 설정합니다.

## 확인 흐름

1. 예측 화면에서 **샘플로 시작하기 → 작업량 예측하기**를 실행합니다. `prediction_id`와 로컬 저장 상태가 표시됩니다.
2. 정답 입력 영역에서 실제 건수, 실제 작업량 단계, 맞음/틀림(T/F) 중 하나를 저장합니다. 정답은 기존 예측에 같은 ID로 연결됩니다.
3. `/log-console`에서 예측·정답 이벤트를 조회합니다. 필터와 페이지 이동으로 기록을 찾고, 원본 JSON의 입력·출력·정답을 확인합니다.
4. 같은 예측의 정답을 수정하면 새 피드백 이벤트의 `revision`이 증가합니다. 이전 정답은 기록으로 남습니다.
5. 서버를 같은 DB 경로로 재시작해도 조회와 정답 연결이 유지됩니다.

샘플은 `data_source=demo`로 저장됩니다. 샘플 또는 사람이 입력한 T/F를 실제 서비스의 검증된 정답처럼 섞어서 집계하지 마세요. `correctness=false`는 예측이 틀렸다는 사용자 판정이지 실제 건수가 0이라는 뜻이 아닙니다.

## 로그 목록 API

`GET /logs`는 최근 기록부터 반환하고 다음 쿼리를 지원합니다.

| 쿼리 | 의미 |
|---|---|
| `limit` | 1~100, 기본 50건 |
| `before` | 1 이상의 이전 응답 `next_cursor`; 이 sequence보다 오래된 기록 조회 |
| `event_type` | 선택적 `prediction` 또는 `feedback` |
| `prediction_id` | 선택적 UUID; 같은 예측에 연결된 기록 조회 |
| `query` | 대소문자를 구분하는 부분 문자열 검색, 최대 128자; 정규식 아님 |

예를 들어 `/logs?event_type=feedback&limit=20`으로 정답 기록을 조회할 수 있습니다. 응답은 `items`, `next_cursor`, `total`이며 각 항목에는 `sequence`, 원본 `event`, 현재 `log_status`가 있습니다. `total`은 페이지 커서와 무관하게 필터에 맞는 전체 기록 수입니다. `next_cursor`가 `null`이면 마지막 페이지입니다. sequence 기준 페이지 이동이므로 앞에 새 기록이 추가되어도 기존 페이지 순서를 유지합니다. 원본 이벤트는 새 기록으로 추가되며, 전송 상태는 이후 바뀔 수 있습니다.

## 파일과 상태

기본 모사 버킷 이름은 `hourahead-local-logs`이며, 파일은 다음 구조로 생성됩니다.

```text
runtime/
  events.sqlite3
  local-s3/
    hourahead-local-logs/
      workload-events/
        prediction/YYYY/MM/DD/<event_id>.json
        feedback/YYYY/MM/DD/<event_id>.json
```

모델의 목표 시간과 별개로 경로의 날짜는 실제 기록 시각(UTC)입니다. 이벤트 한 건당 JSON 파일 하나이며, `prediction_id`로 입력·예측과 정답을 연결합니다. SQLite에는 기록 및 완료 상태가 저장되고, 로컬 객체 파일은 S3의 버킷/키 구성을 확인할 수 있게 만듭니다. 이 모사는 S3 호환 HTTP 서버나 실제 CloudWatch 서비스가 아닙니다.

| `log_status` | 의미 |
|---|---|
| `local` | SQLite에 보관, 외부 저장 대상 미사용 |
| `pending` | 선택한 저장 대상에 쓰기 대기 또는 재시도 중 |
| `simulated` | 로컬 S3 모사 JSON 저장 완료; AWS 업로드가 아님 |
| `sent` | 실제 AWS S3 업로드 확인 완료 |

`/logging.destination`이 `local-s3`이면 완료 집계도 모사 저장에 대한 값입니다. **모사 저장 성공을 실제 AWS 인증·권한·원격 업로드 검증으로 해석하지 않습니다.** 저장소 장애 시 완료로 표시하지 않고 대기 기록을 보존합니다.

## 나중에 실제 AWS로 전환하기

[AWS 권한 목록](AWS_PERMISSIONS.md)과 [S3 설정 안내](AWS_SETUP.md)를 참고해 실제 버킷과 인증을 준비합니다. `WORKLOAD_LOG_BACKEND=s3` 및 실제 `WORKLOAD_S3_BUCKET`을 설정하고 서버를 다시 시작합니다.

모사 완료와 실제 S3 완료는 따로 기록하므로, **같은 DB로 `local-s3`에서 `s3`로 전환하면 모사 기록도 실제 S3 전송 대상이 됩니다.** 시험용 로그까지 업로드하지 않으려면 기존 모사 DB를 보존하고 새 운영 DB 경로를 지정하세요. 이미 실제 S3로 보낸 뒤 다른 실제 버킷/접두사로 변경하는 경우에는 기존 완료 기록이 자동 재전송되지 않습니다.

로컬 DB와 모사 파일은 `runtime/` 아래에 있고 Git에서 제외됩니다. 앱에는 사용자 인증이 없으므로 이 콘솔도 로컬 개발용입니다. 외부 공개 전 기록 조회와 정답 작성에 접근 제어가 필요합니다.
