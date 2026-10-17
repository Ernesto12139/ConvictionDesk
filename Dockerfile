FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MIA_RUNTIME_MODE=PUBLIC_DEMO MIA_RESEARCH_EXECUTION=OFFLINE_REPLAY \
    MIA_DEMO_DIR=/var/lib/mia-demo PORT=8080
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home mia \
    && mkdir -p /var/lib/mia-demo \
    && chown mia:mia /var/lib/mia-demo
USER 10001:10001
ARG MIA_BUILD_REVISION=development
LABEL org.opencontainers.image.revision=$MIA_BUILD_REVISION
ENV MIA_BUILD_REVISION=$MIA_BUILD_REVISION
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=2).read()"
CMD ["python", "-m", "market_agent.serve"]
