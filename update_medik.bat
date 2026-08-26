@echo off
REM ===================================================================
REM  Double-click this to bring the laptop up to date and verify it.
REM
REM  Three attempts, in increasing order of force, because a plain
REM  `git pull` fails silently often enough that "I ran it" and "it
REM  worked" have repeatedly turned out to be different things.
REM ===================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set BRANCH=claude/interactive-broker-python-connect-knsid8

echo ===================================================================
echo  MEDIK UPDATE
echo ===================================================================
echo.
echo Before:
git log --oneline -1
echo.

echo [1/4] fetching from GitHub...
git fetch origin %BRANCH%
if errorlevel 1 (
    echo.
    echo    FETCH FAILED. No network, or GitHub credentials expired.
    echo    Nothing was changed.
    goto :done
)

echo [2/4] checking out the branch...
git checkout %BRANCH% 2>nul
if errorlevel 1 echo    (already on it, or checkout blocked - continuing)

echo [3/4] updating...
git pull --ff-only origin %BRANCH%
if errorlevel 1 (
    echo    plain pull refused - stashing local edits and retrying
    git stash
    git pull --ff-only origin %BRANCH%
    if errorlevel 1 (
        echo    still refused - forcing to match GitHub
        echo    ^(discards local edits to tracked files; your own .bat
        echo     files and logs are untracked and survive^)
        git reset --hard origin/%BRANCH%
    )
)

echo.
echo [4/4] verifying...
echo.
echo After:
git log --oneline -1
echo.

REM The real test is not what git says but what is in the file.
set FOUND=
for /f %%I in ('findstr /C:"make_quote_feed" examples\medik_etf_live.py 2^>nul ^| find /c /v ""') do set FOUND=%%I
if "%FOUND%"=="" set FOUND=0

for /f %%I in ('find /c /v "" ^< examples\medik_etf_live.py') do set LINES=%%I

echo    examples\medik_etf_live.py : %LINES% lines  ^(expected 1131^)
echo    make_quote_feed present    : %FOUND% match^(es^)  ^(expected 5^)
echo.
if %FOUND% GEQ 1 (
    echo ===================================================================
    echo   RESULT: UP TO DATE. The Client Portal quote path is present.
    echo ===================================================================
) else (
    echo ===================================================================
    echo   RESULT: STILL OUT OF DATE.
    echo   Send Claude everything printed above this line.
    echo ===================================================================
)

:done
echo.
pause
