
cd venv\Scripts
call activate.bat
cd ..\..
python -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload

pause
