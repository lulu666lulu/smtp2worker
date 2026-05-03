FROM python:3.12-slim

WORKDIR /app
COPY smtp2worker.py /app/smtp2worker.py

ENV SMTP_LISTEN_HOST=0.0.0.0
ENV SMTP_LISTEN_PORT=2525

EXPOSE 2525
CMD ["python", "/app/smtp2worker.py"]
