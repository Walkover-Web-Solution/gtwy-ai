FROM python:3.10
WORKDIR /app
COPY ./req.txt /app/req.txt
RUN pip install --upgrade pip
RUN pip install --trusted-host --no-cache-dir -r req.txt
COPY . /app
EXPOSE 8080
CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080", "--timeout", "300", "index:app"]
