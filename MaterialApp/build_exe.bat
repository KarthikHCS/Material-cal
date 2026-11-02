@echo off
cd /d %~dp0
pip install pyinstaller pywebview pymupdf openpyxl numpy cadquery-ocp
python -m PyInstaller --noconfirm --clean --windowed --name MaterialUsageCalculator --add-data "%cd%\index.html;." --collect-all OCP --collect-all fitz --collect-all pymupdf --hidden-import tkinter --hidden-import engine --hidden-import excel_out --hidden-import pdfparse --hidden-import stepgeo --distpath dist --workpath build --specpath build desktop.py
pause
