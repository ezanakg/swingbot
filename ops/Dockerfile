FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=America/New_York
RUN apt-get update && apt-get install -y --no-install-recommends tzdata cron && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/swingbot
COPY pyproject.toml requirements.txt README.md ./
COPY swingbot ./swingbot
COPY config ./config
COPY ops/crontab.example /etc/cron.d/swingbot
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir . \
    && useradd -r -m -d /home/swingbot swingbot && mkdir -p var && chown -R swingbot:swingbot /opt/swingbot \
    && sed -i 's#/opt/swingbot/.venv/bin/swingbot#/usr/local/bin/swingbot#' /etc/cron.d/swingbot \
    && chmod 0644 /etc/cron.d/swingbot

VOLUME ["/opt/swingbot/var"]
# default: run the scheduler (cron in the foreground). Override the command to run a single mode, e.g. `swingbot scan`.
CMD ["cron", "-f"]
