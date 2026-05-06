# Playwright browser + lightweight Python
FROM apify/actor-python:3.12

# Install system deps for Chromium as root BEFORE switching to myuser
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

USER myuser

COPY --chown=myuser:myuser requirements.txt ./

RUN pip install -r requirements.txt \
 && python -m playwright install chromium

COPY --chown=myuser:myuser . ./

RUN python -m compileall -q my_actor/

CMD ["python", "-m", "my_actor"]
