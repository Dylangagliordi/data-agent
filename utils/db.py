"""
Postgres connection helpers for the SQL analyst graph's runtime.

Only the app_reader (read-only) role is used here — never the admin/superuser
connection, which is reserved exclusively for utils/load_data.py.
"""

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.hermes/profiles/data-agent/.env"))


def get_app_reader_connection():
    """Connect as the read-only app_reader role — used by all graph nodes."""
    return psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=os.environ["PG_PORT"],
        dbname=os.environ["PG_DATABASE"],
        user=os.environ["PG_APP_READER_USER"],
        password=os.environ["PG_APP_READER_PASSWORD"],
    )
