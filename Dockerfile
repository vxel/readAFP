# Run readAFP privately on your own machine or internal network.
#   docker build -t readafp .
#   docker run -p 8770:8770 readafp
# then open http://localhost:8770 — your AFP files never leave the container.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
ENV PYTHONPATH=/app/src
EXPOSE 8770

CMD ["gunicorn", "readafp.app:create_app()", "--bind", "0.0.0.0:8770", "--workers", "2", "--timeout", "120"]
