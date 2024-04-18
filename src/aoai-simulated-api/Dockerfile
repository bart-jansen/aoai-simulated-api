FROM python:3.12.1-slim-bullseye

WORKDIR /app

# Install ARM64 dependencies
RUN apt-get update && apt-get install --no-install-recommends -y gcc python3-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

RUN pip install .

CMD [ "gunicorn", "aoai_simulated_api.main:app", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]