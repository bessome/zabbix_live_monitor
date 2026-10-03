FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --home-dir /app --no-create-home monitor \
    && mkdir /data \
    && chown monitor:monitor /data

COPY main.py app_config.py app_storage.py app_web.py zabbix_service.py \
     routes_account.py routes_devices.py routes_settings.py \
     ping_monitor.py snmp_monitor.py ./
COPY templates ./templates
COPY static ./static

USER monitor
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
