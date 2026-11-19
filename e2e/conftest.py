# Infraestrutura do teste end-to-end: API real (uvicorn, outro processo), banco
# SQLite real, firmware real no ESP32 virtual e o dashboard Streamlit real.
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))                     # simulador/
sys.path.insert(0, str(RAIZ / "dashboard"))       # dados.py, api_cliente.py

_abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServidorAPI:
    """`python -m uvicorn api.main:app` num processo separado, como em produção."""

    def __init__(self, banco, log):
        self.banco, self.log = banco, log
        self.porta = porta_livre()
        self.url = "http://127.0.0.1:{}".format(self.porta)
        self.proc = None

    def iniciar(self, espera_s=20):
        env = dict(os.environ, NEXUS_DB=str(self.banco))
        saida = open(self.log, "a")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
             "--port", str(self.porta), "--log-level", "warning"],
            cwd=RAIZ / "backend", env=env, stdout=saida, stderr=subprocess.STDOUT)
        limite = time.monotonic() + espera_s
        while time.monotonic() < limite:
            try:
                with _abridor.open(self.url + "/", timeout=1) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise TimeoutError("API não subiu; veja " + str(self.log))

    def parar(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)


@pytest.fixture(scope="module")
def pasta(tmp_path_factory):
    return tmp_path_factory.mktemp("e2e")


@pytest.fixture(scope="module")
def api(pasta):
    srv = ServidorAPI(pasta / "nexus.db", pasta / "api.log")
    srv.iniciar()
    yield srv
    srv.parar()
