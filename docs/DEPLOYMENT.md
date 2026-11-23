# 현재 Windows PC에 배포하기

이번 배포 대상은 현재 사용자 PC입니다. 앱은 `127.0.0.1`에서 실행하고 AWS에는 허용된 기존 CloudWatch 로그 그룹·스트림으로 로그만 보냅니다. EC2·컨테이너·IAM 리소스를 만들지 않습니다. 기본값은 AWS를 호출하지 않는 `local` 저장입니다.

## 준비와 첫 배포

Windows PowerShell 5.1 이상, `uv`, Node.js 20 이상, Python 3.12 설치에 필요한 네트워크가 필요합니다. `uv sync --locked --python 3.12`가 잠긴 의존성을 설치하며, 코드 검사는 현재 CI와 같은 Ruff·형식·BasedPyright·pytest·JavaScript 입력 계약 테스트를 모두 실행합니다. 검사를 건너뛰는 옵션은 없습니다.

저장소 루트에서 실행합니다. 기본 포트는 기존 개발 서버의 8000을 피한 **8001**입니다.

```powershell
.\scripts\deploy-local.ps1 -Action Deploy
.\scripts\deploy-local.ps1 -Action Status
```

`uv`나 `node`가 PATH에 없으면 `-UvPath C:\tools\uv.exe -NodePath C:\tools\node.exe`처럼 실제 실행 파일을 지정합니다. Python 다운로드 대신 이미 설치된 Python 3.12를 사용하려면 `-PythonPath C:\tools\python.exe`를 지정합니다. `-SourcePath`로 다른 완성된 소스 디렉터리를 지정할 수 있습니다. 설치 위치는 현재 사용자 문서 폴더의 `Hourahead`입니다. 이 위치는 Codex 같은 MSIX 앱의 AppData 가상화 때문에 Windows 로그인 시 설치를 찾지 못하는 문제를 피합니다.

소스·모델은 `releases/<새 ID>`에 복사합니다. `runtime`, `.env`, AWS 인증 캐시는 복사 대상에 없습니다. 릴리스마다 별도 가상환경을 설치하고 검사한 뒤 임시 포트에서 `/health`와 `workload.model_check`로 실제 예측·모델 해시를 대조합니다. 사전 검증은 로컬 저장만 사용합니다. 선택한 서비스 포트에서도 검증을 통과한 경우에만 `current.json`을 바꿉니다. 실패한 배포는 기존 릴리스를 현재 상태로 유지하며, 기존 서버를 중지한 뒤 실패했다면 이전 릴리스를 다시 시작합니다. 이전 코드와 모델은 자동 삭제하지 않습니다.

## 시작·중지·되돌리기

```powershell
.\scripts\deploy-local.ps1 -Action Start
.\scripts\deploy-local.ps1 -Action Stop
.\scripts\deploy-local.ps1 -Action Rollback
```

이 스크립트가 기록한 PID, 생성 시각, 릴리스 Python 실행 경로, uvicorn 명령과 포트를 확인한 프로세스만 관리합니다. Windows 가상환경 런처의 서버 자식 프로세스도 확인하며, Windows 콘솔 호스트는 직접 종료하지 않습니다. 재부팅 뒤 PID가 재사용되면 시작 명령은 이전 기록을 만료된 것으로 처리하고 새 포트를 확인합니다. 중지 명령은 소유권을 확인할 수 없는 PID를 거부합니다. 다른 프로세스가 포트를 쓰면 임의로 비우지 않습니다. 재배포와 되돌리기는 한 번에 하나만 실행하세요. 장애 진단 로그는 설치 폴더의 `process-logs`에 있습니다.

## 선택한 모델 배포

`-ModelPath`를 생략하면 기존 `artifacts/model.json`을 사용합니다. 후보를 지정하면 설치·프로세스 변경 전에 모델과 보고서의 SHA256·버전 일치, 두 파일의 용도 `real_feedback`, 보고서의 `quality_gate_passed`와 `promotion_eligible`가 모두 JSON boolean `true`인지 검사합니다. 실습 모델·실패한 평가·문자열 `"true"`는 거부합니다. 보고서는 기본적으로 모델 옆 `report.json`이며 `-ModelReportPath`로 명시할 수 있습니다.

허용된 후보는 기존 기준 모델로 품질 검사를 통과한 뒤 새 릴리스의 `serving-model.json`과 `serving-report.json`으로 복사합니다. 복사 후 두 파일의 SHA256을 다시 대조합니다. 원본 기준 모델은 덮어쓰지 않으며, 셸에 `WORKLOAD_MODEL_PATH`가 있어도 품질 검사는 릴리스의 기준 모델을 사용한 뒤 원래 환경변수의 존재 여부와 값을 복원합니다.

```powershell
.\scripts\deploy-local.ps1 -Action Deploy -ModelPath .\runtime\training\candidate\model.json
# 보고서 파일명이 다를 때:
.\scripts\deploy-local.ps1 -Action Deploy -ModelPath .\runtime\training\candidate\model.json -ModelReportPath .\runtime\training\candidate\evaluation.json
```

이 예시는 기본 `local` 저장을 사용합니다. 기존 CloudWatch 수집 설정을 유지하려면 아래 AWS 배포 예시의 `-Backend`, `-Profile`, `-Region`, `-LogGroup`, `-LogStream` 옵션도 함께 지정합니다. 실제 서버의 모델 해시와 예측까지 확인해야 현재 릴리스로 전환합니다. 선택한 모델 파일은 릴리스 상태에 기록되므로 시작·되돌리기도 해당 릴리스 모델을 사용합니다. 보고서가 통과해도 실제 운영 환경의 성능을 별도로 확인해야 합니다. 품질 기준을 통과하지 못한 실습 후보는 현재 릴리스로 승격하지 않으며, 필요하면 운영 서비스와 분리된 로컬 임시 서버에서만 서빙 동작을 확인합니다.

현재 구현은 PC의 로컬 주소만 열기 때문에 다른 팀원 PC에서는 바로 접속할 수 없습니다. 같은 PC에서 순서대로 테스트하거나, 별도로 허용된 네트워크 공개·접근 제어 방식을 정한 뒤 팀원 접속을 진행해야 합니다.

## 현재 사용자 로그인 시 자동 실행

검증된 릴리스가 준비된 뒤 선택적으로 설치합니다.

```powershell
.\scripts\deploy-local.ps1 -Action Start -InstallStartup
```

현재 사용자 Startup 폴더에 `Hourahead.lnk`를 만들고 설치 폴더의 스크립트를 숨김 PowerShell로 실행합니다. 관리자 권한이나 실행 정책 우회는 사용하지 않습니다. Windows 로그인 시 시작하며 로그아웃 상태의 시스템 서비스는 아닙니다. 자동 실행을 해제하려면 현재 사용자의 Startup 폴더에서 `Hourahead.lnk`만 삭제합니다. 이미 실행 중인 자체 서버는 다시 만들지 않고 상태를 검증합니다.

## AWS 로그 연결

실제 수집이 허용된 프로필과 **이미 있는** 로그 그룹·스트림이 확보되면 명시적으로 선택합니다.

```powershell
.\scripts\deploy-local.ps1 -Action Deploy -Backend cloudwatch -Profile workload -Region ap-northeast-2 -LogGroup /hourahead/existing -LogStream existing-stream
```

버킷·로그 그룹·스트림·IAM은 생성하지 않습니다. 실행 환경의 AWS 프로필·임시 인증만 사용하고 키를 코드나 이미지에 넣지 않습니다. 프로필·리전·그룹·스트림 조합마다 별도 로컬 로그 DB를 사용하여 다른 목적지의 전송 완료 상태를 재사용하지 않습니다. 로그 DB는 `data` 아래에 있어 코드 릴리스 교체·되돌리기 후에도 보존됩니다. 로컬 모드의 시험 로그는 CloudWatch 모드 DB에 섞이지 않습니다.

`/health`와 모델 검증 성공은 AWS 전송 성공을 뜻하지 않습니다. `/logging`의 대기/실패 상태와 실제 AWS CloudWatch 객체 조회를 별도로 대조해야 합니다. CLI 로그인 세션이 만료되면 다시 로그인해야 하므로, Startup 등록 자체가 AWS 인증 갱신까지 보장하지 않습니다. CloudWatch 모드는 기본값으로 자동 승격하지 않습니다.

스크립트 문법 검사만으로 실제 PC 배포·시작·중지·복구가 검증되는 것은 아닙니다. 실제 실행 결과와 화면은 배포 실행 단계에서 별도로 기록합니다.
