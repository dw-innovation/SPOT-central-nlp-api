FROM python:3.12-slim-bookworm

# ENV http_proxy=ADD_PROXY
# ENV https_proxy=ADD_PROXY

# No apt layer: the official Python image already ships pip, and nothing
# else here needs a system package.
RUN pip install --no-cache-dir --upgrade pip

WORKDIR /app

COPY app /app
COPY data /app/data

COPY requirements.txt /app

RUN pip install --no-cache-dir -r /app/requirements.txt

EXPOSE 8080

CMD exec uvicorn main:app --reload --port 8080 --host 0.0.0.0