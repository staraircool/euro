# Lightweight Python actor - no browser needed
FROM apify/actor-python:3.12

USER myuser

# Copy requirements and install
COPY --chown=myuser:myuser requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

# Copy source code
COPY --chown=myuser:myuser . ./

# Compile to verify
RUN python -m compileall -q my_actor/

# Run the actor
CMD ["python", "-m", "my_actor"]
