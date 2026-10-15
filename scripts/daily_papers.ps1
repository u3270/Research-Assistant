$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

$Root = "C:\path\to\research-assistant"
$Python = "C:\path\to\Python314\python.exe"
$LogDirectory = Join-Path $Root "logs"
$Today = Get-Date -Format "yyyy-MM-dd"
$LogFile = Join-Path $LogDirectory "$Today.log"
$PromptFile = Join-Path $Root "prompts\daily_prompt.txt"
$CandidatesFile = Join-Path $Root "data\candidates.json"

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null

# 작업 스케줄러에서는 새 프로세스가 사용자 환경변수를 바로 읽도록 명시합니다.
$env:IEEE_API_KEY = [Environment]::GetEnvironmentVariable(
    "IEEE_API_KEY",
    "User"
)

Start-Transcript -Path $LogFile -Append

try {
    Set-Location $Root

    Write-Output "[$(Get-Date)] Daily research workflow started."

    if (-not $env:IEEE_API_KEY) {
        throw "IEEE_API_KEY is not configured."
    }

    # 1. IEEE Xplore 및 arXiv 검색
    Write-Output "[$(Get-Date)] Searching research-paper sources."

    & $Python (Join-Path $Root "scripts\search_papers.py")

    if ($LASTEXITCODE -ne 0) {
        throw "search_papers.py failed with exit code $LASTEXITCODE."
    }

    if (-not (Test-Path $CandidatesFile)) {
        throw "The candidates file was not generated: $CandidatesFile"
    }

    # 2. Claude가 후보 평가, Zotero 저장, Obsidian 노트 생성
    Write-Output "[$(Get-Date)] Starting Claude Code."

    $Prompt = Get-Content $PromptFile -Raw |
    claude --print `
      --permission-mode auto `
      --allowedTools "Read,Write,Edit,mcp__zotero__*"
        $Prompt

    if ($LASTEXITCODE -ne 0) {
        throw "Claude Code failed with exit code $LASTEXITCODE."
    }

    Write-Output "[$(Get-Date)] Daily research workflow completed."

    # 3. Obsidian 노트 Git 커밋 및 푸시
    $Vault = "C:\path\to\YourVault\Research\Daily Papers\Paper"

    Set-Location $Vault

    git add .

    $Changes = git status --porcelain

    if ($Changes) {
        git commit -m "Add daily paper digest $Today"

        if ($LASTEXITCODE -ne 0) {
            throw "Git commit failed."
        }

        git push origin main

        if ($LASTEXITCODE -ne 0) {
            throw "Git push failed."
        }

        Write-Output "[$(Get-Date)] Obsidian note committed and pushed."
    }
    else {
        Write-Output "[$(Get-Date)] No Obsidian changes to commit."
    }

    Set-Location $Root

}
catch {
    Write-Error "Workflow failed: $($_.Exception.Message)"
    exit 1
}
finally {
    Stop-Transcript
}