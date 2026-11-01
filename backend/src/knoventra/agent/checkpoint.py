from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = "knv_checkpoint"


def make_checkpointer(sqlalchemy_url: str):
    """PostgresSaver on the same instance as the business tables, isolated in its own schema."""
    url = sqlalchemy_url.replace("+psycopg", "")
    with Connection.connect(url, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    pool = ConnectionPool(url, max_size=8, open=True,
                          kwargs={"autocommit": True, "row_factory": dict_row, "options": f"-csearch_path={SCHEMA}"})
    saver = PostgresSaver(pool)
    saver.setup()
    return saver, pool
