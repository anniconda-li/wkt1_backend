FROM python:3.12-slim

LABEL org.opencontainers.image.title="wkt-intercom-server"
LABEL org.opencontainers.image.description="WebSocket intercom relay for WTK1 devices"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY main.py ./
COPY server ./server

USER app

EXPOSE 18081

CMD ["python", "main.py"]
