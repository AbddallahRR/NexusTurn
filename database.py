import sqlite3

DB_PATH = "turnos.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # permite acceder a columnas por nombre
    return conn

def inicializar_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS turnos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            numero          TEXT NOT NULL,
            hora_creacion   TEXT NOT NULL,
            estado          TEXT NOT NULL DEFAULT 'EN_COLA',
            ventanilla      INTEGER,
            hora_llamado    TEXT,
            hora_finalizado TEXT
        )
    """)
    conn.commit()
    conn.close()