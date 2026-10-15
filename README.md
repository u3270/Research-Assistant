# AI Research Assistant

> Personal AI-powered research assistant for wireless positioning and RFSoC-based communication systems.

매일 최신 논문을 자동으로 수집하고, Claude Code를 이용해 연구 관련성을 평가한 뒤,
Zotero 및 Obsidian에 자동으로 정리하고 GitHub에 백업하는 자동화 시스템입니다.

---

## 🚀 처음 사용하는 분을 위한 설치 가이드 (Getting Started)

이 저장소에는 **API 키가 포함되어 있지 않습니다.** 각자 본인의 키를 발급받아
환경변수로 등록해야 정상 동작합니다.

### 1. 저장소 클론

```powershell
git clone <이 저장소 URL>
cd research-assistant
```

### 2. Python 의존성 설치 (Python 3.14 권장)

```powershell
py -3.14 -m pip install -r requirements.txt
```

### 3. API 키 / 환경변수 등록 (필수)

| 환경변수 | 발급처 | 필수 여부 |
|---|---|---|
| `IEEE_API_KEY` | [IEEE Xplore API Portal](https://developer.ieee.org/) | **필수** |
| `SEMANTIC_SCHOLAR_API_KEY` | [Semantic Scholar API](https://www.semanticscholar.org/product/api) | 선택 (없으면 rate limit 낮음) |
| `CROSSREF_MAILTO` | 본인 이메일 주소 | 선택 (Crossref polite pool 권장) |

Windows 사용자 환경변수 등록 (PowerShell):

```powershell
[Environment]::SetEnvironmentVariable("IEEE_API_KEY", "발급받은_키", "User")
[Environment]::SetEnvironmentVariable("SEMANTIC_SCHOLAR_API_KEY", "발급받은_키", "User")
[Environment]::SetEnvironmentVariable("CROSSREF_MAILTO", "you@example.com", "User")
```

> 등록 후 PowerShell 창을 새로 열어야 값이 반영됩니다.

### 4. 로컬 경로 수정 (필수)

`scripts/daily_papers.ps1` 상단의 경로는 원작성자 PC 기준입니다.
**본인 환경에 맞게 반드시 수정하세요.**

```powershell
$Root   = "C:\...\research-assistant"                       # 이 저장소 경로
$Python = "C:\...\Python314\python.exe"                     # 본인 Python 경로
$Vault  = "C:\...\YourVault\Research\Daily Papers\Paper"     # 일일 노트 폴더(= Git 저장소 루트)
```

### 5. 사전 준비 도구

- [Claude Code](https://claude.com/claude-code) 설치 및 로그인
  (무인 자동 실행 시에는 **7번의 장기 토큰** 설정 권장 — 대화형 로그인 세션은 만료됨)
- **Zotero MCP** 서버 연결 (`claude mcp list`로 확인)
- Obsidian Vault 준비 (Git 연동 시 원격 저장소도 설정)

### 6. 실행

```powershell
# 전체 워크플로우
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\daily_papers.ps1"

# 논문 검색만
py -3.14 .\scripts\search_papers.py
```

### 7. (선택) 매일 자동 실행

Windows 작업 스케줄러(`taskschd.msc`)에 `daily_papers.ps1`을 매일 07:30 실행으로 등록하세요.

**⚠️ 무인 실행에는 장기 인증 토큰이 필요합니다.**
대화형 로그인(`claude /login`)의 OAuth 세션은 주기적으로 만료되는데, 스케줄러의
비대화형 실행은 재로그인을 할 수 없어 그대로 실패합니다(노트가 조용히 생성되지 않음).
이를 막으려면 **1년짜리 장기 토큰**(`claude setup-token`)을 발급해 환경변수로 등록하세요:

```powershell
# 1) 대화형 터미널에서 실행 → 브라우저에서 승인
claude setup-token

# 2) 승인 후 "터미널에 출력된 토큰"을 복사

# 3) User 환경변수로 영구 등록
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_OAUTH_TOKEN", "<터미널에_출력된_토큰>", "User")
```

확인(새 터미널): `"say ok" | claude -p` → `ok`가 나오면 정상.

- 구독(Pro/Max)에 과금됩니다. `ANTHROPIC_API_KEY`가 설정돼 있으면 그쪽이 **우선**하므로,
  구독 토큰을 쓰려면 `ANTHROPIC_API_KEY`는 비워 두세요.
- 토큰은 **약 1년 후 만료**되며 자동 갱신이 없으니, 만료 전 재발급 리마인더를 걸어두세요.
- 토큰은 저장소나 `CLAUDE.md`에 넣지 말고 **환경변수로만** 보관하세요.

---

## 연구 주제 변경

검색 키워드와 Zotero 주제 태그는 모두 **`config/topics.json` 한 파일**에서 관리됩니다.
관심 연구 주제가 바뀌면 이 파일의 `topics`만 수정하면 됩니다 — 검색어, 관련도 가중치,
주제 태그가 함께 갱신됩니다. 편집 방법은 파일 상단 주석을 참고하세요.
(노이즈 필터 등 안정적인 설정은 `scripts/search_papers.py`에 그대로 둡니다.)

---

## Architecture

```
                                      ┌──────────────────────────┐
                                      │ Windows Task Scheduler   │
                                      │ (Daily 07:30)            │
                                      └────────────┬─────────────┘
                                                   │
                                                   ▼
                                   daily_papers.ps1 (Main Orchestrator)
                                                   │
            ┌──────────────────────────────────────┼──────────────────────────────────────┐
            ▼                                      ▼                                      ▼
   search_papers.py                    Claude Code (CLI)                         Logging
            │                                      │
            ▼                                      ▼
 ┌──────────────────────┐               Read candidates.json
 │ IEEE Xplore API      │                       │
 │ arXiv API            │                       ▼
 │ Semantic Scholar API │              Paper Ranking (A/B/C)
 │ Crossref API         │                       │
 └──────────┬───────────┘                       ▼
            ▼                          Select Top 6 + Korean Summary
  candidates.json                               │
                                       ┌─────────┴─────────┐
                                       ▼                   ▼
                                  Zotero MCP        Obsidian Vault
                                       │                   │
                                       ▼                   ▼
                                 seen_dois.txt      Git Commit & Push → GitHub
```

## Features

- 최신 논문 자동 검색 (IEEE / arXiv / Semantic Scholar / Crossref)
- 중복 논문 제거 (기계적 점수 없음 — 관련성 평가는 Claude가 수행)
- Claude Code 기반 관련성 판단 · A/B/C 평가 및 한국어 요약 생성
- Zotero 자동 저장 및 태그 부여
- Obsidian Daily Research Note 생성
- GitHub 자동 백업 / Windows Task Scheduler 지원

## Requirements

- Windows 11, Python 3.14, PowerShell, Git
- Claude Code, Zotero Desktop, Obsidian
- MCP Servers: Zotero (필수), GitHub / MATLAB (선택)

## Project Structure

```
research-assistant/
├── scripts/
│   ├── daily_papers.ps1          # 메인 오케스트레이터
│   └── search_papers.py          # 논문 검색
├── prompts/
│   └── daily_prompt.txt          # Claude에게 전달되는 프롬프트
├── config/
│   └── topics.json               # 연구 주제 단일 소스 (검색어 + Zotero 태그)
├── data/
│   ├── candidates.json           # 검색 결과 후보
│   └── seen_dois.txt             # 중복 방지용 DOI 목록
├── CLAUDE.md                     # Claude 동작 규칙
├── requirements.txt
└── README.md
```

## Commands

```powershell
# Claude만 실행
Get-Content .\prompts\daily_prompt.txt -Raw |
  claude --print --permission-mode auto --allowedTools "Read,Write,Edit,mcp__zotero__*"

# 스케줄러 테스트 실행
Start-ScheduledTask -TaskName "Daily Research Assistant"

# MCP 목록 확인
claude mcp list
```

## Version History

### v1.1 (2026.10)

- 일일 노트 저장 위치를 Obsidian 메인 Vault로 이전
- 키워드 점수 체계 제거 → Claude가 후보를 직접 평가·A/B/C 등급·선정 (최대 6편)
- 연구 주제를 `config/topics.json` 단일 소스로 통합 (검색어 + Zotero 태그)
- 무인 실행용 장기 인증 토큰(`claude setup-token`) 도입

### v1.0 (2026.07)

최초 구현:

- IEEE Xplore / arXiv / Semantic Scholar / Crossref API 연동
- Claude Code 기반 논문 평가 및 한국어 요약
- Zotero MCP 연동 및 자동 저장
- Obsidian Daily Note 자동 생성
- GitHub 자동 Commit & Push
- Windows Task Scheduler 자동 실행
- VS Code MCP

## Acknowledgements

Thank you to arXiv for use of its open access interoperability.
