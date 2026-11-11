@echo off
rem Сборка портативного exe: dist\VilknzPrizeWheel.exe (нужен Python 3 с pip)
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install --quiet --upgrade pyinstaller || goto :error
python -m PyInstaller --noconfirm --clean --onefile --console --name VilknzPrizeWheel ^
  --icon "%~dp0assets\icon.ico" ^
  --add-data "%~dp0index.html;." ^
  --add-data "%~dp0assets\icon.ico;assets" ^
  --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" ^
  "%~dp0server.py" || goto :error
echo.
echo Готово: dist\VilknzPrizeWheel.exe
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
