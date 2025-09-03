FROM python:3.11-slim

# (opcionalno) OS timezone
ENV TZ=Europe/Zagreb

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# (opcionalno, ali korisno za neke wheelove)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8050
# Gunicorn služi Dash (Flask) server objekt
CMD ["gunicorn","app:server","-b","0.0.0.0:8050","-w","2","--timeout","120"]
