import re
import time
import logging
import asyncio
import smbus2
from fastapi import FastAPI, HTTPException, Path, Security, Depends, Request
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
from k16v5 import K16V5

# --- CONFIGURAÇÕES DE SEGURANÇA ---
API_KEY = "sua_chave_secreta_aqui" 
API_KEY_NAME = "X-Api-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

# --- LOGGING INDUSTRIAL ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("SmartLocker-Core")

# =================================================================
# DRIVER MCP23017 (INTEGRADO)
# =================================================================
class I2CWrapper:
    def __init__(self, bus):
        self.bus = bus
    def write_to(self, address, offset, value):
        self.bus.write_byte_data(address, offset, value)
    def read_from(self, address, offset):
        return self.bus.read_byte_data(address, offset)

class SensorMonitor:
    def __init__(self, address, i2c_wrapper):
        self.i2c = i2c_wrapper
        self.address = address
        self.GPIOA = 0x12
        self.GPIOB = 0x13
        # Configuração: Todos como Entrada (0xFF) e Pull-up Habilitado (0xFF)
        self.i2c.write_to(self.address, 0x00, 0xFF) # IODIRA
        self.i2c.write_to(self.address, 0x01, 0xFF) # IODIRB
        self.i2c.write_to(self.address, 0x0C, 0xFF) # GPPUA
        self.i2c.write_to(self.address, 0x0D, 0xFF) # GPPUB

    def digital_read(self, pin):
        # Mapeia pinos 0-7 para GPIOA e 8-15 para GPIOB
        offset = self.GPIOA if pin < 8 else self.GPIOB
        bit = pin % 8
        value = self.i2c.read_from(self.address, offset)
        # Retorna True se o contato seco estiver ABERTO (Lógica Inversa com Pull-up)
        # Se o sensor aterra o pino quando fechado, o bit será 0 (Closed) e 1 (Open)
        return bool((value >> bit) & 1)

# =================================================================
# INICIALIZAÇÃO DO HARDWARE
# =================================================================
try:
    bus = smbus2.SMBus(1)
    i2c_wrap = I2CWrapper(bus)
    
    # Atuador (Relés) - Endereço 0x20
    board = K16V5(1, 0x20)
    
    # Sentinela (Sensores MCP23017) - Endereço 0x21
    sensors = SensorMonitor(0x21, i2c_wrap)
    
    logger.info("Hardware I2C (K16V5 + MCP23017) inicializado com sucesso.")
except Exception as e:
    logger.error(f"Erro Crítico de Hardware: {e}")
    board = None
    sensors = None

app = FastAPI(title="Smart Locker Secure API", version="2.1.0")

# =================================================================
# MIDDLEWARES E DEPENDÊNCIAS
# =================================================================
async def get_api_key(header_key: str = Security(api_key_header)):
    if header_key == API_KEY: return header_key
    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="API Key Inválida.")

@app.middleware("http")
async def observability_log(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    logger.info(f"REQ: {request.method} {request.url.path} | IP: {request.client.host} | Tempo: {duration:.4f}s")
    return response

# =================================================================
# LÓGICA DE NEGÓCIO: EVENT LATCHING
# =================================================================
async def monitor_door_transition(pin_num: int, timeout: float = 4.0):
    """
    Realiza o polling de 50ms para capturar a transição de fechado -> aberto.
    Garante que mesmo retiradas rápidas sejam registradas.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        if sensors.digital_read(pin_num):
            return True # Porta abriu!
        await asyncio.sleep(0.05)
    return False # Timeout sem abertura

# =================================================================
# ENDPOINTS
# =================================================================
@app.get("/health", tags=["Diagnostic"])
async def health():
    return {"status": "online", "hardware_synced": board is not None and sensors is not None}

@app.get("/locker/open/{relay_id}", dependencies=[Depends(get_api_key)], tags=["Control"])
async def open_locker(relay_id: str = Path(..., example="A3")):
    if not board or not sensors:
        raise HTTPException(status_code=503, detail="Hardware offline.")

    # Parse do relay_id (ex: A3 -> section A, pino 3)
    match = re.match(r"^([A-B])([0-7])$", relay_id.upper())
    if not match: raise HTTPException(status_code=400, detail="ID Inválido.")
    section, pin = match.group(1), int(match.group(2))

    # Mapeamento do MCP23017: Usamos o pino correspondente ao relé
    # Ex: Relés de A0-A7 mapeados nos pinos 0-7 do MCP
    sensor_pin = pin if section == 'A' else pin + 8

    try:
        # 1. Envia Pulso
        await asyncio.to_thread(board.send_pulse, section, pin)
        
        # 2. Vigilância de Abertura (Polling em Janela)
        opened = await monitor_door_transition(sensor_pin)
        
        if opened:
            return {"status": "success", "event": "DOOR_OPENED", "relay": relay_id}
        else:
            logger.warning(f"FALHA FÍSICA: Porta {relay_id} não detectou abertura.")
            return {"status": "error", "event": "DOOR_STUCK", "relay": relay_id}

    except Exception as e:
        logger.error(f"Erro de I/O: {e}")
        raise HTTPException(status_code=500, detail="Erro no barramento I2C.")

@app.get("/locker/status/{relay_id}", dependencies=[Depends(get_api_key)], tags=["Control"])
async def get_status(relay_id: str = Path(...)):
    # Lógica de mapeamento idêntica ao open_locker
    match = re.match(r"^([A-B])([0-7])$", relay_id.upper())
    section, pin = match.group(1), int(match.group(2))
    sensor_pin = pin if section == 'A' else pin + 8
    
    is_open = sensors.digital_read(sensor_pin)
    return {"relay_id": relay_id, "is_open": is_open}
