"""Comandos dashboard -> API -> ESP32: criação, entrega FIFO, confirmação e expiração."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import criar_app
from api.servico import MAX_ENTREGAS, PRAZO_CONFIRMACAO_S
from conftest import DISP


class Relogio:
    """Relógio controlado pelo teste (o serviço usa svc._relogio)."""

    def __init__(self):
        self.agora = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.agora

    def avancar(self, segundos):
        self.agora += timedelta(seconds=segundos)


@pytest.fixture
def relogio(cliente):
    r = Relogio()
    cliente.app.state.servico._relogio = r
    return r


@pytest.fixture
def com_totem(cliente, totem):
    cliente.post("/leituras", json=totem.inicio())
    return totem


def criar(cliente, tipo, **extra):
    return cliente.post("/comandos", json=dict(dispositivo=DISP, tipo=tipo, **extra))


def proximo(cliente, dispositivo=DISP):
    return cliente.get("/dispositivos/{}/comandos/proximo".format(dispositivo)).json()["comando"]


def confirmar(cliente, cid, resultado="executado", dispositivo=DISP, **extra):
    return cliente.post("/comandos/{}/confirmacao".format(cid),
                        json=dict(dispositivo=dispositivo, resultado=resultado, **extra))


def situacao(cliente, cid):
    return cliente.get("/comandos/{}".format(cid)).json()["situacao"]


# ---------------------------------------------------------------- validação
def test_comando_para_totem_desconhecido_e_recusado(cliente):
    assert criar(cliente, "travar").status_code == 404


@pytest.mark.parametrize("corpo", [
    {"tipo": "abrir_cofre"},                          # tipo inexistente
    {"tipo": "travar", "duracao_s": 5},               # duração só vale para alarme
    {"tipo": "acionar_alarme", "duracao_s": 600},     # alarme longo demais
    {"tipo": "travar", "validade_s": 1},              # validade curta demais
    {"tipo": "travar", "extra": 1},                   # campo desconhecido
])
def test_comando_invalido_422(cliente, com_totem, corpo):
    assert cliente.post("/comandos", json=dict(dispositivo=DISP, **corpo)).status_code == 422


# ----------------------------------------------------------- ciclo completo
def test_ciclo_criar_entregar_confirmar(cliente, com_totem, banco, relogio):
    r = criar(cliente, "acionar_alarme", duracao_s=5)
    assert r.status_code == 201
    cmd = r.json()
    assert cmd["situacao"] == "pendente" and cmd["parametros"] == {"duracao_s": 5}

    entregue = proximo(cliente)
    assert entregue == {"id": cmd["id"], "tipo": "acionar_alarme",
                        "parametros": {"duracao_s": 5}, "tentativa": 1}
    assert situacao(cliente, cmd["id"]) == "entregue"
    assert proximo(cliente) is None                   # aguardando a confirmação

    r = confirmar(cliente, cmd["id"], estado={"travado": False, "alarme": True})
    assert r.status_code == 200
    final = r.json()["comando"]
    assert final["situacao"] == "executado" and final["estado_totem"]["alarme"] is True
    assert proximo(cliente) is None

    # confirmação repetida (o ESP32 não recebeu a resposta) é idempotente
    r = confirmar(cliente, cmd["id"])
    assert r.status_code == 200 and r.json()["status"] == "ja_confirmado"

    eventos = [l["evento"] for l in cliente.get("/auditoria").json()]
    assert {"comando_criado", "comando_entregue", "comando_executado"} <= set(eventos)
    assert cliente.get("/").json()["comandos_abertos"] == 0


def test_fila_fifo_um_comando_por_vez(cliente, com_totem, relogio):
    ids = [criar(cliente, t).json()["id"] for t in ("travar", "liberar_trava", "resetar_contador")]
    entregues = []
    for _ in ids:
        c = proximo(cliente)
        entregues.append(c["id"])
        assert proximo(cliente) is None               # o seguinte espera a confirmação
        confirmar(cliente, c["id"])
    assert entregues == ids                           # ordem de criação preservada


def test_falha_informada_pelo_totem(cliente, com_totem, relogio):
    cid = criar(cliente, "travar").json()["id"]
    proximo(cliente)
    r = confirmar(cliente, cid, "falhou", detalhe="pino da trava não configurado")
    assert r.json()["comando"]["situacao"] == "falhou"
    niveis = {l["evento"]: l["nivel"] for l in cliente.get("/auditoria").json()}
    assert niveis["comando_falhou"] == "aviso"


def test_confirmacao_de_outro_totem_ou_nao_entregue(cliente, com_totem, relogio):
    cid = criar(cliente, "travar").json()["id"]
    assert confirmar(cliente, cid).status_code == 409             # ainda não entregue
    proximo(cliente)
    assert confirmar(cliente, cid, dispositivo="intruso").status_code == 409
    assert confirmar(cliente, 999).status_code == 404
    assert situacao(cliente, cid) == "entregue"


# ------------------------------------------------- expiração e reentrega
def test_comando_nao_buscado_expira(cliente, com_totem, relogio):
    cid = criar(cliente, "acionar_alarme", validade_s=30).json()["id"]
    relogio.avancar(31)
    assert proximo(cliente) is None                   # o alarme de 31 s atrás não toca
    assert situacao(cliente, cid) == "expirado"
    assert "comando_expirado" in [l["evento"] for l in cliente.get("/auditoria").json()]


def test_varredura_expira_sem_o_totem_consultar(cliente, com_totem, relogio):
    cid = criar(cliente, "travar", validade_s=5).json()["id"]
    relogio.avancar(6)
    assert cliente.app.state.servico.varrer_comandos() == 1
    assert situacao(cliente, cid) == "expirado"


def test_reentrega_sem_confirmacao_e_sem_resposta(cliente, com_totem, relogio):
    cid = criar(cliente, "travar").json()["id"]
    for tentativa in range(1, MAX_ENTREGAS + 1):
        c = proximo(cliente)
        assert c["id"] == cid and c["tentativa"] == tentativa
        relogio.avancar(PRAZO_CONFIRMACAO_S)
    assert proximo(cliente) is None
    assert situacao(cliente, cid) == "sem_resposta"
    # o totem estava sem rede, executou e confirma depois: aceito
    r = confirmar(cliente, cid)
    assert r.status_code == 200 and r.json()["comando"]["situacao"] == "executado"


def test_comandos_abertos_recarregados_ao_reiniciar(banco, totem):
    with TestClient(criar_app(banco)) as c:
        c.post("/leituras", json=totem.inicio())
        a = criar(c, "travar").json()["id"]
        b = criar(c, "liberar_trava").json()["id"]
        assert proximo(c)["id"] == a
    with TestClient(criar_app(banco)) as c:                    # servidor reiniciado
        c.app.state.servico._relogio = lambda: datetime.now(timezone.utc) + timedelta(
            seconds=PRAZO_CONFIRMACAO_S)
        assert proximo(c) == {"id": a, "tipo": "travar", "parametros": {}, "tentativa": 2}
        confirmar(c, a)
        assert proximo(c)["id"] == b


def test_banco_recusa_situacao_incoerente(cliente, com_totem, banco, relogio):
    cid = criar(cliente, "travar").json()["id"]
    with sqlite3.connect(banco) as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE comandos SET situacao = 'executado' WHERE id = ?", (cid,))


# ------------------------------------------------ reset de contador (sessão)
def test_reset_de_contador_vira_nova_sessao(cliente, com_totem):
    totem = com_totem
    cliente.post("/leituras", json=totem.emitir("N001"))
    totem.reiniciar()                                 # o firmware zera seq e contadores
    p = totem.pacote("inicio", ip="192.168.0.50", motivo="resetar_contador", comando_id=7)
    r = cliente.post("/leituras", json=p)
    assert r.json()["avisos"] == ["contador_resetado"]
    r = cliente.post("/leituras", json=totem.status(0, 0))
    assert "avisos" not in r.json()                   # contagem da sessão nova bate
    assert cliente.get("/saldos/" + DISP).json()["boot"] == 2


def test_status_com_estado_dos_atuadores(cliente, com_totem):
    r = cliente.post("/leituras", json=com_totem.status(0, 0, travado=True, alarme=False))
    assert r.status_code == 201
    r = cliente.post("/leituras", json=com_totem.status(0, 0, travado="sim"))
    assert r.status_code == 422
