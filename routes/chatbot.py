from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from datetime import datetime
import httpx
import json
import asyncio
from typing import AsyncGenerator

from database import get_connection

router = APIRouter()

# ============================================================
#  CONFIGURACIÓN
# ============================================================
OLLAMA_URL = "http://localhost:11434/api/generate"
MODELO = "llama3.2:3b"
TIMEOUT_CONEXION = 10.0      # segundos para establecer conexión
TIMEOUT_LECTURA = 180.0      # segundos para recibir el primer token / completar stream

SYSTEM_PROMPT = """Eres un asistente de atención al cliente para un sistema de turnos.
Responde ÚNICAMENTE sobre información del sistema: turnos en cola, tiempos de espera,
estado de ventanillas y posiciones. Sé breve, amable y directo.
Si te preguntan algo fuera de este tema, indica amablemente que solo puedes
ayudar con información del sistema de turnos."""


# ============================================================
#  MODELO DE DATOS CON VALIDACIÓN
# ============================================================
class MensajeChat(BaseModel):
    mensaje: str = Field(..., min_length=1, max_length=500, description="Consulta del usuario")


# ============================================================
#  CONSTRUCCIÓN DEL CONTEXTO (CON MANEJO DE ERRORES)
# ============================================================
def construir_contexto() -> str:
    """Consulta la DB y devuelve un resumen completo del estado del sistema.
    Si ocurre algún error, lanza una excepción con un mensaje claro."""
    try:
        conn = get_connection()
    except Exception as e:
        raise RuntimeError(f"No se pudo conectar a la base de datos: {e}")

    try:
        # ---- Ventanillas activas (LLAMADO o ATENDIENDO) ----
        cursor = conn.execute("""
            SELECT ventanilla, numero, hora_llamado
            FROM turnos
            WHERE estado IN ('LLAMADO', 'ATENDIENDO')
            ORDER BY ventanilla
        """)
        atendiendo = {}
        for row in cursor.fetchall():
            atendiendo[row["ventanilla"]] = (row["numero"], row["hora_llamado"])

        ventanillas_lineas = []
        for n in range(1, 4):  # ventanillas 1 a 3
            if n in atendiendo:
                numero, hora = atendiendo[n]
                ventanillas_lineas.append(
                    f"  - Ventanilla {n}: atendiendo turno {numero} (desde {hora or 'hace un momento'})"
                )
            else:
                ventanillas_lineas.append(f"  - Ventanilla {n}: libre")

        # ---- Cola de espera (estado EN_COLA) ----
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
        espera_estimada = total_cola * 4   # 4 minutos por turno (aproximación)

        # ---- Estadísticas del día ----
        hoy = ahora.strftime("%Y-%m-%d")
        cursor = conn.execute("""
            SELECT estado, COUNT(*) as cnt
            FROM turnos
            WHERE DATE(hora_creacion) = ?
            GROUP BY estado
        """, (hoy,))
        stats = {row["estado"]: row["cnt"] for row in cursor.fetchall()}

        atendidos_hoy  = stats.get("FINALIZADO", 0)
        ausentes_hoy   = stats.get("AUSENTE", 0)
        cancelados_hoy = stats.get("CANCELADO", 0)

        # ---- Ensamblado del texto ----
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
    finally:
        conn.close()


# ============================================================
#  GENERADOR SSE CON MANEJO ROBUSTO DE ERRORES
# ============================================================
async def generar_stream_sse(prompt_completo: str) -> AsyncGenerator[str, None]:
    """
    Consulta a Ollama en modo streaming y produce eventos SSE con formato:
        data: {"token": "texto"}
        data: [DONE]
    Maneja errores de conexión, timeouts, respuestas HTTP no exitosas,
    y cancelación del cliente.
    """
    # Configuración de timeout: conexión + lectura global
    timeout_config = httpx.Timeout(TIMEOUT_CONEXION, read=TIMEOUT_LECTURA)

    try:
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            async with client.stream(
                "POST",
                OLLAMA_URL,
                json={"model": MODELO, "prompt": prompt_completo, "stream": True}
            ) as response:
                # Verificar código de estado HTTP
                if response.status_code != 200:
                    # Intentar leer el cuerpo del error
                    error_body = await response.aread()
                    error_msg = f"Ollama responded with {response.status_code}: {error_body.decode()[:200]}"
                    print(f"[chatbot] {error_msg}")
                    yield f"data: {json.dumps({'error': 'Error en el servidor de IA. Intente más tarde.'})}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                # Procesar el stream línea por línea (cada línea es un JSON de Ollama)
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as e:
                        print(f"[chatbot] JSON inválido: {line!r} -> {e}")
                        continue

                    # Detectar error interno de Ollama
                    if "error" in data:
                        print(f"[chatbot] Error de Ollama: {data['error']}")
                        yield f"data: {json.dumps({'error': 'Error interno del modelo.'})}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    token = data.get("response", "")
                    if token:
                        yield f"data: {json.dumps({'token': token})}\n\n"

                    # Final del stream
                    if data.get("done"):
                        print("[chatbot] Stream completado correctamente.")
                        break

                # Señal de fin para el cliente
                yield "data: [DONE]\n\n"

    except httpx.ConnectError as e:
        print(f"[chatbot] Error de conexión a Ollama: {e}")
        yield f"data: {json.dumps({'error': 'No se pudo conectar con el servicio de IA. ¿Ollama está corriendo?'})}\n\n"
        yield "data: [DONE]\n\n"

    except httpx.ReadTimeout as e:
        print(f"[chatbot] Timeout de lectura: {e}")
        yield f"data: {json.dumps({'error': 'El modelo tardó demasiado en responder. Intente de nuevo.'})}\n\n"
        yield "data: [DONE]\n\n"

    except asyncio.CancelledError:
        # El cliente cerró la conexión (por ejemplo, recarga de página)
        print("[chatbot] Stream cancelado por el cliente")
        raise  # Permite que FastAPI maneje la cancelación limpiamente

    except Exception as e:
        print(f"[chatbot] Error inesperado en el stream: {type(e).__name__}: {e}")
        yield f"data: {json.dumps({'error': 'Error interno del servidor.'})}\n\n"
        yield "data: [DONE]\n\n"


# ============================================================
#  ENDPOINT PRINCIPAL /chat
# ============================================================
@router.post("/chat")
async def chat(body: MensajeChat):
    """
    Recibe un mensaje del usuario, construye el contexto en tiempo real,
    y devuelve un stream SSE con la respuesta del asistente.
    """
    # 1. Obtener el contexto (falla rápido si hay error en DB)
    try:
        contexto = construir_contexto()
    except Exception as e:
        print(f"[chatbot] Error en construir_contexto: {e}")
        # Lanzamos HTTPException para que FastAPI devuelva 500 sin intentar stream
        raise HTTPException(status_code=500, detail=f"Error al obtener estado del sistema: {str(e)}")

    # 2. Construir el prompt completo con system prompt + contexto + mensaje
    prompt_completo = (
        f"{SYSTEM_PROMPT}\n\n"
        f"{contexto}\n\n"
        f"Usuario: {body.mensaje}\n"
        f"Asistente:"
    )

    # Opcional: log del prompt (útil para depuración)
    print(f"[chatbot] Prompt enviado a Ollama ({len(prompt_completo)} caracteres)")

    # 3. Devolver StreamingResponse con el generador SSE
    return StreamingResponse(
        generar_stream_sse(prompt_completo),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Desactiva buffering en Nginx
            "Connection": "keep-alive",
        }
    )