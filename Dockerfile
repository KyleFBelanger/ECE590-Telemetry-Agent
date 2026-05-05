FROM arm64v8/python:3.11-slim

WORKDIR /workspace

RUN pip install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu \
    torchvision \
    numpy \
    docker \
    psutil

COPY training/ ./training/
COPY telemetry/ ./telemetry/

CMD ["python3"]
