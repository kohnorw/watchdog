FROM python:3.12-slim
WORKDIR /app
RUN mkdir -p /data /app/templates
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG CACHEBUST=1
COPY app.py .
COPY templates/index.html templates/
EXPOSE 5000
CMD ["python", "-u", "app.py"]
