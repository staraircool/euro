# Playwright browser + lightweight Python
FROM apify/actor-python:3.12

USER myuser

COPY --chown=myuser:myuser requirements.txt ./

RUN echo "Installing dependencies:" \
 && pip install -r requirements.txt \
 && echo "All packages:" \
 && pip freeze

# Install Chromium browser for Playwright
RUN python -m playwright install chromium --with-deps 2>/dev/null || \
    python -m playwright install chromium || true

COPY --chown=myuser:myuser . ./

RUN python -m compileall -q my_actor/

CMD ["python", "-m", "my_actor"]
