# 로그 다운로드와 전처리 입력

`workload.export_logs`는 로컬 SQLite 또는 **이미 있는 CloudWatch 로그 그룹**을 읽어 `events.jsonl`과 `manifest.json`을 새 디렉터리에 저장합니다. AWS 리소스·IAM 정책을 만들지 않습니다.

```powershell
uv run --locked python -m workload.export_logs --source local --database runtime/events.sqlite3 --output runtime/exports/local-20261109
uv run --locked python -m workload.export_logs --source cloudwatch --group /shopping-week04/workload --stream pc-local --start 2026-11-09T00:00:00Z --end 2026-11-09T01:00:00Z --profile workload --region ap-northeast-2 --output runtime/exports/cloudwatch-20261109
```

CloudWatch의 `--group`, UTC `--start`·`--end`는 필수이며 `--stream`을 생략하면 그룹 전체 스트림을 조회합니다. UTC `Z` 또는 `+00:00`을 사용하고 시작은 종료보다 빨라야 합니다. 조회 구간은 CloudWatch 이벤트 시각 기준이며, 밀리초보다 세밀한 시각은 거부합니다. 과거 모델 입력의 `target_hour`를 기준으로 조회하지 않습니다.

로컬 DB는 SQLite `mode=ro`로 열고 `events.body`를 sequence 순으로 읽습니다. 없는 DB는 만들지 않고 실패합니다. 올바른 JSON 객체는 필드를 모두 보존하고, 일반 로그 문자열·JSON 배열 등은 `unparsed_message`로 감싸 전처리 단계에서 격리할 수 있게 합니다. 이벤트 스키마 검증·중복 제거·정답 연결은 `workload.prepare_logs`가 수행하며 입력 파일로 내보낸 `events.jsonl`을 전달합니다.

빈 CloudWatch 페이지도 `nextToken`이 있으면 계속 읽습니다. 토큰이 반복되거나 조회가 실패하면 작업이 실패합니다. 기본 최대 기록은 100,000건이며 `--max-records`로 변경할 수 있습니다. 제한 초과 시 일부만 성공한 것처럼 저장하지 않고 실패하므로 구간을 나누어 다시 실행하세요. 조회 중 늦게 도착한 이벤트까지 영구히 포함하는 스냅샷을 보장하지 않으므로, 테스트가 끝나고 전송 완료를 확인한 뒤 확정 구간을 내려받으세요. [AWS FilterLogEvents API](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html)

모든 조회와 파일 쓰기가 성공한 뒤 결과 디렉터리를 공개합니다. 기존 출력 디렉터리는 덮어쓰지 않습니다. 실패하면 최종 출력·성공 manifest가 생기지 않습니다. manifest에는 출처, CloudWatch 그룹·스트림·시간 구간, 건수, JSONL SHA-256이 기록되며 인증 정보는 포함하지 않습니다.

CloudWatch 조회에는 기존 로그 그룹의 `logs:FilterLogEvents` 권한만 필요합니다. 생성·삭제·Unmask 권한은 사용하지 않습니다. 로컬 CLI 로그인은 기존 IAM 사용자/역할에 `SignInLocalDevelopmentAccess` 또는 동등한 로그인 권한이 필요하며, Identity Center 환경은 기존 SSO 프로필을 사용합니다. 자격증명은 AWS 프로필/역할에서 읽고 소스에 넣지 않습니다. [CLI 로그인 요구 조건](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)

이 도구의 자동 검증은 임시 SQLite와 가짜 CloudWatch 클라이언트로 수행합니다. 실제 AWS 다운로드 성공 여부는 실제 계정에서 실행해 별도로 확인해야 합니다.
