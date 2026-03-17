from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database import inicializar_db
from routes.turnos import router

app = FastAPI(title="Sistema de Turnos")

# Crea la base de datos si no existe
@app.on_event("startup")
def startup():
    inicializar_db()

# registra las rutas de turnos bajo el prefijo /api
app.include_router(router, prefix="/api")

# sirve los archivos HTML/CSS/JS desde la carpeta static/
app.mount("/static", StaticFiles(directory="static"), name="static")

# cuando alguien entra a la razi, muestra el index.html
@app.get("/")
def raiz():
    return FileResponse("static/index.html")