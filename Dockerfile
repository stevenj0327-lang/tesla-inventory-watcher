FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=pwuser:pwuser tracker ./tracker
COPY entrypoint.sh /usr/local/bin/tesla-watcher
RUN chmod 755 /usr/local/bin/tesla-watcher && mkdir /data && chown pwuser:pwuser /data
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data POLL_SECONDS=120
ENTRYPOINT ["/usr/local/bin/tesla-watcher"]
