@echo off

:: This special command changes the directory to where the batch file is located.
cd /d "%~dp0"

ECHO Starting Philia Translator from the correct path...

:: Now that we are in the correct folder, this command will work.
venv\Scripts\python.exe main.py

ECHO Program has finished.
pause