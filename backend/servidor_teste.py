# ==============================================================================
# servidor_teste.py — Servidor HTTP de teste para receber a telemetria do ESP32
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
# Usa apenas a biblioteca padrão do Python (nada para instalar).
#
#   python backend/servidor_teste.py            # porta 5000
#   python backend/servidor_teste.py --porta 8080
#
# Endpoints:
#   POST /leituras  -> recebe um pacote JSON, valida, imprime e grava em
#                      backend/leituras_recebidas.jsonl (uma linha por pacote)
#   GET  /leituras  -> devolve os últimos pacotes recebidos (JSON)
#   GET  /          -> status do servidor
#
# No ESP32, configure em config.py:  API_URL = "http://<IP-DO-PC>:5000/leituras"
# (o PC e o ESP32 precisam estar na mesma rede; libere a porta no firewall).
# ==============================================================================
import argparse
import json
import os
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAMPOS_OBRIGATORIOS = ("dispositivo", "seq", "adc", "tensao_v", "botao_pressionado")
ARQUIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "leituras_recebidas.jsonl")
MAX_EM_MEMORIA = 200

_recebidas = []
_lock = threading.Lock()


def ip_local():
    """Descobre o IP do PC na rede local (para colocar no config.py)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "NexusDataTeste/1.0"

    def _responder(self, status, corpo):
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        if self.path.rstrip("/") == "/leituras":
            with _lock:
                ultimas = _recebidas[-50:]
            self._responder(200, {"total": len(_recebidas), "leituras": ultimas})
        elif self.path == "/":
            self._responder(200, {"status": "ok", "recebidas": len(_recebidas)})
        else:
            self._responder(404, {"erro": "rota não encontrada"})

    def do_POST(self):
        if self.path.rstrip("/") != "/leituras":
            self._responder(404, {"erro": "use POST /leituras"})
            return
        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho)
        try:
            pacote = json.loads(bruto.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._responder(400, {"erro": "corpo não é JSON válido"})
            return
        if not isinstance(pacote, dict):
            self._responder(400, {"erro": "esperado um objeto JSON"})
            return
        faltando = [c for c in CAMPOS_OBRIGATORIOS if c not in pacote]
        if faltando:
            self._responder(422, {"erro": "campos ausentes", "campos": faltando})
            return

        registro = dict(pacote)
        registro["recebido_em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        registro["ip_origem"] = self.client_address[0]
        with _lock:
            _recebidas.append(registro)
            del _recebidas[:-MAX_EM_MEMORIA]
            with open(ARQUIVO, "a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")

        print("[{}] {} seq={:<4} ADC={:<4} ({:.2f} V) média={} botão={} {}".format(
            registro["recebido_em"][11:19], pacote["dispositivo"], pacote["seq"],
            pacote["adc"], pacote["tensao_v"], pacote.get("adc_media_movel"),
            "ON " if pacote["botao_pressionado"] else "OFF",
            "(reenviado da fila offline)" if pacote.get("gerado_offline") else ""))
        self._responder(201, {"status": "recebido", "seq": pacote["seq"]})

    def log_message(self, formato, *args):
        pass  # silencia o log padrão; usamos o print acima


def main():
    ap = argparse.ArgumentParser(description="Servidor de teste da telemetria ESP32")
    ap.add_argument("--porta", type=int, default=5000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    servidor = ThreadingHTTPServer((args.host, args.porta), Handler)
    print("Servidor de teste ouvindo em http://{}:{}/leituras".format(ip_local(), args.porta))
    print("Gravando pacotes em", ARQUIVO)
    print("Ctrl+C para encerrar.\n")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado. Pacotes recebidos:", len(_recebidas))
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
