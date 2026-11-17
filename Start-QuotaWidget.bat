@echo off
cd /d "%~dp0"
set "PYW=C:\Users\thieu\AppData\Local\Programs\Python\Python312\pythonw.exe"
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0app.py" %*
) else (
  python "%~dp0app.py" %*
)
