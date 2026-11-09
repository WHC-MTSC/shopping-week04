# PC 서비스와 AWS 로그로 다음 모델 만들기

서비스는 사용자 PC의 `127.0.0.1:8001`에서 실행하고, 로그는 서울 리전 CloudWatch로 전송합니다. [배포 스크립트](DEPLOYMENT.md)는 설치·재시작·롤백 및 로그인 시 자동 실행을 지원합니다. AWS 자격증명은 현재 사용자 프로필을 사용하고, 소스·배포 ZIP에 포함하지 않습니다.

## 1. 실제 CloudWatch 수집 확인

[AWS 설정](AWS_SETUP.md)에 따라 `workload` 프로필, `/shopping-week04/workload` 그룹, `pc-local` 스트림을 설정합니다. 검증기 실행 터미널에도 같은 환경 변수를 지정해야 합니다.

```powershell
$env:AWS_PROFILE = 'workload'
$env:AWS_REGION = 'ap-northeast-2'
$env:WORKLOAD_LOG_BACKEND = 'cloudwatch'
$env:WORKLOAD_CLOUDWATCH_LOG_GROUP = '/shopping-week04/workload'
$env:WORKLOAD_CLOUDWATCH_LOG_STREAM = 'pc-local'
uv run --locked python -m workload.cloudwatch_check --base-url http://127.0.0.1:8001 --timeout 60 --output runtime/cloudwatch-check.json
```

성공 보고서는 실제 AWS에서 예측·정답 두 이벤트의 전체 본문을 다시 읽었다는 증거입니다. 검사 자체가 추가한 이벤트는 `demo`이며 팀원 테스트나 새로운 학습 데이터로 세지 않습니다. 브라우저 `/log-console`은 로컬 기록 조회이므로 실제 AWS 화면 캡처가 필요하면 AWS 콘솔의 해당 그룹·스트림을 사용합니다.

## 2. 배포·복구

GitHub Actions `checks`의 Linux 품질 검사와 Windows 배포·재시작·업데이트·롤백 검사가 모두 통과하면 커밋 소스 ZIP을 제공합니다. 이 workflow는 사용자 PC에 접속하지 않습니다. 다운로드한 소스에서 `scripts/deploy-local.ps1 -Action Deploy`로 배포하고 `-Action Rollback`으로 이전 릴리스를 복구합니다. 실행 옵션과 자동 시작 해제 방법은 [배포 안내](DEPLOYMENT.md)를 참고하세요.

## 3. 팀원별 실제 테스트

PC의 브라우저에서 각 팀원이 직접 테스트합니다. 로컬 주소는 다른 PC에서 접속하는 공유 주소가 아닙니다. 테스트 담당자·시각·prediction_id·feedback_id는 별도의 팀 테스트 기록표에 남깁니다. API는 로그인 계정이나 팀원 이름을 수집하지 않으므로 이벤트만 보고 작성자를 추정하지 않습니다.

팀원에게는 [테스트 안내와 기록표](TEAM_TESTING.md)를 전달합니다. 샘플을 사용한 화면 테스트와 확인된 실제 데이터 제출을 구분합니다.

1. 확인 가능한 실제 168시간 입력 CSV를 업로드하고 예측합니다. 체험용 샘플은 `demo`로 유지합니다.
2. 반환된 예측 ID를 기록합니다. 실제 결과를 확인한 뒤 실제 건수·단계 또는 맞음/틀림을 제출합니다.
3. 저장 상태를 새로고침하고 `/log-console`에서 입력·예측·정답 연결, 최신 revision을 확인합니다.
4. 실제 CloudWatch에서도 같은 prediction_id의 두 이벤트를 확인합니다.

자동 테스트·에이전트의 샘플 제출은 팀원이 직접 수행한 테스트를 대신하지 않습니다. 민감한 고객 정보 없이 시간별 집계만 사용합니다.

## 4. 다운로드·전처리

테스트와 전송이 끝난 UTC 구간을 지정합니다. 아래 시각은 예시이며 실제 실행 구간으로 바꿉니다. 매번 새로운 출력 디렉터리를 사용합니다.

```powershell
uv run --locked python -m workload.export_logs --source cloudwatch --group /shopping-week04/workload --stream pc-local --start 2026-11-09T00:00:00Z --end 2026-11-10T00:00:00Z --profile workload --region ap-northeast-2 --output runtime/exports/team-run
uv run --locked python -m workload.prepare_logs --input runtime/exports/team-run/events.jsonl --output runtime/training/team-run
```

원본 SHA-256과 출처는 `manifest.json`, 사용·제외 건수와 충돌 사유는 `summary.json`에 남습니다. 지도학습은 명시적 실제 건수/단계만 사용합니다. T/F는 행동·보상 데이터로 분리하고 틀렸다는 답에서 임의의 정답 단계를 만들지 않습니다. 이는 완전한 강화학습 에피소드가 아닙니다. [세부 전처리 계약](TRAINING_DATA.md)

## 5. 후보 모델 학습·비교

```powershell
uv run --locked python -m workload.retrain --dataset runtime/training/team-run --output runtime/candidates/team-run --baseline artifacts/model.json
```

실제 정답이 있는 중복 없는 사례가 최소 40개, 학습 최소 30개·평가 최소 10개이고 양쪽에 세 단계가 있어야 합니다. 이는 작은 데모만으로 배포 성능을 주장하지 않기 위한 기본 검증 조건이며 운영 성능을 보장하는 표본 수는 아닙니다. 데이터가 부족하면 수집을 더 진행합니다.

목표 시각 기준으로 뒤쪽을 평가 구간으로 남기고 같은 시각의 사례는 분할하지 않습니다. 고정된 ExtraTrees 설정으로 앞 구간만 학습합니다. 기존·후보 모델을 같은 뒤 구간에서 비교하고 macro F1·혼잡 재현율이 모두 감소하지 않아야 `promotion_eligible=true`가 됩니다. 직렬화 모델과 학습 모델의 확률 오차도 검사합니다. 이전 모델이 그 평가 구간으로 이미 학습됐다면 별도의 새로운 평가 기간이 필요합니다.

`model.json`과 `report.json`은 새 후보 디렉터리에만 저장됩니다. 현재 서비스 모델은 덮어쓰지 않습니다. 종료 코드 1은 데이터·실행 오류, 2는 성능 기준 미달입니다. 데이터 SHA·기존/후보 모델 SHA·버전·학습/평가 시각·지표를 보고서에 보관합니다. 기본 명령은 `--include-demo`로 만든 전처리 결과를 거부합니다.

### 과거 데이터 실습 모드

사용자가 실습을 선택한 경우 `workload.replay_history`가 저장소의 과거 테스트 시각과 원본 시간별 집계를 대조하고, 과거 입력·실제 건수를 HTTP API에 `demo`로 제출합니다. 336시간이 기본이며 팀원 테스트·현재 운영 관측으로 표시하지 않습니다. 원본 SHA와 모델 SHA를 포함한 고정 요청 ID를 사용합니다.

```powershell
uv run --locked python -m workload.replay_history --base-url http://127.0.0.1:8001 --limit 336 --output runtime/historical-replay.json
# /logging의 대기 0건을 확인하고 위 CloudWatch export 명령으로 새 구간을 내려받습니다.
uv run --locked python -m workload.prepare_logs --input runtime/exports/practice/events.jsonl --output runtime/training/practice --include-demo
uv run --locked python -m workload.retrain --dataset runtime/training/practice --output runtime/candidates/practice --allow-demo
```

명시적 `--allow-demo`가 있을 때만 실습 학습을 허용합니다. 결과는 `historical_replay` 용도와 `practice-...` 버전을 사용하며, 품질 기준 통과 여부와 관계없이 `promotion_eligible=false`입니다. `quality_gate_passed`와 실제 비교 지표는 별도로 남깁니다. 실습 모델을 로컬에서 실행해도 새로운 운영 데이터에 대한 개선을 입증한 것은 아닙니다.

## 6. 모델 반영과 실서빙 확인

실데이터 후보는 보고서의 품질 기준과 승격 조건을 통과한 경우 `scripts/deploy-local.ps1 -Action Deploy -ModelPath <후보 model.json 경로>`로 배포합니다. 배포 스크립트는 모델 옆 `report.json`(또는 명시한 `-ModelReportPath`)의 용도·승격 여부·모델 SHA·버전을 확인합니다. 기존 `artifacts/model.json`은 회귀 검증 기준으로 보존하고 후보는 릴리스의 별도 파일로 복사합니다. 후보 서비스의 준비 상태와 파일 SHA·예측 결과를 검사한 뒤에만 릴리스를 전환하고 이전 버전을 보존합니다. 배포 후 별도로 검증하려면:

```powershell
uv run --locked python -m workload.model_check --base-url http://127.0.0.1:8001 --model runtime/candidates/team-run/model.json
```

`/metadata`의 버전·SHA가 후보와 같고 실제 HTTP 예측의 단계·확률도 일치해야 통과합니다. 이 명령도 진단용 `demo` 예측 한 건을 기록합니다. PC 로그인 시 자동 실행되는 것은 현재 릴리스이며 AWS 임시 로그인은 만료 후 다시 해야 합니다. 인증 만료 중 로그는 로컬에 대기하고 재로그인 후 전송합니다.

과거 실습 후보는 승격 조건을 통과하지 못하므로 현재 릴리스로 배포하지 않습니다. 별도 로컬 서버와 별도 DB에서 모델 파일을 선택하여 위 검증기를 실행합니다. 이번 실습은 [실서빙 검사 결과](evidence/practice-model-check.json)에 기록했으며 최종 PC 서비스는 기존 모델을 유지했습니다. 실습 모델은 메타데이터와 화면에도 과거 데이터 실습용임을 표시합니다.

## 7. PR·리뷰 증거

PR에는 실제 완료한 단계, CI 실행 링크, GitHub Actions 또는 로컬 배포 실행 화면, CloudWatch 읽기 검증 보고서, 데이터 전처리 요약 및 후보 비교 보고서를 연결합니다. 자격증명·MFA QR/코드·고객 입력 원문은 첨부하지 않습니다. 팀원 테스트나 후보 승격이 아직 없으면 미완료로 표시합니다.
