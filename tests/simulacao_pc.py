# ==============================================================================
# simulacao_pc.py — Roda o firmware/main.py NO PC, sem placa, de ponta a ponta
#
#   python tests/simulacao_pc.py
#
# O que faz:
#   - Substitui os módulos do MicroPython (machine, network, urequests, ntptime)
#     por simulações; urequests.post() faz um POST HTTP DE VERDADE.
#   - Sobe o backend/servidor_teste.py numa porta livre do PC.
#   - Executa o loop do main.py com relógio virtual (~3 min em poucos segundos):
#       0–40 s  online  -> pacotes enviados na hora
#       40–80 s queda do Wi-Fi -> pacotes acumulam na fila de contingência
#       80 s    Wi-Fi volta -> fila é drenada em ordem FIFO
#       100 s   botão segurado 3 s -> modo offline simulado
#       130 s   botão segurado 3 s -> volta ao modo online
#   - Confere que o servidor recebeu TODOS os pacotes, sem perda e em ordem.
#
# Serve para validar a lógica; a evidência oficial é o ESP32 real/Wokwi.
# ==============================================================================
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(RAIZ, "firmware"))
sys.path.insert(0, os.path.join(RAIZ, "backend"))

# ------------------------------------------------------------------------------
# Relógio virtual + roteiro de eventos
# ------------------------------------------------------------------------------
relogio = {"ms": 0}
rede = {"enlace": True}          # o "roteador" está no ar?
random.seed(42)


def segundos():
    return relogio["ms"] / 1000.0


def botao_pressionado_agora():
    s = segundos()
    return (20 <= s < 21) or (100 <= s < 103.5) or (130 <= s < 133.5)


def roteiro():
    s = segundos()
    if 40 <= s < 80:
        if rede["enlace"]:
            print("\n>>> [simulação] Wi-Fi CAIU em t={:.1f}s\n".format(s))
        rede["enlace"] = False
        wlan_sim.conectado = False
    elif s >= 80 and not rede["enlace"]:
        print("\n>>> [simulação] Roteador voltou em t={:.1f}s\n".format(s))
        rede["enlace"] = True


# ------------------------------------------------------------------------------
# Simulação dos módulos do MicroPython
# ------------------------------------------------------------------------------
time.ticks_ms = lambda: relogio["ms"]
time.ticks_diff = lambda a, b: a - b


def _sleep_ms(ms):
    relogio["ms"] += ms
    roteiro()


time.sleep_ms = _sleep_ms

machine = types.ModuleType("machine")


class Pin:
    OUT, IN, PULL_UP = 1, 0, 2

    def __init__(self, numero, modo=None, pull=None):
        self.numero, self._v = numero, 0

    def value(self, v=None):
        if v is None:
            if self.numero == 15:  # botão com PULL_UP: 0 = pressionado
                return 0 if botao_pressionado_agora() else 1
            return self._v
        self._v = v


class ADC:
    ATTN_11DB = 3

    def __init__(self, pino):
        pass

    def atten(self, a):
        pass

    def read(self):
        base = 2048 + 1800 * math.sin(segundos() / 15.0)
        return max(0, min(4095, int(base + random.gauss(0, 120))))  # com ruído


class PWM:
    def __init__(self, pino, freq=0):
        self._d = 0

    def duty(self, d=None):
        if d is None:
            return self._d
        assert 0 <= d <= 1023, d
        self._d = d


machine.Pin, machine.ADC, machine.PWM = Pin, ADC, PWM
sys.modules["machine"] = machine


class WLAN:
    def __init__(self):
        self.conectado = True

    def isconnected(self):
        return self.conectado

    def active(self, a=None):
        return True

    def connect(self, ssid, senha):
        if rede["enlace"]:
            self.conectado = True
            print(">>> [simulação] wlan.connect() reconectou.")

    def ifconfig(self):
        return ("10.10.0.2", "255.255.255.0", "10.10.0.1", "10.10.0.1")


wlan_sim = WLAN()
network = types.ModuleType("network")
network.STA_IF = 0
network.WLAN = lambda _modo: wlan_sim
sys.modules["network"] = network

ntptime = types.ModuleType("ntptime")
ntptime.settime = lambda: None
sys.modules["ntptime"] = ntptime


class Resposta:
    def __init__(self, status, corpo):
        self.status_code, self.text = status, corpo

    def close(self):
        pass


def post(url, json=None, timeout=None):
    if not wlan_sim.conectado:
        raise OSError(113, "EHOSTUNREACH")
    dados = __import__("json").dumps(json).encode()
    req = urllib.request.Request(url, data=dados, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Resposta(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        return Resposta(e.code, e.read().decode())


urequests = types.ModuleType("urequests")
urequests.post = post
sys.modules["urequests"] = urequests

# ------------------------------------------------------------------------------
# Servidor de teste real numa porta livre
# ------------------------------------------------------------------------------
import servidor_teste  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

servidor_teste.ARQUIVO = os.path.join(tempfile.mkdtemp(), "leituras.jsonl")
srv = ThreadingHTTPServer(("127.0.0.1", 0), servidor_teste.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
porta = srv.server_address[1]

config = types.ModuleType("config")
config.WIFI_SSID, config.WIFI_SENHA = "RedeSimulada", "x"
config.API_URL = "http://127.0.0.1:{}/leituras".format(porta)
config.DISPOSITIVO_ID = "esp32-simulado"
config.INTERVALO_ENVIO_S = 5
sys.modules["config"] = config

# ------------------------------------------------------------------------------
# Executa o firmware
# ------------------------------------------------------------------------------
import main  # noqa: E402  (o if __name__ == "__main__" não dispara aqui)

CICLOS = 600  # 600 × 300 ms = 180 s virtuais
main.executar(max_ciclos=CICLOS)
# Dá uma última chance de drenar o que restou
main.esvaziar_fila()
srv.shutdown()

# ------------------------------------------------------------------------------
# Verificações
# ------------------------------------------------------------------------------
with open(servidor_teste.ARQUIVO, encoding="utf-8") as f:
    recebidos = [json.loads(l) for l in f]
seqs = [p["seq"] for p in recebidos]
gerados = main.estado["seq"]
offline = [p["seq"] for p in recebidos if p["gerado_offline"]]

print("\n" + "=" * 70)
print("Pacotes gerados pelo firmware : ", gerados)
print("Pacotes recebidos no servidor : ", len(recebidos))
print("Gerados offline e reenviados  : ", len(offline), offline)
print("Exemplo de pacote recebido    :")
print(json.dumps(recebidos[len(recebidos) // 2], indent=2, ensure_ascii=False))

assert seqs == list(range(1, gerados + 1)), "perda ou fora de ordem: {}".format(seqs)
assert len(offline) >= 10, "a fila de contingência deveria ter sido usada"
assert len(main.fila) == 0 and main.fila.descartados == 0
print("\nOK: nenhum pacote perdido, ordem FIFO preservada.")
