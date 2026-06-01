@echo off
chcp 65001 > nul
REM ============================================================
REM   창이 절대 즉시 닫히지 않도록 cmd /k 안에서 자기 자신을 재실행.
REM   (더블클릭/파서 오류/예기치 못한 종료에도 창과 오류 메시지가 유지된다)
REM ============================================================
if /i "%~1"=="/inner" goto INNER
cmd /k ""%~f0" /inner"
exit /b

:INNER
setlocal enabledelayedexpansion
title ai-agent 자동매매 시스템 런처

set "ROOT=C:\ai-team"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"

cd /d "%ROOT%"
if errorlevel 1 (
    echo [오류] %ROOT% 폴더로 이동 실패. 폴더 존재 여부를 확인하세요.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo            ai-agent 자동매매 시스템 런처
echo ============================================================
echo.

REM ============================================================
REM   사전 점검 (traidair → venv → 패키지 → 설정 → 디렉터리)
REM ============================================================

REM --- [1/5] traidair 프록시 서버 (가장 먼저 자동 백그라운드 시작) ---
echo [1/5] traidair 프록시 서버 ^(localhost:3000^)...
call :ENSURE_TRAIDAIR

REM --- HTS 화면 브라우저 자동 오픈 (서버 준비 후) ---
echo       HTS 화면을 브라우저에서 엽니다 ^(http://localhost:3000/hts^)...
start "" "http://localhost:3000/hts"

REM --- [2/5] Python 가상환경 ---
echo [2/5] Python 가상환경 확인...
if not exist "%PYTHON%" (
    echo       [오류] 가상환경이 없습니다: %PYTHON%
    echo       아래 명령으로 생성하세요:
    echo         cd /d "%ROOT%"
    echo         python -m venv .venv
    echo         ".venv\Scripts\python.exe" -m pip install httpx pydantic pyyaml pytest pytest-asyncio
    echo.
    pause
    exit /b 1
)
echo       OK ^(%PYTHON%^)

REM --- [3/5] 필수 패키지 ---
echo [3/5] 필수 패키지 확인...
"%PYTHON%" -c "import httpx, pydantic, yaml" 1>nul 2>nul
if errorlevel 1 (
    echo       [오류] 필수 패키지가 없습니다 ^(httpx / pydantic / pyyaml^).
    echo       아래 명령으로 설치하세요:
    echo         "%PYTHON%" -m pip install httpx pydantic pyyaml pytest pytest-asyncio
    echo.
    pause
    exit /b 1
)
echo       OK

REM --- [4/5] 설정 파일 ---
echo [4/5] 설정 파일 확인...
if not exist "%ROOT%\config\mode.yaml" (
    echo       [오류] config\mode.yaml 이 없습니다.
    pause
    exit /b 1
)
if not exist "%ROOT%\config\strategy_params.yaml" (
    echo       [오류] config\strategy_params.yaml 이 없습니다.
    pause
    exit /b 1
)
if not exist "%ROOT%\config\kis_api.yaml" (
    if exist "%ROOT%\config\kis_api.yaml.example" (
        echo       [경고] config\kis_api.yaml 이 없어 예시에서 복사합니다.
        echo              실행 전 KIS 키/계좌 값을 채우세요.
        copy /y "%ROOT%\config\kis_api.yaml.example" "%ROOT%\config\kis_api.yaml" >nul
    ) else (
        echo       [오류] config\kis_api.yaml 및 예시 파일이 모두 없습니다.
        pause
        exit /b 1
    )
)
echo       OK

REM --- [5/5] 런타임 디렉터리 보장 ---
echo [5/5] 런타임 디렉터리 확인...
for %%D in (state data "data\journal" logs) do (
    if not exist "%ROOT%\%%~D" md "%ROOT%\%%~D" >nul 2>nul
)
echo       OK
echo.


REM ============================================================
REM   메인 메뉴
REM ============================================================
:MENU
echo.
echo ============================================================
echo                 무엇을 시작하시겠습니까?
echo ============================================================
echo   1^) 모의투자/백테스트 화면 열기 ^(브라우저 HTS^)
echo   2^) 실전투자 시작 ^(scripts\run_live.py^)
echo   3^) Claude Code 열기
echo   4^) 종료
echo ============================================================

choice /c 1234 /n /m "선택 (1-4): "
if errorlevel 4 goto QUIT
if errorlevel 3 goto CLAUDE
if errorlevel 2 goto LIVE
if errorlevel 1 goto PAPER
goto MENU


REM ─── 모의투자/백테스트 (브라우저 HTS 가 유일한 실행 경로) ───
REM   백테스트는 traidair 서버가 단독으로 실행/관리한다(한 번에 하나만). start.bat 은
REM   더 이상 run_paper.py 를 직접 띄우지 않는다 — 그래야 "브라우저에서 백테스트를
REM   누르면 start.bat 쪽 모의투자가 종료되는" 이중 실행 충돌이 사라진다.
REM   (개발용 직접 실행: 터미널에서 "%PYTHON% scripts\run_paper.py" — 단, 브라우저
REM    백테스트와 동시에 켜지 마세요. 엔진이 중복 실행을 자동 차단합니다.)
:PAPER
echo.
echo --------- [모의투자/백테스트] ---------
echo   백테스트는 브라우저 HTS 화면의 '▶ 백테스트' 버튼으로 실행합니다.
echo   서버가 실행 상태를 단독 관리하므로 한 번에 하나의 백테스트만 돕니다.
echo.
call :ENSURE_TRAIDAIR
echo       HTS 화면을 다시 엽니다 ^(http://localhost:3000/hts^)...
start "" "http://localhost:3000/hts"
echo.
echo   브라우저에서 '▶ 백테스트' 버튼을 누르면 09:00~15:20 가상매매가 진행됩니다.
echo   (이 창은 그대로 유지됩니다 — 닫지 마세요.)
goto MENU


REM ─── 실전 ───
:LIVE
echo.
echo ============================================================
echo                     [경고] 실전 모드
echo  실 자금이 투입됩니다. config\mode.yaml 이 live 인지 확인.
echo  모드 전환: scripts\switch_mode.py --to live
echo ============================================================
choice /c YN /n /m "실전 모드로 진입하시겠습니까? (Y/N): "
if errorlevel 2 goto MENU
echo.
echo --------- [실전 모드] ---------
echo   Ctrl+C 또는 scripts\kill_switch.py 로 종료
echo.
"%PYTHON%" "%ROOT%\scripts\run_live.py"
set "PYEXIT=!errorlevel!"
echo.
echo --------- [실전 종료] (종료 코드: !PYEXIT!) ---------
if not "!PYEXIT!"=="0" (
    echo [오류] run_live.py 가 비정상 종료했습니다. 위 메시지를 확인하세요.
    pause
)
goto MENU


REM ─── Claude Code ───
:CLAUDE
echo.
echo Claude Code 를 새 창에서 시작합니다...
start "Claude Code (C:\ai-team)" cmd /k "cd /d C:\ai-team && claude"
echo 새 창이 열렸습니다. 본 런처는 메뉴 유지.
goto MENU


REM ─── 종료 ───
:QUIT
echo.
choice /c YN /n /m "traidair 백그라운드 서버도 종료할까요? (Y/N): "
if errorlevel 2 (
    echo traidair 는 유지된 채 런처만 종료합니다.
) else (
    echo traidair 종료 시도...
    taskkill /FI "WINDOWTITLE eq TraidAIr*" /F >nul 2>nul
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr :3000 ^| findstr LISTENING') do (
        taskkill /PID %%P /F >nul 2>nul
    )
    echo 완료.
)
echo.
echo 런처를 종료합니다.
endlocal
REM cmd /k 호스트까지 완전히 닫는다
exit


REM ============================================================
REM   서브루틴: traidair 자동 시작 (포트 3000 확인 → 스킵 / 백그라운드 시작)
REM ============================================================
:ENSURE_TRAIDAIR
netstat -ano | findstr :3000 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo       이미 포트 3000에서 실행 중 ^(traidair 추정^). 스킵.
    goto :eof
)
if not exist "C:\traidair\server.js" (
    echo       [경고] C:\traidair\server.js 없음 + 포트 3000 미점유.
    echo              traidair 가 없으면 KIS/DART 호출이 모두 실패합니다.
    echo              C:\traidair 에 레포를 두거나 config\kis_api.yaml 의
    echo              traidair_base_url 을 올바른 주소로 설정하세요.
    goto :eof
)
where node >nul 2>nul
if errorlevel 1 echo       [경고] node ^(Node.js^) 를 PATH 에서 못 찾음 — traidair 시작 실패 가능.
echo       C:\traidair 에서 node server.js 백그라운드 시작...
echo              ^(콘솔 로그는 C:\traidair\logs\server.log 에 영구 기록^)
start "TraidAIr (KIS 프록시)" /MIN cmd /k "cd /d C:\traidair && if not exist logs mkdir logs && node server.js >> logs\server.log 2>&1"
echo       서버 부팅 대기 ^(포트 3000, 최대 15초^)...
set "TR_READY="
for /l %%i in (1,1,15) do (
    timeout /t 1 /nobreak >nul
    if not defined TR_READY (
        netstat -ano | findstr :3000 | findstr LISTENING >nul && set "TR_READY=1"
    )
)
if defined TR_READY (
    echo       traidair 준비 완료 ^(포트 3000^).
) else (
    echo       [경고] 15초 내 포트 3000 확인 실패 — TraidAIr 창의 로그를 확인하세요.
)
set "TR_READY="
goto :eof
