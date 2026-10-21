@echo off
chcp 65001 >nul
title Content Factory — панель сериалов

echo.
echo  Content Factory - панель сериалов
echo  ====================================
echo  Обзор / Эпизоды / Сценарии / Аналитика / Сериалы / Задачи / Журнал
echo.

start "Content Factory API" cmd /k "cd /d %~dp0 && py -X utf8 utils/api_server.py"
timeout /t 3 /nobreak >nul
start "" "http://localhost:5000"

echo  [OK] Панель -> http://localhost:5000
echo  Чтобы остановить сервер - закройте окно "Content Factory API"
echo.
