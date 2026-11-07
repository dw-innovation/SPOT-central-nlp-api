FROM python:3.9.12-slim

# ENV http_proxy=ADD_PROXY
# ENV https_proxy=ADD_PROXY

# No apt layer: bullseye is end of life, so its security pocket still
# publishes an index but no longer serves the packages, and installing
# python3-pip 404s on the python3.9 it depends on. The official Python image
# already ships pip, so nothing here needed apt in the first place.
RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app

COPY app /app
COPY data /app/data

COPY requirements.txt /app

RUN pip install --no-cache-dir -r /app/requirements.txt

EXPOSE 8080

CMD exec uvicorn main:app --reload --port 8080 --host 0.0.0.0