from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database import inicializar_db
from routes.turnos import router

app = FastAPI(title="Sistema de Turnos")

@app.on_event("startup")
def startup():
    inicializar_db()

app.include_router(router, prefix="/api")
app.mount("/static", StaticFiles(directory="static"), name="static")

# / → redirige al monitor (la raíz la ve el TV)
@app.get("/")
def raiz():
    return FileResponse("static/monitor.html")

# /monitor → pantalla pública con QR
@app.get("/monitor")
def monitor():
    return FileResponse("static/monitor.html")

# /turno → página que abre el celular al escanear el QR
@app.get("/turno")
def turno():
    return FileResponse("static/index.html")

# /operador → panel del operador en cada ventanilla
@app.get("/operador")
def operador():
    return FileResponse("static/operador.html")