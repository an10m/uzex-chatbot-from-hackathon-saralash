FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/uzex

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY uzex_spot_data ./uzex_spot_data
COPY .env.example ./.env.example

# Build the database at image build time when the data directory is present.
RUN python app/build_db.py --data-dir uzex_spot_data --out /opt/uzex/data/uzex.sqlite

CMD ["python", "-m", "app.bot"]
