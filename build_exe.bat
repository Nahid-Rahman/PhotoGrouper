@echo off
setlocal
title Photo Grouper - building .exe
cd /d "%~dp0"
echo ============================================
echo   Building PhotoGrouper.exe
echo   (takes 5-10 minutes, keep this window open)
echo ============================================
echo.
set "PYEXE="
for %%C in ("py -3.12" "py -3.11" "py -3.13" "python" "py -3") do if not defined PYEXE call :try %%~C
if not defined PYEXE (
  echo Python 3.11-3.13 not found. Install it from https://www.python.org/downloads/ ^(tick 'Add to PATH'^). Found:
  where python
  where py
  pause
  exit /b
)
echo Using Python: %PYEXE%
"%PYEXE%" --version
echo.
if not exist .venv\Scripts\python.exe "%PYEXE%" -m venv .venv
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Could not create the virtual environment ^(.venv^).
  pause
  exit /b
)
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install pyinstaller -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installing libraries failed. See the error above.
  pause
  exit /b
)
"%VPY%" -m pip install -r requirements-optional.txt || echo (Optional HEIC/RAW support could not be installed. Those photos will be skipped.)
echo.
set "EXTRA="
"%VPY%" -c "import pillow_heif" 2>nul && set "EXTRA=%EXTRA% --collect-binaries pillow_heif"
"%VPY%" -c "import rawpy" 2>nul && set "EXTRA=%EXTRA% --collect-binaries rawpy"
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name PhotoGrouper ^
  --icon assets\icon.ico --add-data "assets\icon.ico;." --paths src --collect-binaries onnxruntime %EXTRA% src\photo_grouper.py
if not exist dist\PhotoGrouper.exe (
  echo.
  echo Building the .exe failed. See the error above.
  pause
  exit /b
)
copy /y dist\PhotoGrouper.exe PhotoGrouper.exe >nul
echo.
echo ============================================
echo   Done!  PhotoGrouper.exe ready.
echo ============================================
explorer /select,"%~dp0PhotoGrouper.exe"
pause
exit /b

:try
for /f "delims=" %%P in ('%* -c "import sys; print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
exit /b
