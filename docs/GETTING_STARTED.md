# 처음 받은 팀원을 위한 실행 안내

Hourahead는 쇼핑몰 운영 담당자가 매 정각, 직전 168시간의 실제 판매전표 집계를 입력해 다음 한 시간의 작업량 단계(없음·보통·혼잡)를 확인하는 로컬 프로토타입입니다. 정확한 건수나 필요한 직원 수를 예측하지 않습니다. [서비스 정의 점검표](SERVICE_DEFINITION_REVIEW.md)에 입력·출력과 적용 범위를 정리했습니다.

이 안내는 **Windows PowerShell 5.1 이상**에서 각자 자신의 PC에 배포하는 절차입니다. 설치 폴더 `Hourahead-TeamTest`, 포트 `18001`, AWS를 호출하지 않는 `local` 모드를 사용합니다. 기존 `Hourahead` 설치 및 포트 `8001`과 구분합니다. 다른 사람의 `127.0.0.1` 주소에 접속하는 방식이 아닙니다.

## 1. 도구 준비

| 도구 | 설치 방법 | 이 프로젝트에서 쓰는 곳 |
|---|---|---|
| Git | [Git 공식 Windows 설치 페이지](https://git-scm.com/install/windows)에서 설치 | 지정 브랜치 받기 |
| uv | [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)의 WinGet 방법: `winget install --id=astral-sh.uv -e` | Python 3.12 및 잠긴 의존성 설치, 검사 |
| Node.js | [Node 공식 다운로드](https://nodejs.org/en/download)에서 지원 중인 LTS와 Windows 설치 프로그램 선택 | 배포 전 JavaScript 계약 검사; 별도 프런트엔드 빌드는 없음 |

공식 설치 안내는 2026-11-16에 확인했습니다. Node는 스크립트 요구사항인 20 이상이면서 현재 지원 중인 LTS를 선택합니다. Python 3.12는 배포 스크립트가 uv로 준비하므로 별도 가상환경 활성화가 필요하지 않습니다. 최초 설치에는 Python·패키지 다운로드용 인터넷 연결과 디스크 여유 공간이 필요합니다.

설치 후 새 PowerShell 창에서 확인합니다.

```powershell
$PSVersionTable.PSVersion
git --version
uv --version
node --version
```

명령을 찾지 못하면 설치와 PATH를 먼저 확인합니다. uv·Node 실행 파일 위치를 알고 있다면 배포 명령에 `-UvPath 'C:\tools\uv.exe' -NodePath 'C:\tools\node.exe'`처럼 실제 경로를 추가할 수 있습니다. 회사 PC의 설치·PowerShell 실행 정책이 차단하면 오류를 기록하고 관리자에게 승인된 설치·실행 방법을 요청합니다. 이 안내는 실행 정책을 변경하거나 우회하지 않습니다.

## 2. 전달용 브랜치 받기

이 안내의 기준은 **`feature/service-handoff`**입니다. 아직 병합되지 않은 동안 기본 `main`에는 이 기능이 없을 수 있으므로 지정 브랜치를 받습니다. 아래 이름의 폴더가 이미 있다면 새 빈 폴더 이름을 사용하세요.

```powershell
Set-Location "$env:USERPROFILE\Documents"
git clone --branch feature/service-handoff --single-branch https://github.com/WHC-MTSC/shopping-week04.git shopping-week04-teamtest
Set-Location .\shopping-week04-teamtest
git branch --show-current
git rev-parse HEAD
```

브랜치 출력이 `feature/service-handoff`인지 확인하고 커밋 SHA를 아래 결과표에 기록합니다. 저장소 접근 권한 또는 브랜치 오류가 발생하면 전달자에게 확인합니다. 다른 브랜치로 대신 실행하지 않습니다. 이후 모든 명령은 `pyproject.toml`, `uv.lock`, `scripts`가 있는 이 저장소 루트에서 실행합니다.

## 3. 독립된 로컬 서비스 배포

```powershell
$teamInstallRoot = "$env:USERPROFILE\Documents\Hourahead-TeamTest"
.\scripts\deploy-local.ps1 -Action Deploy -InstallRoot $teamInstallRoot -Port 18001 -Backend local
.\scripts\deploy-local.ps1 -Action Status -InstallRoot $teamInstallRoot -Port 18001
```

배포는 소스를 새 릴리스에 복사하고 잠긴 Python 의존성, Ruff·형식·타입·Python·JavaScript 검사를 실행합니다. HTTP 상태와 실제 모델 응답 검증까지 통과한 뒤 현재 릴리스를 바꿉니다. 처음에는 다운로드와 검사 때문에 시간이 걸릴 수 있습니다. 실패하면 마지막 오류를 제출하고 검사를 생략하지 않습니다. 포트가 사용 중이면 해당 프로세스를 임의로 종료하지 말고 담당자와 별도 포트를 정해 모든 명령과 URL에 일관되게 적용합니다.

브라우저에서 [사용자 화면](http://127.0.0.1:18001/), [로그 화면](http://127.0.0.1:18001/log-console)을 엽니다. PowerShell에서도 실제 서비스를 확인합니다.

```powershell
Invoke-RestMethod http://127.0.0.1:18001/health
Invoke-RestMethod http://127.0.0.1:18001/metadata
Invoke-RestMethod http://127.0.0.1:18001/logging
```

기대 결과는 `/health`의 `status=ready`, `/metadata`의 모델 버전·SHA256, `/logging`의 `destination=local`입니다. 모델 버전과 SHA는 결과표에 기록합니다. **로컬 저장 확인은 AWS 수집 성공이 아닙니다.** 데이터는 설치 폴더의 `data/local/events.sqlite3`에 남고 배포 진단 파일은 `process-logs`에 있습니다.

이어서 실제 HTTP API의 예측·중복 요청·정답 수정·로그 연결을 자동 검사합니다. 재검사 시 기존 보고서를 보존하려면 다른 파일명을 사용합니다.

```powershell
.\scripts\verify-service.ps1 -BaseUrl http://127.0.0.1:18001 -OutputPath runtime/acceptance.json
```

기대 보고서는 `status=passed`, `data_source=demo`, `event_count=3`, `logging_destination=local`입니다. 새 예측 1건과 정답 revision 1·2가 저장되며 중복 요청은 같은 기록을 재사용합니다. 검사에 쓰는 건수 `0`과 T/F `false`는 **합성 API 계약 검사값이며 검증된 실제 정답이 아닙니다.** 보고서도 이 의미를 명시하고 `remote_delivery_verified=false`로 남깁니다. 이 예측 ID의 정답을 수동으로 변경하면 재시작 검사의 기준이 달라지므로, 화면 확인은 별도의 새 예측으로 진행합니다.

## 4. 확인 가능한 샘플로 예측·정답 저장

1. 화면의 **샘플 불러오기**를 누릅니다. 목표 시각 `2011-09-27T06:00:00`과 그 직전 168시간 이력을 확인합니다. 원본 영국 현지 시각이며 한국 시각으로 변환하지 않습니다.
2. **데이터 출처**가 **실습·테스트 데이터 (학습 대상 제외)**인지 확인한 뒤 **예측 실행**을 누르고 단계·세 확률·모델 버전·예측 ID를 확인합니다. 확률은 보정되지 않은 모델 출력입니다.
3. 이 샘플의 실제 정답은 [평가 CSV](../artifacts/test-predictions.csv)의 같은 목표 시각 행에 있는 **0건**입니다. 원본을 수정하지 않은 샘플에 대해 정답 입력 방식 **실제 판매전표 건수**, 값 `0`을 저장합니다. 임의의 T/F나 건수를 실제 정답처럼 제출하지 않습니다.
4. **저장 상태 새로고침** 후 로컬 보관 상태를 확인합니다. `/log-console`에서 예측 ID로 필터하여 prediction과 feedback이 연결되고 `data_source=demo`인지 확인합니다. 예측 ID와 피드백 ID 또는 revision을 기록합니다.
5. 페이지를 새로 열고 같은 예측 ID로 조회해 저장한 결과를 확인합니다. 잘못된 CSV 행 수나 누락 시간도 제출해 오류가 안내되는지 확인할 수 있습니다. 오류 입력의 결과를 실제 예측 성공으로 기록하지 않습니다.

자신의 데이터를 사용하려면 UTF-8 CSV `hour,count`와 **정확히 168개의 연속된 정각 행**이 필요합니다. 양수 판매 거래에 해당하는 전표 건수를 세되 거래가 없는 시간은 0건으로 포함합니다. 건수는 0 이상 100,000 이하 정수이며 마지막 행은 목표 정각의 한 시간 전입니다. 원본과 같은 판매전표 집계·시각 체계를 확인하고, 대상 시간이 끝난 뒤 실제 결과를 확인했을 때만 정답을 제출합니다. 정답을 모르면 저장하지 않습니다. 새 점포의 정확도나 업무 효과는 이 과거 샘플로 검증되지 않습니다.

**데이터 출처**의 기본값은 실습입니다. 실제 집계임을 확인한 경우에만 **확인된 실제 판매 집계**를 명시적으로 선택합니다. CSV 업로드나 직접 편집은 선택한 출처를 유지하며 자동으로 실제 집계로 바꾸지 않습니다. 샘플 버튼은 출처를 실습으로 되돌립니다.

## 5. 중지·다시 시작

같은 PowerShell 창에서는 앞에서 지정한 설치 경로를 계속 사용합니다. 새 창에서는 저장소 루트로 이동하고 `$teamInstallRoot`를 다시 지정합니다.

```powershell
.\scripts\deploy-local.ps1 -Action Stop -InstallRoot $teamInstallRoot -Port 18001
.\scripts\deploy-local.ps1 -Action Start -InstallRoot $teamInstallRoot -Port 18001
.\scripts\deploy-local.ps1 -Action Status -InstallRoot $teamInstallRoot -Port 18001
.\scripts\verify-service.ps1 -BaseUrl http://127.0.0.1:18001 -PreviousReport runtime/acceptance.json -OutputPath runtime/persistence.json
```

재시작 검사는 기존 보고서의 예측·정답 ID와 revision 2가 유지되는지 확인하며 새 예측을 만들지 않습니다. `runtime/persistence.json`의 `status=passed`, `verification=persistence`를 확인하고, 사람이 저장한 별도 예측 ID도 화면에서 다시 조회합니다. 자동 API 검사가 통과해도 버튼·입력·결과 화면을 직접 확인한 것으로 기록하지 않습니다. 이전 릴리스가 있는 경우에만 `-Action Rollback`을 같은 설치 경로로 실행할 수 있습니다. 처음 인수 시험에서는 자동 시작 등록이나 후보 모델 교체를 수행하지 않습니다. 별도 배포·복구·모델 승인 절차는 [배포 안내](DEPLOYMENT.md)에 있습니다.

## 6. 선택 사항: 실제 AWS 수집

AWS 없이 위 시험을 완료할 수 있습니다. 중앙 수집이 필요한 팀원은 **자신에게 허용된 AWS 프로필**, 리전, 기존 로그 그룹·스트림 또는 S3 버킷을 먼저 확인합니다. 다른 사람의 비밀번호, 키, 인증 캐시를 복사하지 않습니다. 필요한 권한과 연결 명령은 [AWS 권한](AWS_PERMISSIONS.md)과 [AWS 연결](AWS_SETUP.md)을 따릅니다. Windows 배포 스크립트의 원격 모드는 CloudWatch이며, S3는 별도 실행 설정을 사용합니다.

CloudWatch 연결에는 지정 스트림의 `logs:PutLogEvents`, 원격 대조·다운로드에는 지정 그룹의 `logs:FilterLogEvents`가 필요합니다. 앱은 리소스나 IAM 정책을 생성하지 않습니다. 본인 프로필로 로그인한 뒤 명시적으로 CloudWatch 배포를 선택하고, 검증 CLI의 `--base-url http://127.0.0.1:18001` 및 같은 프로필·리전·대상을 지정합니다. 수락 상태 `cloudwatch_sent`에 더해 원격 이벤트의 ID·JSON 일치가 확인되어야 실제 AWS 검증 성공으로 기록합니다. `/log-console`은 이 경우에도 로컬 기록 화면입니다.

## 7. 팀원 직접 실행 결과 제출

**2026-11-16 기준 이 문서만으로 신규 팀원이 직접 배포·사용했다는 결과는 아직 미완료입니다.** 기존 자동 검사, 작성자 PC 검증, 과거 데이터 재생은 해당 팀원의 실행 증거를 대신하지 않습니다. 아래 빈 서식을 실행한 사람이 채워 전달합니다. 실패·차단도 그대로 남기며 비밀번호·토큰·키·인증 화면은 첨부하지 않습니다.

| 항목 | 직접 기록할 내용 |
|---|---|
| 실행자 / 실행 시각 | 이름 또는 팀 식별자, 시간대 포함 시각 |
| 환경 | Windows·PowerShell·uv·Node 버전 |
| 소스 | 브랜치, `git rev-parse HEAD` 결과 |
| 배포 | 설치 경로, 포트, Deploy/Status 성공 또는 오류 단계 |
| 모델 | `/metadata`의 model_version, model_sha256 |
| 자동 API 검사 | acceptance.json·persistence.json의 status, 검사 시각, 예측 ID; 합성 demo 검사임을 표시 |
| 예측·정답 | 목표 시각, prediction_id, feedback_id 또는 revision, demo/user 구분, 정답 출처 |
| 화면·재시작 | 예측/정답/로그 화면 확인, Stop→Start 후 같은 ID 조회 결과 |
| AWS | 미사용 / 실패 / 원격 대조 성공; 성공이면 안전하게 정리한 검증 보고서 |
| 증거 | 화면 캡처 및 오류/검증 결과 파일의 경로 또는 공유 링크 |
| 최종 결과 | 완료 / 부분 완료 / 차단과 원인 |

팀원 신원은 위 제출 기록으로 관리합니다. 앱에는 사용자 인증이나 담당자 이름 기록 기능이 없으므로 이벤트만으로 실행자를 식별하지 않습니다.
