# DONZO backend — public deploy Dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libjpeg62-turbo-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DJANGO_SETTINGS_MODULE=config.settings

EXPOSE 8000

# cloud_launcher daphne + bot + user_client ni boshqaradi va migratsiyani
# fon thread'da (direct Neon ulanish bilan) ishga tushiradi — daphne
# darhol /health/ ga javob beradi.
CMD ["python", "cloud_launcher.py"]
