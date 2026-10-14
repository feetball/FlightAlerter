FROM python:3.13.16-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /code
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run unprivileged. docker-compose.yml runs it as the owner of the data folder (deploy.sh sets that up).
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin flightalerter
USER flightalerter

EXPOSE 8095
CMD ["python", "app.py"]
