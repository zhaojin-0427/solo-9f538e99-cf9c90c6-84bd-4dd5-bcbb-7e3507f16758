FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRIAL_DB_PATH=/data/trials.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sample ./sample

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 生产用 gunicorn；SQLite 已开 WAL 与 busy_timeout，2 worker 可安全共写
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "app.app:create_app()"]
