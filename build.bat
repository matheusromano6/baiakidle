@echo off
python -m pip install pyinstaller

for /f "delims=" %%i in ('python -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver'))"') do set PLAYWRIGHT_DRIVER=%%i

python -m PyInstaller --onefile --windowed --name BaiakIdleBot --icon icon.ico --add-data "%PLAYWRIGHT_DRIVER%;playwright\driver" gui.py
if errorlevel 1 (
    echo.
    echo ERRO: o build falhou. Veja as mensagens acima.
    pause
    exit /b 1
)

echo.
echo Copiando routines.json e icon.ico para dist...
copy /Y routines.json dist\routines.json >nul
copy /Y icon.ico dist\icon.ico >nul
if exist settings.json copy /Y settings.json dist\settings.json >nul

echo.
echo Build pronto em dist\BaiakIdleBot.exe (ja com routines.json, icon.ico e settings.json se existir)
pause
