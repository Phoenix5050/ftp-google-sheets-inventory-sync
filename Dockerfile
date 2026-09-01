# Use a Python 3.11 base image, optimized for size
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Update packages and install SSL/TLS prerequisites (ca-certificates)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file and install dependencies first (for faster rebuilds)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY app.py .

# Expose the port (Cloud Run sets the PORT env variable)
ENV PORT 8080 

# Command to run the application using Gunicorn (a production web server)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
