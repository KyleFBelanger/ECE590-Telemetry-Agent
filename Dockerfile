FROM arm64v8/python:3.11-slim

WORKDIR /workspace

ENV PYTHONUNBUFFERED=1

# Upgrade pip first
RUN pip install --no-cache-dir --upgrade pip

# Install PyTorch CPU wheels from the PyTorch index only.
# Keep this separate so normal packages are pulled from normal PyPI.
RUN pip install --no-cache-dir \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# Install regular project dependencies from PyPI
COPY requirements.txt /workspace/requirements.txt
RUN pip install --no-cache-dir -r /workspace/requirements.txt

# Copy project files into the image.
# Your docker-compose volumes will still override mounted folders during development.
COPY . /workspace

# Streamlit default port
EXPOSE 8501

CMD ["bash"]
