# S3 로그 수집 설정

이 문서는 **실제 AWS 연결을 선택할 때** 사용하는 안내입니다. AWS 없이 확인할 수 있는 [로컬 S3 모사와 로그 콘솔](LOCAL_LOG_CONSOLE.md), 계정 관리자에게 전달할 [역할별 AWS 권한 목록](AWS_PERMISSIONS.md)도 제공합니다.

이 서비스는 입력·예측·사용자 정답 이벤트를 SQLite에 먼저 기록하고 S3로 전송합니다. 현재 구현된 원격 저장소는 **S3**입니다. CloudWatch Logs 전송은 구현하지 않았으며, 서버의 일반 요청 로그와 이 이벤트 수집은 별개입니다.

## 서울 리전의 저장소 준비

`infra/log-bucket.json`은 스택을 생성한 리전에 비공개 S3 버킷을 만듭니다. 버킷 이름은 CloudFormation이 자동 생성합니다. SSE-S3(AES256), 퍼블릭 액세스 차단 4종, ACL 비활성화(`BucketOwnerEnforced`), HTTPS 강제 정책을 적용합니다. 버킷과 정책은 스택 삭제·교체 시에도 유지합니다. IAM 사용자·역할·액세스 키는 생성하지 않습니다. 버킷 생성 위치는 스택의 리전으로 결정됩니다. [CloudFormation S3 리소스 문서](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-s3-bucket.html)

기존 AWS CLI 프로필 또는 실행 환경의 IAM 역할을 사용하세요. 로컬 개발에서는 AWS CLI 2.32.0 이상의 `aws login`으로 콘솔 계정의 임시 인증을 연결할 수 있습니다. 브라우저 콘솔 로그인만으로 Python 프로세스가 인증되지는 않습니다. 프로젝트의 `boto3[crt]` 의존성이 이 로그인 프로필을 지원합니다. 로그인 세션이 만료되면 다시 로그인해야 합니다. [AWS 로그인 인증 공급자](https://docs.aws.amazon.com/sdkref/latest/guide/feature-login-credentials.html)

아래 PowerShell 명령은 저장소 루트에서 실행합니다. `workload`는 사용할 프로필명입니다. CLI가 PATH에 없다면 `aws` 대신 해당 실행 파일 경로를 사용하세요.

```powershell
aws login --profile workload --region ap-northeast-2
aws sts get-caller-identity --profile workload --region ap-northeast-2
aws cloudformation validate-template --template-body file://infra/log-bucket.json --profile workload --region ap-northeast-2
aws cloudformation deploy --stack-name shopping-workload-logs --template-file infra/log-bucket.json --profile workload --region ap-northeast-2 --no-fail-on-empty-changeset
aws cloudformation describe-stacks --stack-name shopping-workload-logs --profile workload --region ap-northeast-2 --query "Stacks[0].Outputs" --output table
```

템플릿 배포에는 CloudFormation과 해당 S3 버킷·정책을 만들 수 있는 권한이 필요합니다. 이는 서비스가 로그를 업로드할 때 필요한 권한과 다릅니다. 템플릿에는 IAM 리소스가 없으므로 `CAPABILITY_IAM`은 요구하지 않습니다.

## 서버 연결

출력의 `BucketName`을 `WORKLOAD_S3_BUCKET`에 지정하고 서버를 다시 시작합니다. `.env.example`은 변수 설명용이며, 애플리케이션이 `.env`를 자동으로 읽지는 않습니다.

```powershell
$env:AWS_PROFILE = 'workload'
$env:AWS_REGION = 'ap-northeast-2'
$env:WORKLOAD_LOG_DB = 'runtime/events.sqlite3'
$env:WORKLOAD_S3_PREFIX = 'workload-events'
$env:WORKLOAD_LOG_BACKEND = 's3'
$env:WORKLOAD_S3_BUCKET = '<BucketName 출력값>'
uv sync --locked
uv run --locked uvicorn workload.api:app --host 127.0.0.1 --port 8000
```

| 변수 | 기본값 / 의미 |
|---|---|
| `AWS_REGION` | `ap-northeast-2`; S3 클라이언트 리전 |
| `AWS_PROFILE` | 미지정 시 Boto3 기본 인증 탐색; 로컬 로그인 프로필명 |
| `WORKLOAD_LOG_BACKEND` | `local`, `local-s3`, `s3`; 미지정이면 버킷이 있을 때 `s3`, 없으면 `local` |
| `WORKLOAD_LOCAL_S3_ROOT` | `runtime/local-s3`; 모사 객체의 루트 디렉터리 |
| `WORKLOAD_S3_BUCKET` | 실제 AWS S3 버킷명; `s3` 모드에서 필요 |
| `WORKLOAD_S3_PREFIX` | `workload-events`; 버킷 안의 저장 경로 접두사 |
| `WORKLOAD_LOG_DB` | `runtime/events.sqlite3`; 입력·결과·정답·전송 상태를 보관하는 파일 |

별도 경로에 CLI 설정/인증 캐시를 보관했다면 `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE`, `AWS_LOGIN_CACHE_DIRECTORY`를 CLI와 서버에 동일하게 전달해야 합니다. 키·토큰·인증 캐시를 소스나 로그에 넣지 마세요. `runtime/` 및 `.env`는 Git에서 제외됩니다. 현재 애플리케이션은 인증이 없는 로컬 개발용이므로 외부 공개 전 예측 조회와 정답 작성에 사용자별 접근 제어를 추가해야 합니다.

## 최소 업로드 권한

[역할별 AWS 권한 목록](AWS_PERMISSIONS.md)은 로그인·인프라 배포·서비스 업로드·운영자 조회 및 선택적 CloudWatch 동작을 구분합니다.

`infra/uploader-policy.example.json`의 `REPLACE_WITH_BUCKET_NAME`을 실제 버킷명으로 바꾸고 기존 실행 역할/사용자의 정책에 적용합니다. 기본 접두사 `workload-events/*`에 업로드용 `s3:PutObject`와 기존 이벤트 검증용 `s3:GetObject`를 허용합니다. 조건부 업로드(`If-None-Match`)가 같은 키의 기존 객체 때문에 `412 PreconditionFailed`를 반환하면 그 객체를 읽어 전송하려던 본문과 비교합니다. 앱은 ACL 변경, 목록 조회, 객체 삭제 권한을 요구하지 않습니다. 접두사를 바꾼다면 두 정책문의 경로도 함께 바꿉니다. [S3 조건부 쓰기 문서](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)

`PutObject`에만 SSE-S3 AES256과 조건부 업로드 헤더 조건을 적용합니다. `GetObject`는 같은 접두사의 별도 정책문에 두어 업로드 전용 조건 때문에 읽기가 거부되지 않게 합니다. 이 정책은 업로더용 예시이며, 운영자의 콘솔 목록 조회나 CloudFormation 배포 권한은 포함하지 않습니다. 정책 파일을 만들었다고 실제 계정에 적용된 것은 아닙니다.

## 이벤트 내용과 연결 방식

모든 이벤트의 `schema_version`은 `1`입니다. `recorded_at`은 UTC 기록 시각이고, `request.target_hour`와 이력 시각은 모델이 사용하는 시간대 없는 원본 현지 시각입니다. 과거 목표 시각으로 시험하더라도 S3 경로는 이벤트를 실제 기록한 UTC 날짜를 따릅니다.

| 이벤트 | 주요 내용 |
|---|---|
| `prediction` | `event_id`, `prediction_id`, `recorded_at`, `data_source`, 전체 `request`(168시간 이력 포함), `response`(단계·확률·모델 버전), `features`, `model_sha256` |
| `feedback` | `event_id`, `prediction_id`, `recorded_at`, `data_source`, `revision`, `submission`(제출 UUID 및 정답 유형·값), `actual_stage`, `is_correct` |

두 이벤트는 `prediction_id`로 연결합니다. 피드백 수정은 새 이벤트를 추가하므로 이전 정답도 남습니다. 동일 예측의 최종 정답은 가장 큰 `revision`으로 선택합니다. 원격 객체가 도착한 순서나 사용자가 입력한 T/F를 임의의 실제 건수로 바꾸지 마세요. `data_source=demo`인 샘플 예측은 실제 운영 성능 집계에서 구분해야 합니다.

정답은 다음 세 종류입니다. 문자열 `"false"`, 숫자 `0`을 T/F 대신 보내거나, 불리언을 실제 건수로 보내면 API가 거부합니다.

```json
{"submission_id":"b3d942da-28a0-4cc3-8df0-df08fc6b5f04","feedback":{"kind":"actual_count","value":12}}
```

```json
{"submission_id":"fb5cc3de-7e52-4bad-832e-708d48a23785","feedback":{"kind":"actual_stage","value":"busy"}}
```

```json
{"submission_id":"bac607dd-c257-40ee-975f-62c09b083413","feedback":{"kind":"correctness","value":false}}
```

`actual_count`는 실제 0~100,000 정수 건수에서 단계를 계산하고 예측 단계와 비교합니다. `actual_stage`는 `idle`·`normal`·`busy` 중 실제 단계를 비교합니다. `correctness`는 사용자가 예측 단계가 맞았는지만 판정한 값이며 `actual_stage`는 `null`로 유지합니다. 피드백은 별도 API로 기록하므로 모델의 예측 입력이나 자동 재학습에 섞이지 않습니다.

## 재시도와 저장 상태

`POST /predict`에 선택적으로 UUID `Idempotency-Key` 헤더를 보낼 수 있습니다. 같은 키와 같은 입력·출처를 재전송하면 기존 예측을 돌려주고, 같은 키에 다른 내용을 보내면 `409`입니다. 헤더를 생략한 API 호출은 매번 새 예측을 만듭니다. 화면은 재시도할 때 같은 UUID를 사용합니다. 정답 제출의 `submission_id`도 같은 방식으로 중복 저장을 막습니다. 정답을 수정할 때는 새 제출 UUID를 사용하세요.

예측과 피드백은 먼저 SQLite 트랜잭션으로 저장됩니다. 이 로컬 저장이 실패하면 API는 `503`을 반환하고 저장 성공을 표시하지 않습니다. S3를 설정한 경우 전송 작업이 대기 이벤트를 순서대로 읽습니다. AWS 연결·권한 오류는 대기 상태로 남고 다음 시도 때 재전송됩니다. 실패 이벤트를 건너뛰지 않으므로 실패가 지속되면 뒤의 이벤트도 대기할 수 있습니다.

```text
workload-events/prediction/YYYY/MM/DD/<event_id>.json
workload-events/feedback/YYYY/MM/DD/<event_id>.json
```

각 이벤트는 JSON 객체 하나로 저장하며 SSE-S3와 `IfNoneMatch="*"`를 사용합니다. 앱은 이미 있는 같은 이벤트 키를 덮어쓰지 않습니다. AWS가 업로드를 수락한 뒤 로컬 확인이 끊겨도 같은 키로 재시도할 수 있습니다. 이때 `412 PreconditionFailed`만으로 완료 처리하지 않고 `GetObject`로 기존 본문을 비교해 동일한 이벤트일 때만 `sent`로 확인합니다. 본문이 다르거나 읽기에 실패하면 대기 상태를 유지하고 오류를 기록합니다. 이는 애플리케이션의 추가 기록 방식이며 S3 Object Lock이나 관리자 삭제 방지를 설정한 것은 아닙니다.

| 상태 | 의미 |
|---|---|
| `local` | 로컬 저장 완료, S3 미설정 |
| `pending` | 로컬 저장 완료, S3 전송 대기 또는 재시도 중 |
| `simulated` | 로컬 S3 모사 파일 저장 완료; 실제 AWS 업로드 아님 |
| `sent` | 해당 이벤트의 실제 S3 업로드 확인 완료 |

`GET /logging`은 `destination`, `region`, `pending_events`, `sent_events`, `failed_events`, `last_error`를 반환합니다. 오류에는 자격증명·입력값 대신 오류 클래스 이름만 남깁니다. 예측 응답 당시의 `log_status`는 이후 변경될 수 있으므로 화면의 **저장 상태 새로고침** 또는 예측 조회 API로 다시 확인합니다. `/health`의 모델 준비 상태만으로 S3 연결 성공을 판단하지 마세요.

서버를 멈췄다가 같은 DB로 다시 실행하면 대기 이벤트를 이어서 전송합니다. **이미 실제 S3에 전송한 뒤 같은 DB의 실제 버킷이나 접두사를 변경해도 `sent`인 이벤트는 재전송하지 않습니다.** 아직 대기 중인 이벤트는 변경된 목적지로 전송됩니다. 모사 완료와 실제 완료는 별도로 관리하므로 `local-s3`에서 `s3`로 바꾸면 같은 DB의 모사 이벤트는 실제 업로드 대상입니다. 새 목적지로 이전할 때는 기존 DB를 보존하고 새 DB를 사용하거나, 별도 검증된 재내보내기 절차를 준비하세요. DB 파일을 삭제하면 아직 전송하지 않은 로그와 예측 ID 조회·정답 연결 정보가 사라집니다. 현재 로컬 DB 및 S3에 자동 삭제·보관 기간 정책은 설정하지 않았습니다.

## 실제 수집 확인

서버에서 샘플 예측을 한 번 실행하고 반환된 `prediction_id`로 정답을 입력합니다. 로그 상태가 `sent`로 바뀌는지 확인한 다음, S3의 해당 날짜 `prediction/`·`feedback/` 객체를 읽어 같은 `prediction_id`로 연결되는지 확인합니다. API 조회에서 최신 정답과 `revision`도 대조합니다. 이 읽기 검증은 별도 권한이 있는 운영자 세션에서 수행합니다.

자동 테스트는 로컬 영속성·중복 요청·정답 타입·전송 실패 후 복구 및 AWS SDK Stubber의 요청 계약을 검증합니다. Stubber 통과만으로 실제 AWS 인증·버킷 권한·원격 업로드 성공이 검증된 것은 아닙니다. 실제 업로드 여부는 위 확인을 완료한 실행에서 별도로 기록해야 합니다.
