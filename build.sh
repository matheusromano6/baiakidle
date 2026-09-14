#!/bin/bash
set -e
python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller

PLAYWRIGHT_DRIVER=$(python3 -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver'))")

python3 -m PyInstaller --onefile --windowed --name BaiakIdleBot --add-data "$PLAYWRIGHT_DRIVER:playwright/driver" gui.py

echo ""
echo "Copiando routines.json para dist..."
cp routines.json dist/routines.json

echo ""
echo "Build pronto em dist/BaiakIdleBot(.app) (ja com routines.json)"
