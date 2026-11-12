import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from api.main import criar_app
from api.servico import ServicoTelemetria
from conftest import DISP, Totem


def post(cliente, pacote):
    return cliente.post("/leituras", json=pacote)


def eventos(cliente, **filtro):
    return [l["evento"] for l in cliente.get("/auditoria", params=filtro).json()]


def contar(banco, tabela):
    with sqlite3.connect(banco) as c:
        return c.execute("SELECT COUNT(*) FROM " + tabela).fetchone()[0]


# ------------------------------------------------------------------- validação
@pytest.mark.parametrize("pacote, campo", [
    ({"seq": 1, "tipo": "inicio"}, "dispositivo"),
    ({"dispositivo": DISP, "seq": "1", "tipo": "inicio"}, "seq"),          # string, não int
    ({"dispositivo": DISP, "seq": 0, "tipo": "inicio"}, "seq"),
    ({"dispositivo": DISP, "seq": 1, "tipo": "xyz"}, "tipo"),
    ({"dispositivo": DISP, "seq": 1, "tipo": "senha_emitida", "senha": "N1",
      "categoria": "convencional"}, "senha"),
    ({"dispositivo": DISP, "seq": 1, "tipo": "senha_emitida", "senha": "P001",
      "categoria": "convencional"}, "senha_emitida"),                      # prefixo errado
    ({"dispositivo": DISP, "seq": 1, "tipo": "status", "total_convencional": -1,
      "total_prioritaria": 0}, "total_convencional"),
    ({"dispositivo": "com espaço", "seq": 1, "tipo": "inicio"}, "dispositivo"),
])
def test_pacote_invalido_422_e_auditado(cliente, banco, pacote, campo):
    r = post(cliente, pacote)
    assert r.status_code == 422
    assert any(campo in e["campo"] for e in r.json()["erro"])
    assert contar(banco, "transacoes") == 0
    log = cliente.get("/auditoria").json()[0]
    assert log["evento"] == "pacote_rejeitado" and log["nivel"] == "erro"


def test_json_invalido_400(cliente):
    r = cliente.post("/leituras", content=b"{nao e json",
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert eventos(cliente) == ["pacote_rejeitado"]


def test_corpo_grande_413(cliente):
    r = cliente.post("/leituras", content=b"x" * 10000)
    assert r.status_code == 413


def test_pacote_minimo_do_curl_do_readme(cliente):
    r = post(cliente, {"dispositivo": "teste", "seq": 1, "tipo": "senha_emitida",
                       "senha": "N001", "categoria": "convencional"})
    assert r.status_code == 201


# ------------------------------------------------------------ fluxo de negócio
def test_emitir_e_cancelar_atualizam_saldo(cliente, banco, totem):
    assert post(cliente, totem.inicio()).status_code == 201
    for s in ("N001", "N002", "P001"):
        assert post(cliente, totem.emitir(s)).status_code == 201
    r = post(cliente, totem.cancelar("N002"))
    assert r.status_code == 201
    assert r.json()["saldo"] == {"categoria": "convencional", "emitidas": 2,
                                 "canceladas": 1, "em_espera": 1, "versao": 3}

    saldo = cliente.get("/saldos/" + DISP).json()
    assert saldo["saldos"]["convencional"]["em_espera"] == 1
    assert saldo["saldos"]["prioritaria"]["em_espera"] == 1
    assert saldo["em_espera_total"] == 2

    assert cliente.get("/dispositivos/{}/senhas/N001".format(DISP)).status_code == 200
    assert cliente.get("/dispositivos/{}/senhas/N002".format(DISP)).status_code == 404

    with sqlite3.connect(banco) as c:
        assert c.execute("SELECT situacao FROM senhas WHERE codigo='N002'").fetchone()[0] \
            == "cancelada"
    # índice em memória e banco dizem a mesma coisa
    assert cliente.get("/saldos").json() == [
        {"dispositivo": DISP, "categoria": "convencional", "emitidas": 2, "canceladas": 1,
         "em_espera": 1, "atualizado_em": cliente.get("/saldos").json()[0]["atualizado_em"]},
        {"dispositivo": DISP, "categoria": "prioritaria", "emitidas": 1, "canceladas": 0,
         "em_espera": 1, "atualizado_em": cliente.get("/saldos").json()[1]["atualizado_em"]},
    ]


def test_reenvio_e_idempotente(cliente, banco, totem):
    p = totem.emitir("N001")
    assert post(cliente, p).status_code == 201
    r = post(cliente, p)                  # o ESP32 não recebeu a resposta e reenviou
    assert r.status_code == 200 and r.json()["status"] == "duplicado"
    assert contar(banco, "transacoes") == 1
    assert cliente.get("/saldos/" + DISP).json()["saldos"]["convencional"]["emitidas"] == 1
    assert "pacote_duplicado" in eventos(cliente)


def test_cancelamento_sem_emissao_aceito_sem_mexer_no_saldo(cliente, totem):
    post(cliente, totem.emitir("N001"))
    r = post(cliente, totem.cancelar("N009"))
    assert r.status_code == 201                       # 2xx: não trava a fila do ESP32
    assert r.json()["avisos"] == ["cancelamento_sem_emissao"]
    assert "saldo" not in r.json()
    assert cliente.get("/saldos/" + DISP).json()["em_espera_total"] == 1


def test_lacuna_de_seq_e_novo_boot(cliente, totem):
    post(cliente, totem.inicio())
    post(cliente, totem.emitir("N001"))
    totem.seq += 2                                    # 2 pacotes perdidos
    r = post(cliente, totem.emitir("N002"))
    assert "seq_lacuna" in r.json()["avisos"]

    totem.reiniciar()
    r = post(cliente, totem.inicio())
    assert "novo_boot" in r.json()["avisos"]
    assert cliente.get("/saldos/" + DISP).json()["boot"] == 2


def test_reboot_sem_pacote_inicio(cliente, totem):
    post(cliente, totem.inicio())
    post(cliente, totem.emitir("N001"))
    totem.reiniciar()
    totem.seq = 1                                     # inicio do boot novo se perdeu
    r = post(cliente, totem.emitir("N001"))
    assert set(r.json()["avisos"]) >= {"novo_boot", "senha_repetida"}


def test_conciliacao_de_totais_no_status(cliente, totem):
    post(cliente, totem.inicio())
    post(cliente, totem.emitir("N001"))
    r = post(cliente, totem.status(1, 0))
    assert "avisos" not in r.json()                   # bate: sem alerta
    r = post(cliente, totem.status(3, 0, descartados=2))
    assert set(r.json()["avisos"]) == {"divergencia_totais", "perda_no_dispositivo"}
    r = post(cliente, totem.status(3, 0, descartados=2))
    assert "avisos" not in r.json()                   # mesma situação: não repete o log


# ----------------------------------------------------------------- atomicidade
def test_falha_no_meio_faz_rollback_completo(cliente, banco, totem, monkeypatch):
    post(cliente, totem.emitir("N001"))
    svc = cliente.app.state.servico
    antes = {t: contar(banco, t) for t in ("transacoes", "senhas", "dispositivos")}
    saldo_antes = cliente.get("/saldos/" + DISP).json()

    def explode(*a, **k):
        raise RuntimeError("queda simulada depois do INSERT da transação")
    monkeypatch.setattr(svc, "_emitir_senha", explode)

    p = totem.emitir("N002")
    assert post(cliente, p).status_code == 500
    assert {t: contar(banco, t) for t in antes} == antes          # nada gravado
    assert cliente.get("/saldos/" + DISP).json() == saldo_antes   # índices intactos
    assert "falha_gravacao" in eventos(cliente, nivel="erro")

    monkeypatch.undo()
    assert post(cliente, p).status_code == 201    # o reenvio do ESP32 agora entra
    assert cliente.get("/saldos/" + DISP).json()["saldos"]["convencional"]["emitidas"] == 2


def test_banco_recusa_saldo_negativo(banco):
    svc = ServicoTelemetria(banco)
    with pytest.raises(sqlite3.IntegrityError):
        svc._conn.execute("INSERT INTO dispositivos (identificador, primeiro_contato, "
                          "ultimo_contato) VALUES ('x', 'a', 'a')")
        svc._conn.execute("INSERT INTO saldos (dispositivo_id, categoria, emitidas, "
                          "canceladas, em_espera, atualizado_em) VALUES (1, 'convencional', "
                          "0, 1, -1, 'a')")
    svc.fechar()


def test_livro_razao_e_auditoria_sao_imutaveis(cliente, banco, totem):
    post(cliente, totem.emitir("N001"))
    with sqlite3.connect(banco) as c:
        for sql in ("UPDATE transacoes SET seq = 99", "DELETE FROM transacoes",
                    "UPDATE logs_auditoria SET nivel = 'info'", "DELETE FROM logs_auditoria"):
            with pytest.raises(sqlite3.IntegrityError, match="somente-inserção"):
                c.execute(sql)


def test_concorrencia_sem_perder_incrementos(cliente, banco):
    totens = [Totem("esp32-{}".format(i)) for i in range(4)]
    erros = []

    def rodar(t):
        for n in range(1, 26):
            r = post(cliente, t.emitir("N{:03d}".format(n)))
            if r.status_code != 201:
                erros.append(r.status_code)

    threads = [threading.Thread(target=rodar, args=(t,)) for t in totens]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not erros
    for t in totens:
        assert cliente.get("/saldos/" + t.dispositivo).json()["em_espera_total"] == 25
    assert contar(banco, "transacoes") == 100


# ------------------------------------------------------- reinício do servidor
def test_indices_recarregados_do_banco_ao_reiniciar(banco, totem):
    p1 = totem.emitir("N001")
    with TestClient(criar_app(banco)) as c:
        post(c, totem.inicio())
        post(c, p1)
        post(c, totem.emitir("P001"))
    with TestClient(criar_app(banco)) as c:                      # servidor reiniciado
        assert post(c, p1).json()["status"] == "duplicado"        # idempotência persiste
        r = post(c, totem.cancelar("P001"))                       # senha achada no índice
        assert r.json()["saldo"]["em_espera"] == 0
        assert "avisos" not in post(c, totem.status(1, 1)).json()  # contagem do boot ok
        assert c.get("/indices").json()["senhas_ativas"]["itens"] == 1
