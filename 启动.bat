@echo off
rem =====================================================================
rem  ODB -> VTU converter launcher (GUI, no console window)
rem  Finds a Python 3 with tkinter, then starts app\odb2vtu_gui.py
rem =====================================================================
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "GUI=%HERE%app\odb2vtu_gui.py"
set "PYEXE="

if not exist "%GUI%" (
  echo [ERROR] app\odb2vtu_gui.py not found next to this script.
  pause
  exit /b 1
)

rem 1) optional override: first line of python_exe.txt next to this script
if exist "%HERE%python_exe.txt" set /p PYEXE=<"%HERE%python_exe.txt"
if defined PYEXE if not exist "%PYEXE%" set "PYEXE="

rem 2) conda / anaconda installs seen on this kind of machine
if not defined PYEXE (
  for %%P in (
    "D:\ProgramData\miniconda3\envs\hm_pd\pythonw.exe"
    "%LOCALAPPDATA%\miniconda3\pythonw.exe"
    "C:\ProgramData\miniconda3\pythonw.exe"
    "D:\ProgramData\miniconda3\pythonw.exe"
    "C:\ProgramData\anaconda3\pythonw.exe"
    "D:\ProgramData\anaconda3\pythonw.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  ) do (
    if not defined PYEXE if exist %%P set "PYEXE=%%~P"
  )
)

rem 3) pythonw on PATH
if not defined PYEXE (
  for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
  )
)

rem 4) py launcher
if not defined PYEXE (
  for /f "delims=" %%i in ('py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do (
    if exist "%%i" set "PYEXE=%%i"
  )
)

if not defined PYEXE (
  echo [ERROR] Python 3 with tkinter was not found.
  echo.
  echo   Install Python 3.x, or write the full path of your pythonw.exe
  echo   into python_exe.txt next to this script.
  echo.
  pause
  exit /b 1
)

start "" "%PYEXE%" "%GUI%" %*
endlocal