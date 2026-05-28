@echo off
chcp 65001 > nul
title ai-agent 자동매매 시스템 런처

cd /d C:\ai-team

REM ============================================================
REM   부팅 단계
REM ============================================================
echo.
echo ============================================================
echo            ai-agent 자동매매 시스템 런처
echo ============================================================
echo.

REM --- traidair 백그라운드 시작 ---
echo [1/2] traidair 프록시 서버 확인...
netstat -ano | findstr :3000 | findstr LISTENING > nul
if %errorlevel% equ 0 (
    echo       이미 포트 3000에서 실행 중 ^(traidair 추정^). 스킵.
) else (
    if not exist C:\traidair\server.js (
        echo       [경고] C:\traidair\server.js 없음. 백그라운드 시작 생략.
    ) else (
        echo       C:\traidair 에서 node server.js 백그라운드 시작...
        start "TraidAIr (KIS 프록시)" /MIN cmd /k "cd /d C:\traidair && node server.js"
        echo       3초 대기 ^(서버 부팅^)...
        timeout /t 3 /nobreak > nul
    )
)

REM --- Python 가상환경 확인 ---
echo [2/2] Python 가상환경 확인...
if not exist C:\ai-team\.venv\Scripts\python.exe (
    echo       [오류] .venv 가상환경 없음. 다음 명령으로 환경 구성:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install httpx pydantic pyyaml pytest pytest-asyncio
    echo.
    pause
    exit /b 1
)
echo       OK


REM ============================================================
REM   메인 메뉴
REM ============================================================
:MENU
echo.
echo ============================================================
echo                 무엇을 시작하시겠습니까?
echo ============================================================
echo   1^) 모의투자 시작 ^(scripts\run_paper.py^)
echo   2^) 실전투자 시작 ^(scripts\run_live.py^)
echo   3^) Claude Code 열기
echo   4^) 종료
echo ============================================================

choice /c 1234 /n /m "선택 (1-4): "
if errorlevel 4 goto QUIT
if errorlevel 3 goto CLAUDE
if errorlevel 2 goto LIVE
if errorlevel 1 goto PAPER


REM ─── 모의투자 ───
:PAPER
echo.
echo --------- [모의투자 모드] ---------
echo   Ctrl+C 로 종료. 비상정지: scripts\kill_switch.py
echo.
C:\ai-team\.venv\Scripts\python.exe scripts\run_paper.py
echo.
echo --------- [모의투자 종료] ---------
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
C:\ai-team\.venv\Scripts\python.exe scripts\run_live.py
echo.
echo --------- [실전 종료] ---------
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
    REM 윈도우 제목으로 매칭하여 traidair cmd 창만 종료
    taskkill /FI "WINDOWTITLE eq TraidAIr*" /F > nul 2>&1
    REM 백업: 포트 3000 점유 PID 강제 종료
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr :3000 ^| findstr LISTENING') do (
        taskkill /PID %%P /F > nul 2>&1
    )
    echo 완료.
)
echo.
echo 런처를 종료합니다.
exit /b 0
