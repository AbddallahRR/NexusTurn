from fastapi import APIRouter, HTTPException
from datetime import datetime
from database import get_connection
from models import TurnoRespuesta
from fastapi import WebSocket, WebSocketDisconnect
import json

router = APIRouter()

def generar_numero_turno(conn) -> str:
    hoy = datetime.now().strftime("%Y-%m-%d")
    cursor = conn.execute(
        "SELECT COUNT(*) FROM turnos WHERE DATE(hora_creacion) = ?", (hoy,)
    )
    count = cursor.fetchone()[0]
    return f"A{(count + 1):03d}"

@router.post("/turno", response_model=TurnoRespuesta)
async def solicitar_turno():
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

    await notificar_monitores()

    return TurnoRespuesta(
        id=turno_id,
        numero=numero,
        personas_delante=personas_delante,
        tiempo_estimado=personas_delante * 4
    )

@router.patch("/turno/{turno_id}/cancelar")
async def cancelar_turno(turno_id: int):
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

    await notificar_monitores()

    return {"ok": True}

@router.get("/monitor")
def estado_monitor():
    conn = get_connection()

    # Turnos siendo atendidos por ventanilla
    cursor = conn.execute("""
        SELECT ventanilla, numero
        FROM turnos
        WHERE estado = 'ATENDIENDO'
        ORDER BY ventanilla
    """)
    atendiendo = {row["ventanilla"]: row["numero"] for row in cursor.fetchall()}

    # Arma la lista de ventanillas (ajusta el rango según cuántas tengas)
    ventanillas = []
    for numero in range(1, 4):
        ventanillas.append({
            "numero": numero,
            "turno_actual": atendiendo.get(numero)
        })

    # Próximos 5 en cola
    cursor = conn.execute("""
        SELECT numero FROM turnos
        WHERE estado = 'EN_COLA'
        ORDER BY id ASC
        LIMIT 5
    """)
    cola_siguiente = [row["numero"] for row in cursor.fetchall()]
    conn.close()

    return {
        "ventanillas": ventanillas,
        "cola_siguiente": cola_siguiente
    }    

monitores_conectados: list[WebSocket] = []

async def notificar_monitores():
    """Llama a esto cada vez que cambie algo en la cola."""
    if not monitores_conectados:
        return
    datos = obtener_estado_monitor()  # la misma olgica de /api/monitor
    mensaje = json.dumps(datos)
    for ws in monitores_conectados.copy():
        try:
            await ws.send_text(mensaje)
        except Exception:
            monitores_conectados.remove(ws)

async def notificar_todos():
    await notificar_monitores()
    await notificar_usuarios()

def obtener_estado_monitor():
    conn = get_connection()
    cursor = conn.execute("""
        SELECT ventanilla, numero FROM turnos
        WHERE estado = 'ATENDIENDO'
        ORDER BY ventanilla
    """)
    atendiendo = {row["ventanilla"]: row["numero"] for row in cursor.fetchall()}

    ventanillas = [
        {"numero": n, "turno_actual": atendiendo.get(n)}
        for n in range(1, 4)
    ]

    cursor = conn.execute("""
        SELECT numero FROM turnos
        WHERE estado = 'EN_COLA'
        ORDER BY id ASC LIMIT 5
    """)
    cola = [row["numero"] for row in cursor.fetchall()]
    conn.close()
    return {"ventanillas": ventanillas, "cola_siguiente": cola}

@router.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
    await websocket.accept()
    monitores_conectados.append(websocket)
    try:
        # Al conectarse, manda el estado actual inmediatamente
        datos = obtener_estado_monitor()
        await websocket.send_text(json.dumps(datos))
        # Mantiene la conexión abierta
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        monitores_conectados.remove(websocket)

# Diccionario: turno_id → websocket del celular
usuarios_conectados: dict[int, WebSocket] = {}

@router.websocket("/ws/turno/{turno_id}")
async def websocket_usuario(websocket: WebSocket, turno_id: int):
    await websocket.accept()
    usuarios_conectados[turno_id] = websocket
    try:
        # Manda el estado actual al conectarse
        await push_estado_usuario(turno_id, websocket)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        usuarios_conectados.pop(turno_id, None)

async def push_estado_usuario(turno_id: int, websocket: WebSocket):
    conn = get_connection()

    # Busca el turno
    cursor = conn.execute(
        "SELECT numero, estado, ventanilla FROM turnos WHERE id = ?", (turno_id,)
    )
    turno = cursor.fetchone()
    if not turno:
        conn.close()
        return

    # Calcula posición real en la cola en este momento
    cursor = conn.execute("""
        SELECT COUNT(*) FROM turnos
        WHERE estado = 'EN_COLA' AND id < ?
    """, (turno_id,))
    posicion = cursor.fetchone()[0]
    conn.close()

    await websocket.send_text(json.dumps({
        "estado":          turno["estado"],
        "posicion":        posicion,
        "ventanilla":      turno["ventanilla"],
        "tiempo_estimado": posicion * 4
    }))

async def notificar_usuarios():
    """Llama a esto cada vez que la cola cambie."""
    for turno_id, ws in list(usuarios_conectados.items()):
        try:
            await push_estado_usuario(turno_id, ws)
        except Exception:
            usuarios_conectados.pop(turno_id, None)        