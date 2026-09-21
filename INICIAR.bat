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
    echo [1/3] Creando entorno virtual e instalando dependencias...
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

REM 3) Abrir el navegador tras 3s (mientras arranca el servidor)
echo [2/3] Abriendo http://localhost:8000 en el navegador...
start "" /min cmd /c "timeout /t 3 /nobreak >nul && explorer http://localhost:8000"

echo [3/3] Iniciando servidor. Cierra esta ventana o pulsa Ctrl+C para detener.
echo.
".venv\Scripts\python.exe" -m uvicorn backend.main:app --port 8000

echo.
echo El servidor se detuvo.
pause
