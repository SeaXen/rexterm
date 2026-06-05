FROM python:3.11-slim

COPY vendor-rootfs-tmux/ /

WORKDIR /app
COPY app /app/app
COPY static /app/static
EXPOSE 8080
CMD ["python", "/app/app/server.py"]
