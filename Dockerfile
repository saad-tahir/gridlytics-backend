# Gridlytics, the hosted API (plan Part 2.7 and 10.1, DEP-06; round 7, audit parity-architecture-02): the API in one container, with the data folder on a
# volume, in the shape of Solarlytics' stage S0b (solarlytics-backend at c853130, Dockerfile). The image holds the API only: the web app is the
# gridlytics-frontend repository's, hosted on Vercel (docs/DEPLOY.md, The web app on Vercel), so no page is built or served here. TLS, the domain, backups
# off the machine and a staging copy are set up around it: see docs/DEPLOY.md.
#
#   docker build -t gridlytics:0.1.0 .
#   docker run -d --name gridlytics -p 127.0.0.1:8061:8061 -v gridlytics-data:/data --env-file .env.production gridlytics:0.1.0
#   (or: docker compose -f deploy/docker-compose.yml up -d)
#
# No sign-in mode is baked in: the server's environment sets it (.env.production.example). One server process per data folder: uvicorn runs one worker,
# with the 15-minute scheduler in it (GRIDLYTICS_SCHEDULER=1), and no two containers share the volume. No new dependency: the Python packages are
# requirements.txt's pins. The base image is pinned to a release; pin it to its digest too once the owner's registry is reached (docs/DEPLOY.md).

FROM python:3.12.16-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    GRIDLYTICS_DATA_DIR=/data GRIDLYTICS_DB=/data/gridlytics.db GRIDLYTICS_DAILY_BACKUP=1 GRIDLYTICS_SCHEDULER=1 GRIDLYTICS_PORT=8061
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/gridlytics ./backend/gridlytics
# A company's backup put back, run with the API stopped (docs/DEPLOY.md, Backups): docker run --rm -it -v gridlytics-data:/data gridlytics:<version>
# python /app/scripts/restore_backup.py
COPY scripts/restore_backup.py ./scripts/restore_backup.py
# The app runs as a user of its own and writes only to /data: platform.db, platform.secret (only without GRIDLYTICS_LOCAL_JWT_SECRET), companies/ (each
# company's workspace and telemetry files, and their copies in companies/backup/), keys/ (the sites' signing keys), reports/, backup/ and logs/
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin gridlytics && mkdir -p /data && chown gridlytics /data
USER gridlytics
VOLUME ["/data"]
EXPOSE 8061
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ['GRIDLYTICS_PORT'], timeout=4).status == 200 else 1)"
WORKDIR /app/backend
# --proxy-headers: behind the TLS proxy the client's address (for the sign-in lockout and the limits per address) is the proxy's X-Forwarded-For, believed
# only from FORWARDED_ALLOW_IPS (uvicorn reads it), with GRIDLYTICS_TRUST_PROXY=1. --no-server-header: no "server: uvicorn" on the answers.
CMD ["sh", "-c", "exec uvicorn gridlytics.api:app --host 0.0.0.0 --port \"$GRIDLYTICS_PORT\" --proxy-headers --no-server-header"]
