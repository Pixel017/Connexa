FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connexa ./connexa
COPY run.py .

# SQLite lives on a mounted volume in production
ENV CONNEXA_DB=/data/connexa.db \
    CONNEXA_HOST=0.0.0.0
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["python", "run.py"]
