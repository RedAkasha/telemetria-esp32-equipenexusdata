# ==============================================================================
# api_cliente.py — Envio de comandos do dashboard para a API
#
# O dashboard LÊ o banco direto, mas ESCREVE só pela API: ela é a única que
# grava no SQLite (um escritor, transações atômicas, auditoria e a fila de
# comandos em memória que o ESP32 consulta).
# ==============================================================================
import json
import urllib.error
import urllib.request

# Sem proxy: a API fica na rede local (um proxy do sistema quebraria o localhost)
_abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _requisicao(url, corpo=None, timeout=5):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method="POST" if corpo is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with _abridor.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def enviar_comando(api, dispositivo, tipo, duracao_s=None, validade_s=60):
    """Cria o comando na API. Devolve (ok, comando | mensagem de erro)."""
    corpo = {"dispositivo": dispositivo, "tipo": tipo, "validade_s": validade_s,
             "origem": "dashboard"}
    if duracao_s is not None:
        corpo["duracao_s"] = int(duracao_s)
    try:
        status, resposta = _requisicao(api.rstrip("/") + "/comandos", corpo)
    except (urllib.error.URLError, OSError) as e:
        return False, "API fora do ar em {} ({})".format(api, getattr(e, "reason", e))
    if status == 201:
        return True, resposta
    if isinstance(resposta, dict):
        erro = resposta.get("erro") or resposta.get("detail") or resposta
    else:
        erro = resposta
    return False, "API respondeu {}: {}".format(status, erro)


def api_no_ar(api):
    try:
        status, _ = _requisicao(api.rstrip("/") + "/", timeout=2)
        return status == 200
    except (urllib.error.URLError, OSError):
        return False
