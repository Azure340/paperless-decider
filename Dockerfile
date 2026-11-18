FROM python:3.13-alpine

# No pip install: app/decider.py is standard library only.
RUN adduser -D -u 10001 decider

WORKDIR /app
COPY app/decider.py /app/decider.py

USER decider
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python3 -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8090/healthz",timeout=3)' || exit 1

CMD ["python3", "/app/decider.py"]
