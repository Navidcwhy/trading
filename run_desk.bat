@echo off
REM Kör en desk och startar om den automatiskt om processen dör.
REM   run_desk.bat            = huvuddesken (reglerna i config.json)
REM   run_desk.bat daytrade   = daytrade-desken (config.daytrade.json)
REM Varje desk körs i sitt eget fönster. Samma desk kan inte startas två gånger.
cd /d %~dp0
set PROFILE_ARG=--profile main
if not "%~1"=="" set PROFILE_ARG=--profile %~1
:loop
python -m desk run %PROFILE_ARG%
echo Desk avslutades - startar om om 15 sekunder (Ctrl+C for att avbryta)
timeout /t 15 /nobreak >nul
goto loop
