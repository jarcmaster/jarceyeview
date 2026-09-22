@echo off
setlocal
cd /d "%~dp0"
title JARC'S EYE View

echo ============================================
echo    JARC'S EYE View - iniciando...
echo ============================================
echo.

REM 1) Crear entorno virtual e instalar dependencias si no existe
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creando entorno virtual e instalando dependencias...
    py -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r "backend\requirements.txt"
    echo.
)

REM 2) Crear .env desde la plantilla si no existe
if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
    echo [AVISO] Se creo .env desde la plantilla. Edita .env y pon tus claves antes de usar todo.
    echo.
)

REM 3) Liberar el puerto 8000 si hay un servidor previo ocupandolo
echo [2/4] Liberando el puerto 8000 si estaba ocupado...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

REM 4) Abrir el navegador tras 3s (mientras arranca el servidor)
echo [3/4] Abriendo http://localhost:8000 en el navegador...
start "" /min cmd /c "timeout /t 3 /nobreak >nul && explorer http://localhost:8000"

echo [4/4] Iniciando servidor (auto-recarga activada). Cierra esta ventana o pulsa Ctrl+C para detener.
echo.
".venv\Scripts\python.exe" -m uvicorn backend.main:app --port 8000 --reload --reload-dir backend

echo.
echo El servidor se detuvo.
pause
