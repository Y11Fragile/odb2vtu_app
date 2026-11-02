@echo off
rem =====================================================================
rem  ODB -> VTU converter launcher (debug, console stays open)
rem =====================================================================
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "GUI=%HERE%app\odb2vtu_gui.py"
set "PYEXE="

if exist "%HERE%python_exe.txt" set /p PYEXE=<"%HERE%python_exe.txt"
if defined PYEXE if not exist "%PYEXE%" set "PYEXE="

if not defined PYEXE (
  for %%P in (
    "D:\ProgramData\miniconda3\envs\hm_pd\python.exe"
    "%LOCALAPPDATA%\miniconda3\python.exe"
    "C:\ProgramData\miniconda3\python.exe"
    "D:\ProgramData\miniconda3\python.exe"
    "C:\ProgramData\anaconda3\python.exe"
    "D:\ProgramData\anaconda3\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  ) do (
    if not defined PYEXE if exist %%P set "PYEXE=%%~P"
  )
)

if not defined PYEXE (
  for /f "delims=" %%i in ('where python.exe 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%i"
  )
)

if not defined PYEXE (
  echo [ERROR] python.exe was not found. Write its full path into python_exe.txt.
  pause
  exit /b 1
)

echo Python : %PYEXE%
echo GUI    : %GUI%
echo.
"%PYEXE%" "%GUI%" %*
echo.
echo [exit code %ERRORLEVEL%]
pause
endlocal