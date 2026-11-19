import sqlite3
from pathlib import Path
from datetime import datetime

RUTA_DB = r"C:\GeoIAZ\historial_ia.sqlite"

def _conectar():
    conn = sqlite3.connect(RUTA_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            extension TEXT,
            tamano_bytes INTEGER,
            entropia REAL,
            codec_elegido TEXT,
            ratio_obtenido REAL
        )
    """)
    return conn

def registrar_operacion(extension, tamano, entropia, codec_elegido, ratio):
    """Guarda una operación en el historial para futuro re-entrenamiento."""
    try:
        conn = _conectar()
        conn.execute("""
            INSERT INTO operaciones 
            (fecha, extension, tamano_bytes, entropia, codec_elegido, ratio_obtenido)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            extension, tamano, entropia, codec_elegido, ratio
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass # Si falla, no rompemos la compresión principal

def contar_operaciones():
    """Devuelve cuántas operaciones se registraron hasta ahora."""
    try:
        conn = _conectar()
        cursor = conn.execute("SELECT COUNT(*) FROM operaciones")
        total = cursor.fetchone()[0]
        conn.close()
        return total
    except Exception:
        return 0