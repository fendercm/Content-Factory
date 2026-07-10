@echo off
title Content Factory — Admin Panel v2

echo.
echo  Content Factory - Admin Panel v2
echo  ====================================
echo  Живая лента агентов / Запуск из UI / Тесты
echo.

REM Запускаем Flask API сервер в отдельном окне
start "Content Factory API" cmd /k "cd /d %~dp0 && py utils/api_server.py"

REM Пауза чтобы сервер успел запуститься
timeout /t 3 /nobreak >nul

REM Открываем Admin Panel через Flask (не как файл — без проблем CORS)
start "" "http://localhost:5000"

echo  [OK] API сервер запущен -> http://localhost:5000
echo  [OK] Admin Panel открыта -> http://localhost:5000
echo.
echo  Страницы:
echo    Dashboard   - KPI + запуск пайплайна прямо из UI
echo    Feed        - живая лента сообщений агентов (SSE)
echo    Pipeline    - анимированная визуализация пайплайна
echo    Ideas       - все сгенерированные идеи и сценарии
echo    Tests       - быстрые тесты всех агентов
echo.
echo  Чтобы остановить сервер - закройте окно "Content Factory API"
echo.
