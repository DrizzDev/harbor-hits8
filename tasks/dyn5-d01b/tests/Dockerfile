# The separate verifier: a fresh container with no network, never touched by the agent. It reads only the
# device state Harbor collected from the phone container (/state/capture).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY . /tests
WORKDIR /tests
