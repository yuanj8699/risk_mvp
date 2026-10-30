FROM python:3.12.12-slim

# 分析依赖构建时安装；任务运行无网络、无运行时pip安装。
COPY requirements.txt /opt/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/requirements.txt
COPY risk_client.py /opt/risk_client.py
ENV PYTHONPATH=/opt PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /workspace
USER 65534:65534
CMD ["python", "--version"]
