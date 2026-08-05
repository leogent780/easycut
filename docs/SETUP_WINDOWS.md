# Windows 로컬 개발환경 설정 (비개발자용)

이 문서는 개발 지식이 없는 사용자가 Windows PC에서 이 프로젝트를 내려받아
테스트까지 돌려보는 과정을 담은 요약본입니다. 시각적으로 따라가기 편한 버전은
대화 중 공유된 링크(Artifact)를 참고하세요.

## 1) 필요한 프로그램 설치

Windows 검색창(⊞ 키)에 `PowerShell`을 입력해서 열고, 아래를 한 줄씩 실행:

```powershell
winget install --id Git.Git -e --source winget
winget install --id Python.Python.3.12 -e --source winget
winget install --id Microsoft.VisualStudioCode -e --source winget
winget install --id Gyan.FFmpeg -e --source winget
```

설치가 끝나면 PowerShell 창을 닫았다가 다시 열어서 `git --version`으로 확인.

## 2) VS Code로 코드 내려받기

1. VS Code 실행
2. `Ctrl+Shift+P` → `Git: Clone` 입력 후 선택
3. 저장소 주소: `https://github.com/leogent780/easycut`
4. 저장 폴더 선택 (예: 문서)
5. "지금 연 창에서 열기" 클릭

## 3) 가상환경 만들고 의존성 설치

상단 메뉴 `Terminal → New Terminal`을 연 뒤:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
playwright install chromium
```

`.venv\Scripts\Activate.ps1` 실행 시 실행 정책 오류가 뜨면 아래를 한 번만 실행 후 재시도:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

## 4) 테스트로 설치 확인

```powershell
pytest tests/ -v
```

`50 passed`가 뜨면 로컬 환경 설정 완료. 이 단계는 실제 유튜브 계정이나 API 키 없이도
통과합니다 (코드 로직만 검사).

## 다음 단계

실제로 유튜브 채널에 영상을 만들어 올리려면 [`README.md`](../README.md)의
"Google Cloud OAuth 설정"부터 이어서 진행하세요.
