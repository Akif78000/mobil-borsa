@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python bulunamadi.
    echo https://www.python.org/downloads/ adresinden Python 3'u indirip kurun.
    echo Kurulum ekraninda "Add python.exe to PATH" kutusunu MUTLAKA isaretleyin.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo '.env' dosyasi olusturuldu. Simdi Not Defteri acilacak.
    echo Once BINANCE / TELEGRAM bilgilerinizi girip kaydedin, sonra Not Defteri'ni kapatin.
    pause
    notepad ".env"
)

:menu
echo.
echo ============================
echo   mobil-borsa Trade Sistemi
echo ============================
echo 1) Botu calistir  (trade_bot.py - surekli calisir, durdurmak icin Ctrl+C)
echo 2) Backtest calistir  (backtest.py - gecmis veride test eder)
echo 3) .env dosyasini duzenle
echo 4) Cikis
echo.
set /p secim="Seciminiz (1-4): "

if "%secim%"=="1" (
    python trade_bot.py
    goto menu
)
if "%secim%"=="2" (
    python backtest.py
    goto menu
)
if "%secim%"=="3" (
    notepad ".env"
    goto menu
)
if "%secim%"=="4" (
    exit /b 0
)
echo Gecersiz secim, tekrar deneyin.
goto menu
