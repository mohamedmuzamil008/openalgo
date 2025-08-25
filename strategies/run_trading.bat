@echo off
cd /d %~dp0
cd ..
python strategies/run_live_trading.py --symbols live --max-symbols 30 %*
echo.
echo Press any key to exit...
pause > nul
