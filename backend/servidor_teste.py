# ==============================================================================
# servidor_teste.py — Servidor HTTP de teste para a telemetria do ESP32
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
# Recebe os pacotes JSON enviados pelo totem (main.py, via urequests.post),
# valida os campos, mostra cada pacote no terminal e grava tudo em
# backend/leituras_recebidas.jsonl (um pacote JSON por linha).
#
# Usa só a biblioteca padrão do Python 3 — nada para instalar.
#
#   python backend/servidor_teste.py            # porta 5000
#   python backend/servidor_teste.py 8080       # outra porta
#
# Endpoints:
#   POST /leituras   recebe uma leitura (responde 201 ou 400)
#   GET  /leituras   devolve as 20 últimas leituras recebidas
#   GET  /           resumo (total recebido, último pacote)
#
# Este é um servidor de TESTE para validar o envio do firmware. A API definitiva
# (validação + gravação no banco relacional) vem nas próximas entregas.
# ==============================================================================
import json
import socket
import sys
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORTA = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
ARQUIVO = Path(__file__).with_name("leituras_recebidas.jsonl")

CAMPOS_OBRIGATORIOS = {
    "dispositivo": str,
    "seq": int,
    "tipo": str,
}
# Campos exigidos conforme o tipo do pacote
CAMPOS_POR_TIPO = {
    "senha_emitida": {"senha": str, "categoria": str},
    "senha_cancelada": {"senha": str},
    "status": {"total_convencional": int, "total_prioritaria": int},
}

ultimas = deque(maxlen=20)       # janela das últimas leituras (para o GET)
ultimo_seq = {}                  # dispositivo -> último seq visto (detecta perdas)
total = 0


def validar(pacote):
    """Retorna uma lista de erros (vazia se o pacote é válido)."""
    if not isinstance(pacote, dict):
        return ["o corpo deve ser um objeto JSON"]
    erros = []
    for campo, tipo in CAMPOS_OBRIGATORIOS.items():
        if campo not in pacote:
            erros.append("campo ausente: " + campo)
        elif not isinstance(pacote[campo], tipo):
            erros.append("tipo inválido: " + campo)
    for campo, tipo in CAMPOS_POR_TIPO.get(pacote.get("tipo"), {}).items():
        if not isinstance(pacote.get(campo), tipo):
            erros.append("campo ausente ou inválido: " + campo)
    return erros


def resumo(p):
    """Texto curto com o conteúdo principal de cada tipo de pacote."""
    tipo = p.get("tipo")
    if tipo == "senha_emitida":
        return "senha {} ({})".format(p["senha"], p["categoria"])
    if tipo == "senha_cancelada":
        return "senha {} CANCELADA".format(p["senha"])
    if tipo == "status":
        return "N={} P={} canc={} interv.médio={}s fila={} rssi={}".format(
            p["total_convencional"], p["total_prioritaria"], p.get("canceladas"),
            p.get("intervalo_medio_s"), p.get("fila_pendentes"), p.get("rssi_dbm"))
    return ""


class Handler(BaseHTTPRequestHandler):
    def _responder(self, codigo, corpo):
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_POST(self):
        global total
        if self.path.rstrip("/") != "/leituras":
            return self._responder(404, {"erro": "use POST /leituras"})

        tamanho = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(tamanho)
        try:
            pacote = json.loads(bruto)
        except (json.JSONDecodeError, UnicodeDecodeError):
            print("[400] JSON inválido:", bruto[:120])
            return self._responder(400, {"erro": "JSON inválido"})

        erros = validar(pacote)
        if erros:
            print("[400]", erros)
            return self._responder(400, {"erro": erros})

        recebido_em = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pacote["recebido_em"] = recebido_em
        total += 1

        # Detecta pacotes perdidos/reenviados pela sequência
        disp, seq = pacote["dispositivo"], pacote["seq"]
        aviso = ""
        anterior = ultimo_seq.get(disp)
        if anterior is not None and seq > anterior + 1:
            aviso = "  ⚠ {} pacote(s) faltando".format(seq - anterior - 1)
        elif anterior is not None and seq <= anterior:
            aviso = "  ↺ seq repetido/reiniciado"
        ultimo_seq[disp] = seq

        ultimas.append(pacote)
        with ARQUIVO.open("a", encoding="utf-8") as f:
            f.write(json.dumps(pacote, ensure_ascii=False) + "\n")

        print("[201] {} {} seq={:<4} {:<15} {:<40} gerado={}{}".format(
                  recebido_em[11:19], disp, seq, pacote["tipo"], resumo(pacote),
                  pacote.get("timestamp"), aviso))
        self._responder(201, {"status": "ok", "seq": seq, "total": total})

    def do_GET(self):
        if self.path.rstrip("/") == "/leituras":
            return self._responder(200, list(ultimas))
        self._responder(200, {
            "servidor": "NEXUS Data — servidor de teste",
            "total_recebido": total,
            "ultimo": ultimas[-1] if ultimas else None,
        })

    def log_message(self, *args):
        pass  # silencia o log padrão; já imprimimos uma linha por leitura


def ip_local():
    """Descobre o IP do computador na rede local (não envia nada pela rede)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Handler)
    print("=" * 70)
    print("Servidor de teste NEXUS Data no ar")
    principal = ip_local()
    print("No config.py do ESP32 use:  API_URL = \"http://{}:{}/leituras\"".format(
        principal, PORTA))
    try:
        outros = sorted(set(socket.gethostbyname_ex(socket.gethostname())[2]) - {principal})
    except OSError:
        outros = []
    if outros:
        print("Outros IPs deste computador:", ", ".join(outros))
        print("Use o IPv4 do adaptador Wi-Fi (confira com: ipconfig).")
    print("Gravando em:", ARQUIVO)
    print("Ctrl+C para encerrar")
    print("=" * 70)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado. Total recebido:", total)
