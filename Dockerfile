FROM python:3.11
WORKDIR /app
COPY ./req.txt /app/req.txt
RUN pip install --upgrade pip setuptools
RUN pip install --trusted-host --no-cache-dir -r req.txt
COPY . /app
EXPOSE 8080
CMD ["opentelemetry-instrument", "gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080", "--timeout", "300", "index:app"]
