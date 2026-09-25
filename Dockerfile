# Web front end only: reads Postgres, runs no models. The batch pipeline has its own
# (heavier) requirements in requirements.txt.
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY app.py web_data.py database.py api.py openapi.py charts.py ./
COPY templates/ ./templates/
COPY static/ ./static/

RUN useradd --create-home --uid 10001 web
USER web

EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-", "app:app"]
