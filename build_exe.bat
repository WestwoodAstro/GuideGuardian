@echo off
rem Builds dist\PHD2Guard.exe (single file, no console window) with PyInstaller.
rem Uses a throw-away virtual environment under %TEMP% so your global Python stays clean.
setlocal
set "HERE=%~dp0"
set "VENV=%TEMP%\phd2guard_build_venv"

if not exist "%VENV%\Scripts\python.exe" (
    python -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :fail
"%VENV%\Scripts\python.exe" -m pip install --quiet -r "%HERE%requirements.txt" pyinstaller || goto :fail

"%VENV%\Scripts\python.exe" "%HERE%make_icon.py" || goto :fail

"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name PHD2Guard --icon "%HERE%assets\phd2guard.ico" ^
    --distpath "%HERE%dist" --workpath "%TEMP%\phd2guard_build" --specpath "%TEMP%\phd2guard_build" ^
    "%HERE%phd2_guard.py" || goto :fail

echo.
echo Built: %HERE%dist\PHD2Guard.exe
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
