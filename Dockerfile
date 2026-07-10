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

# Drop root: run as an unprivileged user so a parser bug can't act as root
# inside the container. The app writes nothing to disk (the Pyodide zip is
# built in memory), so no writable dirs are needed; port 8770 is > 1024 so a
# non-root process can bind it.
RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["gunicorn", "readafp.app:create_app()", "--bind", "0.0.0.0:8770", "--workers", "2", "--timeout", "120"]
