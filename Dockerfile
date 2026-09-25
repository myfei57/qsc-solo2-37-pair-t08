FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY uhtline ./uhtline
COPY main.py ./

ENV UHTLINE_HOST=0.0.0.0 \
    UHTLINE_PORT=8080 \
    UHTLINE_DATA=/data

VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "main.py", "--data-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "8080"]
