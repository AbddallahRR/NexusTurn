from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from datetime import datetime
import httpx
import json

from database import get_connection

router = APIRouter()

OLLAMA_URL = "http://localhost:11434/api/generate"
MODELO     = "qwen:0.5b"

SYSTEM_PROMPT = """Eres un asistente de atención al cliente para un sistema de turnos.
Responde ÚNICAMENTE sobre información del sistema: cantidad de turnos en cola sin detalles más específicos, tiempo de espera para el usuario que lo solicita,
posicione en la cola de usuarios en que se encuentra el usuario que lo solicita. Sé breve, amable y directo.
Si eres cuestionaodo sobre información que vaya fuera del funcionamiento de este sistema de turnos o información que corresponda al funcionamiento general del sistema o 
información que pueda comprometer la privacidad del sistema o de los demás usuarios, 
indica amablemente que solo puedes ayudar con información del sistema de turnos."""


class MensajeChat(BaseModel):
    mensaje: str


def construir_contexto() -> str:
    """Consulta la DB directamente y construye un resumen completo para el LLM."""
    conn = get_connection()

    # ── Ventanillas ────────────────────────────────────────────────────────────
    cursor = conn.execute("""
        SELECT ventanilla, numero, hora_llamado
        FROM turnos
        WHERE estado IN ('LLAMADO', 'ATENDIENDO')
        ORDER BY ventanilla
    """)
    atendiendo = {row["ventanilla"]: (row["numero"], row["hora_llamado"])
                  for row in cursor.fetchall()}

    ventanillas_lineas = []
    for n in range(1, 4):
        if n in atendiendo:
            numero, hora = atendiendo[n]
            ventanillas_lineas.append(f"  - Ventanilla {n}: atendiendo turno {numero} (desde {hora or 'hace un momento'})")
        else:
            ventanillas_lineas.append(f"  - Ventanilla {n}: libre")

    # ── Cola completa ──────────────────────────────────────────────────────────
    cursor = conn.execute("""
        SELECT id, numero, hora_creacion
        FROM turnos
        WHERE estado = 'EN_COLA'
        ORDER BY id ASC
    """)
    cola = cursor.fetchall()

    ahora = datetime.now()
    cola_lineas = []
    for pos, row in enumerate(cola, start=1):
        try:
            creado = datetime.fromisoformat(row["hora_creacion"])
            espera_real = int((ahora - creado).total_seconds() / 60)
            espera_txt = f"esperando {espera_real} min"
        except Exception:
            espera_txt = "tiempo desconocido"
        cola_lineas.append(
            f"  {pos}. Turno {row['numero']} (id={row['id']}, {espera_txt})"
        )

    total_cola = len(cola)
    espera_estimada = total_cola * 4   # ~4 min por turno

    # ── Estadísticas del día ───────────────────────────────────────────────────
    hoy = ahora.strftime("%Y-%m-%d")
    cursor = conn.execute("""
        SELECT estado, COUNT(*) as cnt
        FROM turnos
        WHERE DATE(hora_creacion) = ?
        GROUP BY estado
    """, (hoy,))
    stats = {row["estado"]: row["cnt"] for row in cursor.fetchall()}
    conn.close()

    atendidos_hoy  = stats.get("FINALIZADO", 0)
    ausentes_hoy   = stats.get("AUSENTE", 0)
    cancelados_hoy = stats.get("CANCELADO", 0)

    # ── Armar texto ────────────────────────────────────────────────────────────
    cola_txt = "\n".join(cola_lineas) if cola_lineas else "  (ninguno en espera)"

    return (
        f"=== ESTADO ACTUAL DEL SISTEMA ({ahora.strftime('%H:%M:%S')}) ===\n"
        f"Ventanillas (3 en total):\n" + "\n".join(ventanillas_lineas) + "\n\n"
        f"Cola actual ({total_cola} turno(s) esperando, ~{espera_estimada} min para el último):\n"
        + cola_txt + "\n\n"
        f"Estadísticas de hoy:\n"
        f"  - Turnos atendidos: {atendidos_hoy}\n"
        f"  - Ausentes: {ausentes_hoy}\n"
        f"  - Cancelados: {cancelados_hoy}\n"
        f"================================================="
    )


async def stream_ollama(prompt_completo: str):
    """Llama a Ollama con streaming y hace yield de cada fragmento de texto."""
    payload = {
        "model":  MODELO,
        "prompt": prompt_completo,
        "stream": True,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("POST", OLLAMA_URL, json=payload) as response:
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    token = data.get("response", "")
                    if token:
                        # Enviamos cada token como SSE para que el frontend lo consuma
                        yield f"data: {json.dumps({'token': token})}\n\n"
                    if data.get("done"):
                        yield "data: [DONE]\n\n"
                        return
                except json.JSONDecodeError:
                    continue


@router.post("/chat")
async def chat(body: MensajeChat):
    contexto = construir_contexto()

    prompt_completo = (
        f"{SYSTEM_PROMPT}\n\n"
        f"{contexto}\n\n"
        f"Usuario: {body.mensaje}\n"
        f"Asistente:"
    )

    return StreamingResponse(
        stream_ollama(prompt_completo),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )