"""Shared version gate for synchronous workflow adapters."""
from elbow_helper.infrastructure.persistence.versioned import VersionedSQLiteDatabase
from .schema import MIGRATIONS, SCHEMA_VERSION, create_schema

DATABASE = VersionedSQLiteDatabase("workflows", create_schema, None,
    migrations=MIGRATIONS, target_version=SCHEMA_VERSION)
