FROM python:3.11-slim

WORKDIR /opt/app

# Копируем зависимости и ставим их.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код и конфиг.
COPY src/ ./src/
COPY config/ ./config/

# Переменные задаются через env_file (pinger.env) в docker-compose.
ENTRYPOINT ["python", "src/pinger.py"]
