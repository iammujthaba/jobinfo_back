import os
from sqlalchemy import create_engine, MetaData, text
from dotenv import load_dotenv

# Load database URL from your .env file
load_dotenv()

# Set up connections
sqlite_url = "sqlite:///./jobinfo.db"
postgres_url = os.getenv("DATABASE_URL")

print("Connecting to databases...")
sqlite_engine = create_engine(sqlite_url)
pg_engine = create_engine(postgres_url)

# Reflect the PostgreSQL schema to get the correct table order (respects foreign keys)
pg_meta = MetaData()
pg_meta.reflect(bind=pg_engine)

with sqlite_engine.connect() as sqlite_conn:
    with pg_engine.begin() as pg_conn:
        for table in pg_meta.sorted_tables:
            # Skip the alembic migration table as it's already up to date
            if table.name == "alembic_version":
                continue
                
            print(f"Transferring table: {table.name}...")
            
            # Fetch all data from the SQLite table
            rows = sqlite_conn.execute(table.select()).mappings().fetchall()
            
            if rows:
                # Insert data into PostgreSQL
                pg_conn.execute(table.insert(), [dict(row) for row in rows])
                print(f" -> Migrated {len(rows)} rows.")
                
                # Fix the PostgreSQL auto-increment sequences
                if 'id' in table.columns:
                    seq_query = text(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table.name};")
                    pg_conn.execute(seq_query)
            else:
                print(" -> Table is empty. Skipping.")

# ==========================================
# POST-MIGRATION VERIFICATION
# ==========================================
print("\nVerifying row counts...")
with sqlite_engine.connect() as sqlite_conn, pg_engine.connect() as pg_conn:
    for table in pg_meta.sorted_tables:
        if table.name == "alembic_version":
            continue
            
        sqlite_count = sqlite_conn.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar()
        pg_count = pg_conn.execute(text(f"SELECT COUNT(*) FROM {table.name}")).scalar()
        
        status = "✅" if sqlite_count == pg_count else "❌ MISMATCH"
        print(f" {status} {table.name}: SQLite={sqlite_count}, Postgres={pg_count}")

print("\nData migration and verification complete!")