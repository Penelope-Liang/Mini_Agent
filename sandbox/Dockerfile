FROM python:3.12-slim
COPY worker.py /opt/miniagent-worker.py
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "-B", "-c", "import time; time.sleep(86400)"]
