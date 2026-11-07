FROM python:3.9.12-slim

# ENV http_proxy=ADD_PROXY
# ENV https_proxy=ADD_PROXY

# No apt-get upgrade: bullseye is end of life, so its security pocket still
# publishes an index but no longer serves the packages, and the upgrade fails
# with a 404 on dpkg. Nothing here needs it — the image is rebuilt from the
# base on every change.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

WORKDIR /app

COPY app /app
COPY data /app/data

COPY requirements.txt /app

RUN pip install -r /app/requirements.txt

EXPOSE 8080

CMD exec uvicorn main:app --reload --port 8080 --host 0.0.0.0