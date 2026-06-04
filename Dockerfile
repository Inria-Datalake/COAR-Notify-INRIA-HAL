FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Install runtime dependencies from pyproject (cached unless metadata changes)
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# Copy the wait-for-it script with executable permissions to PATH
COPY --chmod=0755 wait-for-it.sh /usr/local/bin/wait-for-it

# Copy the rest of the application
COPY . .

EXPOSE 5000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--timeout", "60", "app.app:app"]
