import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # pasta backend/

from api.main import criar_app  # noqa: E402

DISP = "esp32-nexus-01"


@pytest.fixture
def banco(tmp_path):
    return tmp_path / "teste.db"


@pytest.fixture
def cliente(banco):
    with TestClient(criar_app(banco)) as c:
        yield c


class Totem:
    """Gera pacotes como o firmware: seq sobe a cada pacote, uptime avança."""

    def __init__(self, dispositivo=DISP):
        self.dispositivo = dispositivo
        self.seq = 0
        self.uptime = 1000

    def pacote(self, tipo, **campos):
        self.seq += 1
        self.uptime += 1234
        p = {"dispositivo": self.dispositivo, "seq": self.seq, "tipo": tipo,
             "timestamp": "2026-11-12T13:00:{:02d}Z".format(self.seq % 60),
             "uptime_ms": self.uptime}
        p.update(campos)
        return p

    def inicio(self):
        return self.pacote("inicio", ip="192.168.0.50")

    def emitir(self, senha):
        cat = "convencional" if senha[0] == "N" else "prioritaria"
        return self.pacote("senha_emitida", senha=senha, categoria=cat)

    def cancelar(self, senha):
        return self.pacote("senha_cancelada", senha=senha)

    def status(self, n, p, **extra):
        campos = dict(canceladas=0, intervalo_medio_s=None, historico=[],
                      fila_pendentes=0, descartados=0, rssi_dbm=-60, mem_livre=90000)
        campos.update(extra)
        return self.pacote("status", total_convencional=n, total_prioritaria=p, **campos)

    def reiniciar(self):
        self.seq = 0
        self.uptime = 900


@pytest.fixture
def totem():
    return Totem()
