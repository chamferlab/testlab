@echo off
rem Update TestLab running as an NSSM service: stop, back up the database,
rem pull, update the virtualenv and migrate, start again.
rem
rem   scripts\update.bat                  service "TestLab" (or TESTLAB_SERVICE)
rem   scripts\update.bat MyServiceName
rem
rem Run from an administrator Command Prompt (or right-click > Run as
rem administrator): nssm needs it to stop and start the service.
rem Backups go to data\backups\testlab-<date>-<time>.sqlite3.
setlocal
for %%I in ("%~dp0..") do set "REPO_DIR=%%~fI"
set "PYTHON=%REPO_DIR%\.venv\Scripts\python.exe"

set "SERVICE=%~1"
if not defined SERVICE set "SERVICE=%TESTLAB_SERVICE%"
if not defined SERVICE set "SERVICE=TestLab"

rem --- Checks before anything is stopped -------------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo TestLab: run this as administrator ^(right-click the Command Prompt ^> Run as administrator^).
    goto end_fail
)
where nssm >nul 2>&1
if errorlevel 1 (
    echo TestLab: nssm was not found on the PATH.
    goto end_fail
)
nssm status "%SERVICE%" >nul 2>&1
if errorlevel 1 (
    echo TestLab: there is no service "%SERVICE%". Give its name: scripts\update.bat ^<service name^>
    goto end_fail
)
if not exist "%PYTHON%" (
    echo TestLab: no virtual environment at %REPO_DIR%\.venv - run scripts\setup.bat first.
    goto end_fail
)

cd /d "%REPO_DIR%"
set "CHANGES="
for /f "delims=" %%L in ('git status --porcelain --untracked-files^=no') do set "CHANGES=1"
if defined CHANGES (
    echo TestLab: files in %REPO_DIR% were edited on this machine; git pull would refuse or mix them in:
    git status --short --untracked-files=no
    echo Nothing was stopped or changed.
    goto end_fail
)

rem The database file as Django sees it (TESTLAB_DATA_DIR / TESTLAB_DATABASE_URL); empty if not SQLite.
set "DB_FILE="
rem "call" first: for /f strips the outer quotes of a command that starts with one.
for /f "usebackq delims=" %%D in (`call "%PYTHON%" app\manage.py shell -v 0 -c "from django.conf import settings; d = settings.DATABASES['default']; print(d['NAME'] if d['ENGINE'].endswith('sqlite3') else '')"`) do set "DB_FILE=%%D"
for /f "delims=" %%H in ('git rev-parse HEAD') do set "OLD_HEAD=%%H"

rem --- Stop, back up, pull ---------------------------------------------------
echo.
echo == Stopping service %SERVICE%
nssm stop "%SERVICE%"

echo.
echo == Backing up the database
if not defined DB_FILE (
    echo    Not a SQLite database: no file backup made. Back it up yourself before continuing.
    pause
) else if not exist "%DB_FILE%" (
    echo    No database file at %DB_FILE% yet: nothing to back up.
) else (
    for /f "delims=" %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"
    call :backup || goto start_old
)

echo.
echo == Pulling the latest version
git pull --ff-only
if errorlevel 1 (
    echo TestLab: git pull failed - the code was not changed.
    goto start_old
)
echo.
echo Changes in this update:
for /f %%C in ('git rev-list --count %OLD_HEAD%..HEAD') do if "%%C"=="0" echo    ^(none: already up to date^)
git log --oneline %OLD_HEAD%..HEAD

rem --- Packages, Chromium, migrations ---------------------------------------
echo.
echo == Updating packages and the database ^(scripts\devenv.py setup^)
"%PYTHON%" "%REPO_DIR%\scripts\devenv.py" setup
if errorlevel 1 (
    echo.
    echo TestLab: the update did not finish - see the messages above. The service is left STOPPED.
    if defined BACKUP echo The database before the update is in %BACKUP%
    echo Fix the problem and run scripts\update.bat again, or go back to the previous version:
    echo     git reset --hard %OLD_HEAD%
    if defined BACKUP echo     copy /y "%BACKUP%" "%DB_FILE%"
    echo     nssm start "%SERVICE%"
    goto end_fail
)

echo.
echo == Starting service %SERVICE%
nssm start "%SERVICE%"
if errorlevel 1 goto end_fail
echo.
echo TestLab is updated and running again.
if defined BACKUP echo Database backup: %BACKUP%
pause
exit /b 0

:start_old
echo Starting the service again with the version it had.
nssm start "%SERVICE%"
goto end_fail

:end_fail
echo.
echo Update did not finish - see the messages above.
pause
exit /b 1

:backup
if not exist "%REPO_DIR%\data\backups" mkdir "%REPO_DIR%\data\backups"
set "BACKUP=%REPO_DIR%\data\backups\testlab-%STAMP%.sqlite3"
copy /y "%DB_FILE%" "%BACKUP%" >nul
if errorlevel 1 (
    echo TestLab: the backup to %BACKUP% failed.
    set "BACKUP="
    exit /b 1
)
echo    %DB_FILE%
echo    -^> %BACKUP%
exit /b 0
