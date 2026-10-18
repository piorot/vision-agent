FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# http-ece (a pywebpush dependency) only ships an old-style sdist that newer setuptools cannot build.
RUN pip install --no-cache-dir "setuptools<80" wheel \
 && pip install --no-cache-dir --no-build-isolation http-ece \
 && pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
