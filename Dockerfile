FROM python:3.11-slim-bookworm

ENV APP_DIR=/opt/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR ${APP_DIR}

COPY code/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY code/ ${APP_DIR}

# Run as an unprivileged user (pairs with the chart's restricted securityContext).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser ${APP_DIR}
USER 10001

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
