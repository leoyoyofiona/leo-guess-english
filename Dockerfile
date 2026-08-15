# LEO · 猜英语 — Render Docker 部署（备选方案）
# 若 render.yaml 自动检测失败，可用 Docker 部署：
#   1) 上传本目录到 GitHub 仓库
#   2) Render → New Web Service → 选仓库 → Runtime 选 Docker
FROM python:3.11-slim
WORKDIR /app
COPY . .
EXPOSE 10000
ENV PORT=10000
CMD ["python3", "server.py"]
