@echo off
chcp 65001 >nul
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
echo 4) ACIL DURDUR  (bot calisirken bir sonraki kontrolde guvenli sekilde durur)
echo 5) Cikis
echo.
set /p secim="Seciminiz (1-5): "

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
    echo. > STOP_BOT
    echo STOP_BOT dosyasi olusturuldu. Calisan bot bir sonraki kontrolde duracak.
    echo Botu tekrar calistirmadan once bu dosyayi silin: del STOP_BOT
    pause
    goto menu
)
if "%secim%"=="5" (
    exit /b 0
)
echo Gecersiz secim, tekrar deneyin.
goto menu
