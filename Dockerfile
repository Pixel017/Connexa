# Connexa — the whole platform in one container
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONNEXA_HOST=0.0.0.0 \
    CONNEXA_PORT=8000 \
    CONNEXA_DB=/data/connexa.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connexa ./connexa
COPY run.py ./

# SQLite lives on a volume so data survives container replacement
RUN mkdir -p /data
VOLUME /data
EXPOSE 8000

CMD ["python", "run.py"]
