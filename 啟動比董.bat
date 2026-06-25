@echo off
chcp 65001 >nul
title 比董股票AI顧問 - 自動啟動

echo.
echo  =============================================
echo    比董的股票AI顧問 一鍵啟動
echo  =============================================
echo.

cd /d "P:\AI 專用資料庫\技術分析系統\stock-linebot"
set PYTHON=C:\Users\saa\AppData\Local\Python\pythoncore-3.14-64\python.exe
set TOKEN=eh4JmYefRO+d+ddFC0tZxStaSeEpQRBWDCl+5udz0KQ6lzA89tskB3ElJ72EEmT1DsYAtoReJKpqcznXT061A4AV7Z6IOibgChq29ueN9TLv9v+/u6SVFRl015aQq/4HdtA+W0S9/OZi/I9sEzsQQwdB04t89/1O/w1cDnyilFU=

:: 關掉舊的
taskkill /F /IM cloudflared.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8090 "') do taskkill /F /PID %%a >nul 2>&1
timeout /t 1 /nobreak >nul

echo [1/3] 啟動 Bot...
start /B "" "%PYTHON%" cloud_bot.py > bot_out3.log 2> bot_err3.log
timeout /t 4 /nobreak >nul

echo [2/3] 啟動 cloudflared 隧道...
del cf_tunnel.log >nul 2>&1
start /B "" cloudflared.exe tunnel --url http://localhost:8090 --logfile cf_tunnel.log
timeout /t 10 /nobreak >nul

echo [3/3] 更新 LINE Webhook...
"%PYTHON%" -X utf8 -c "import requests,re,time; log=open('cf_tunnel.log',encoding='utf-8',errors='ignore').read(); m=re.search(r'https://\S+trycloudflare\.com',log); url=m.group(0) if m else ''; r=requests.put('https://api.line.me/v2/bot/channel/webhook/endpoint',headers={'Authorization':'Bearer %TOKEN%','Content-Type':'application/json'},json={'endpoint':url+'/webhook'}) if url else None; print('Webhook 已更新：'+url+'/webhook' if url else '找不到 URL')"

echo.
echo  =============================================
echo    比董已啟動！可以傳訊息測試了 📱
echo  =============================================
pause
