from pydantic import BaseModel
from typing import Optional

class TurnoRespuesta(BaseModel):
    id: int
    numero: str
    personas_delante: int
    tiempo_estimado: int