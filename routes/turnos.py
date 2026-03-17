from fastapi import APIRouter, HTTPException
from datetime import datetime
from database import get_connection
from models import TurnoRespuesta

router = APIRouter()

def generar_numero_turno(conn) -> str:
    hoy = datetime.now().strftime("%Y-%m-%d")
    cursor = conn.execute(
        "SELECT COUNT(*) FROM turnos WHERE DATE(hora_creacion) = ?", (hoy,)
    )
    count = cursor.fetchone()[0]
    return f"A{(count + 1):03d}"

@router.post("/turno", response_model=TurnoRespuesta)
def solicitar_turno():
    conn = get_connection()
    numero = generar_numero_turno(conn)
    hora_creacion = datetime.now().isoformat()

    cursor = conn.execute(
        "INSERT INTO turnos (numero, hora_creacion, estado) VALUES (?, ?, ?)",
        (numero, hora_creacion, "EN_COLA")
    )
    turno_id = cursor.lastrowid  # captura el id generado
    conn.commit()

    cursor = conn.execute(
        "SELECT COUNT(*) FROM turnos WHERE estado = 'EN_COLA'"
    )
    personas_delante = max(0, cursor.fetchone()[0] - 1)
    conn.close()

    return TurnoRespuesta(
        id=turno_id,
        numero=numero,
        personas_delante=personas_delante,
        tiempo_estimado=personas_delante * 4
    )

@router.patch("/turno/{turno_id}/cancelar")
def cancelar_turno(turno_id: int):
    conn = get_connection()

    # verifica que el turno exista y esté en un estado cancelable
    cursor = conn.execute(
        "SELECT estado FROM turnos WHERE id = ?", (turno_id,)
    )
    turno = cursor.fetchone()

    if not turno:
        conn.close()
        raise HTTPException(status_code=404, detail="Turno no encontrado")

    if turno["estado"] not in ("EN_COLA", "LLAMADO"):
        conn.close()
        raise HTTPException(status_code=400, detail="El turno no se puede cancelar")

    conn.execute(
        "UPDATE turnos SET estado = 'AUSENTE' WHERE id = ?", (turno_id,)
    )
    conn.commit()
    conn.close()

    return {"ok": True}