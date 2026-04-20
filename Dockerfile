FROM python:3.11-slim
WORKDIR /app

# LibreOffice headless: 支持老 Office 二进制格式 (.doc/.ppt/.xls → .docx/.pptx/.xlsx)
# 只装需要的模块 + 中文字体, 避免全套 libreoffice (~500MB 额外)
# 放在 pip 之前是因为这层重且稳定, 代码改动不会导致这层重 build
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress libreoffice-calc \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000", "--timeout", "180", "--graceful-timeout", "30"]
