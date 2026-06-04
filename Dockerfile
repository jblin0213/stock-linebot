FROM python:3.12-slim

WORKDIR /app

# 先複製需求檔，利用快取層
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製所有程式
COPY . .

# 啟動 LINE bot（UTF-8 模式）
CMD ["python", "-X", "utf8", "cloud_bot.py"]
