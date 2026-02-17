# Pincer - Your personal AI agent
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
RUN pip install .

COPY . .

CMD ["pincer"]
