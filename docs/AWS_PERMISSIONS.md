# 역할별 AWS 권한

로컬 S3 모사와 `/log-console` 화면에는 **AWS 로그인·IAM 권한이 필요하지 않습니다.** 아래 내용은 나중에 실제 AWS 연결을 선택했을 때 계정 관리자에게 전달할 목록입니다. 현재 구현의 원격 저장 경로는 S3이며 실제 CloudWatch Logs 전송은 구현하지 않았습니다. 정책 예시를 저장한 것만으로 계정 권한이 변경되지는 않습니다.

권한은 로그인, 인프라 배포, 앱 업로드, 운영자 조회를 구분합니다. 앱에 배포 권한을 함께 부여할 필요는 없습니다.

## 1. 로컬 CLI 로그인

`aws login --profile workload --region ap-northeast-2`를 사용할 IAM 사용자·그룹·역할에는 다음 AWS 관리형 정책이 필요합니다.

```text
arn:aws:iam::aws:policy/SignInLocalDevelopmentAccess
```

이 정책은 `signin:AuthorizeOAuth2Access`, `signin:CreateOAuth2Token`을 AWS Sign-In의 OAuth 클라이언트에 허용합니다. 로그인할 수 있게 하는 정책이며 S3 버킷 생성·업로드 권한까지 주지는 않습니다. IAM Identity Center를 사용하는 조직은 관리자가 준비한 SSO 프로필과 `aws sso login` 흐름을 따릅니다. [AWS 관리형 정책 원문](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/SignInLocalDevelopmentAccess.html)

로그인 오류의 HTTP 상태만으로 권한 부족을 확정할 수는 없습니다. 계정의 로그인 방식, 위 정책 적용 여부, 명시적 거부·조직 SCP·권한 경계도 관리자가 확인해야 합니다. 이 작업을 위해 관리자 전체 권한이나 장기 액세스 키를 새로 만들 필요는 없습니다.

## 2. 버킷을 만드는 배포자

제공된 `infra/log-bucket.json`을 `shopping-workload-logs` 스택으로 배포할 때 사용할 정책 예시는 [`infra/deployer-policy.example.json`](../infra/deployer-policy.example.json)입니다. `REPLACE_WITH_ACCOUNT_ID`를 실제 계정 ID로 바꿉니다. 서울 리전의 해당 스택·CLI 변경 집합과 `shopping-workload-logs-*` 버킷 이름에 범위를 제한한 예시입니다.

| 작업 | 필요한 동작 |
|---|---|
| 템플릿 검사 | `cloudformation:ValidateTemplate`, `cloudformation:GetTemplateSummary` |
| 생성·갱신 및 CLI 변경 집합 | `cloudformation:CreateStack`, `UpdateStack`, `CreateChangeSet`, `DescribeChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet`, `ListChangeSets` |
| 배포 대기·문제 확인 | `cloudformation:DescribeStacks`, `DescribeStackEvents`, `DescribeStackResource`, `DescribeStackResources`, `ListStackResources`, `GetTemplate` |
| 버킷 생성 | `s3:CreateBucket` |
| 템플릿의 버킷 설정 | `s3:PutEncryptionConfiguration`, `PutBucketPublicAccessBlock`, `PutBucketOwnershipControls`, `PutBucketPolicy`, `PutBucketTagging` |
| 버킷 존재·설정 확인 | 예시의 `s3:ListBucket`, `GetBucket*`와 암호화·수명주기·복제 등 구성 조회 동작 |

표에서 서비스 접두사를 생략한 동작도 같은 행의 접두사를 사용합니다. 예를 들어 `UpdateStack`은 `cloudformation:UpdateStack`입니다. `s3:GetBucket*`는 해당 이름 범위의 버킷 구성 조회이며, 객체 본문을 읽는 `s3:GetObject`는 포함하지 않습니다. CloudFormation 리소스 제공자가 설정을 읽는 데 필요한 구성 조회를 포함했지만 객체 삭제·업로드 권한은 부여하지 않습니다.

이 파일은 **해당 템플릿과 CLI 배포 경로를 위한 제한된 예시**입니다. 계정의 SCP·권한 경계·기존 명시적 거부까지 우회하거나 모든 CloudFormation 기능을 허용하는 정책은 아닙니다. 스택 이름·CLI 변경 집합 이름·생성되는 버킷 이름을 바꾸면 정책의 ARN 범위도 맞춰야 합니다. 운영자는 실제 생성/검증 오류에 나온 리소스와 동작을 확인해 범위를 조정해야 합니다. 콘솔의 계정 전체 목록은 예시에 포함하지 않았습니다. 콘솔 목록이 필요하면 별도 조회 정책에 `cloudformation:ListStacks`를 `Resource: "*"`로 추가할 수 있습니다.

CloudFormation에는 스택 권한뿐 아니라 템플릿의 AWS 리소스를 만들 권한도 필요합니다. 별도 CloudFormation 서비스 역할을 지정한다면 배포자에게 그 역할의 `iam:PassRole`이 추가로 필요하지만, 제공 명령은 서비스 역할을 생성·지정하지 않습니다. [CloudFormation IAM 안내](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/control-access-with-iam.html), [S3 API별 필요한 동작](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html)

## 3. 서비스 런타임 업로더

[`infra/uploader-policy.example.json`](../infra/uploader-policy.example.json)의 버킷명을 바꾸면 다음 범위만 허용합니다.

```text
Action: s3:PutObject, s3:GetObject
Resource: arn:aws:s3:::<실제 버킷명>/workload-events/*
```

`PutObject`는 SSE-S3 AES256과 조건부 업로드 헤더가 있는 요청만 허용합니다. 같은 키에 객체가 이미 있어 `412 PreconditionFailed`가 반환되면 `GetObject`로 그 본문을 읽고 동일한 이벤트인지 확인한 뒤에만 전송 완료로 처리합니다. 읽기 실패나 본문 불일치는 대기 상태로 남습니다. `GetObject`는 같은 접두사의 별도 정책문이며 업로드 전용 암호화·`If-None-Match` 조건을 적용하지 않습니다.

현재 앱에는 S3 목록·삭제나 CloudWatch 권한이 필요하지 않습니다. 기본 접두사 `workload-events`를 바꾸면 두 정책문도 같은 경로로 바꾸세요. AWS 실행 환경에서는 이 정책을 부여한 기존 실행 역할을 사용하는 방식이 적합합니다.

## 4. 원격 기록을 확인하는 운영자

[`infra/log-reader-policy.example.json`](../infra/log-reader-policy.example.json)은 직접 S3 API/CLI로 기록을 확인할 때 사용하는 예시입니다.

| 대상 | 동작 | 범위 |
|---|---|---|
| 이벤트 목록 | `s3:ListBucket` | 버킷 ARN + `workload-events` 접두사 조건 |
| JSON 본문 | `s3:GetObject` | 해당 버킷의 `workload-events/*` |

S3 콘솔의 모든 버킷 목록을 보려면 별도로 `s3:ListAllMyBuckets`(`Resource: "*"`)가 필요할 수 있고, 선택한 버킷의 리전 조회에는 `s3:GetBucketLocation`을 추가할 수 있습니다. 이 콘솔 편의 권한은 서비스 업로더에 필요하지 않아 예시 파일에서는 제외했습니다.

## 5. 실제 CloudWatch Logs를 추후 선택하는 경우

현재 `/log-console`은 **CloudWatch 스타일의 로컬 기록 화면**이며 AWS CloudWatch와 통신하지 않습니다. 아래 권한만 추가해도 CloudWatch 업로드가 자동으로 생기지는 않습니다. 실제 전송 어댑터를 구현할 때 필요한 동작을 구분해 준비합니다.

| 역할·기능 | 동작 |
|---|---|
| 로그 그룹 최초 생성 | `logs:CreateLogGroup` |
| 로그 스트림 최초 생성 | `logs:CreateLogStream` |
| 이벤트 전송 | `logs:PutLogEvents` |
| 그룹·스트림 찾기 | `logs:DescribeLogGroups`, `logs:DescribeLogStreams` |
| 스트림 읽기·필터 | `logs:GetLogEvents`, `logs:FilterLogEvents` |
| Logs Insights 쿼리 실행·결과 | `logs:StartQuery`, `logs:GetQueryResults`; 취소 기능을 쓸 때만 `logs:StopQuery` |
| 보관 기간 설정을 선택한 경우 | `logs:PutRetentionPolicy` |

그룹과 스트림이 미리 준비되면 업로더에는 `PutLogEvents`만 주고 생성 권한을 제외할 수 있습니다. 생성·쓰기는 선택한 로그 그룹/스트림 ARN으로 제한합니다. 목록·쿼리 결과처럼 특정 리소스 ARN 제한을 지원하지 않는 동작은 별도 조회 정책에서 `Resource: "*"`가 필요할 수 있습니다. 콘솔 기능에 따라 추가적인 읽기 동작이 필요하므로 운영자가 사용할 화면/API만 선택합니다. [CloudWatch Logs 권한 참조](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/permissions-reference-cwl.html)
