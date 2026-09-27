@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente virtual...
    py -3.13 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Nao foi possivel criar o venv. Instale o Python 3.13.
        pause
        exit /b 1
    )
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo Falha ao instalar dependencias.
    pause
    exit /b 1
)

start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000"
echo Painel em http://127.0.0.1:8000  (Ctrl+C para parar)
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
