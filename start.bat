@echo off
cd /d "%~dp0"
title HELIOS V7
color 06
chcp 65001 >nul

echo.
echo  ==========================================
echo   H.E.L.I.O.S. V7 - A INICIAR
echo  ==========================================
echo.

REM Verifica .env
if not exist ".env" (
    echo  [ERRO] Ficheiro .env nao encontrado!
    pause
    exit /b 1
)

REM Carrega variaveis do .env
for /f "eol=# tokens=1,2 delims==" %%A in (.env) do (
    set "%%A=%%B"
)

REM Mata qualquer processo anterior na porta
echo  [0/2] A libertar portas anteriores...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765 " 2^>nul') do (
    taskkill /PID %%P /F >nul 2>&1
)

REM Build do frontend SEMPRE (garante codigo atualizado)
echo  [1/2] A construir o frontend...
cd frontend
call npm run build 2>&1
cd ..
echo  [1/2] Frontend pronto!
echo.

REM Lanca o HELIOS
echo  [2/2] A lancar o HELIOS...
echo.

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    py core\main.py
) else (
    py core\main.py
)

pause
