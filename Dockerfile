FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY slack_mirror.py .
RUN useradd --system --create-home mirror && mkdir /data && chown mirror:mirror /data

USER mirror
ENV SLACK_MIRROR_DB=/data/slack-mirror.sqlite3

ENTRYPOINT ["python", "slack_mirror.py"]
CMD ["watch"]
