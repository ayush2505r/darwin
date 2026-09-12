"""Database connectivity module for Darwin Evolving Multi-Agent Teaching Assistant.

Mandatory constraint:
All connections must be created using mysql-connector-python with use_pure=True.
"""

import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import mysql.connector
from mysql.connector import errorcode
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def get_db_config(include_database: bool = True) -> Dict[str, Any]:
    """Return database configuration dictionary loaded from environment."""
    config: Dict[str, Any] = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", 3306)),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "use_pure": True,  # Mandatory constraint
    }
    if include_database:
        config["database"] = os.getenv("MYSQL_DATABASE", "darwin_db")
    return config


def get_connection(include_database: bool = True) -> mysql.connector.MySQLConnection:
    """Connection-factory function that always passes use_pure=True to mysql.connector."""
    config = get_db_config(include_database=include_database)
    return mysql.connector.connect(**config)


@contextmanager
def get_db_cursor(commit: bool = False, dictionary: bool = True):
    """Context manager providing a managed MySQL connection and cursor.
    
    Ensures both cursor and connection are properly closed.
    Rolls back automatically on exception if commit was requested.
    """
    conn = get_connection(include_database=True)
    cursor = conn.cursor(dictionary=dictionary)
    try:
        yield cursor, conn
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def init_db(schema_path: Optional[str] = None) -> None:
    """Initialize database and tables defined in schema.sql."""
    db_name = os.getenv("MYSQL_DATABASE", "darwin_db")
    
    # First ensure the database exists
    conn = get_connection(include_database=False)
    cursor = conn.cursor()
    try:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    finally:
        cursor.close()
        conn.close()

    # Now execute statements from schema.sql
    if schema_path is None:
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")

    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"Schema file not found at {schema_path}")

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    # Split by statements
    statements = [stmt.strip() for stmt in schema_sql.split(";") if stmt.strip()]

    with get_db_cursor(commit=True, dictionary=False) as (cursor, conn):
        for stmt in statements:
            # Skip comments or empty lines
            cleaned_stmt = "\n".join(
                line for line in stmt.splitlines() if not line.strip().startswith("--")
            ).strip()
            if cleaned_stmt:
                cursor.execute(cleaned_stmt)


def execute_query(query: str, params: Optional[Tuple[Any, ...]] = None, commit: bool = False) -> int:
    """Execute an INSERT, UPDATE, or DELETE query and return affected rows."""
    with get_db_cursor(commit=commit) as (cursor, conn):
        cursor.execute(query, params or ())
        return cursor.rowcount


def execute_insert(query: str, params: Optional[Tuple[Any, ...]] = None) -> int:
    """Execute an INSERT query and return the last inserted ID."""
    with get_db_cursor(commit=True) as (cursor, conn):
        cursor.execute(query, params or ())
        return cursor.lastrowid


def fetch_all(query: str, params: Optional[Tuple[Any, ...]] = None) -> List[Dict[str, Any]]:
    """Execute a SELECT query and return all rows as a list of dicts."""
    with get_db_cursor(commit=False, dictionary=True) as (cursor, conn):
        cursor.execute(query, params or ())
        return cursor.fetchall()


def fetch_one(query: str, params: Optional[Tuple[Any, ...]] = None) -> Optional[Dict[str, Any]]:
    """Execute a SELECT query and return the first row as a dict."""
    with get_db_cursor(commit=False, dictionary=True) as (cursor, conn):
        cursor.execute(query, params or ())
        return cursor.fetchone()
