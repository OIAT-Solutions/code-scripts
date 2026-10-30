# Code-only rebuild for remote (SSH) deploys on oiat-srv-01.
# Docker Desktop's login helper is unavailable over SSH, so the full build (FROM python:3.11-slim) cannot run
# there. This builds on the currently running image instead: the app code is replaced (old files removed, so
# deleted modules don't linger), requirements are re-checked (no-op unless requirements.txt changed) and
# static files are collected. A change to Playwright/system packages still needs a full build at the server.
FROM oiat-portal:latest
RUN find /app -mindepth 1 -delete
COPY . /app
RUN pip install -r /app/requirements.txt \
    && chmod +x /app/docker/entrypoint.sh \
    && DJANGO_DEBUG=0 DJANGO_ALLOWED_HOSTS="*" python manage.py collectstatic --noinput
