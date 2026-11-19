# ==============================================================================
# test_fluxo_completo.py — Teste END-TO-END do Totem ByteBank Queue
#
#   botão do ESP32 -> firmware -> Wi-Fi/HTTP -> API -> SQLite -> dashboard
#   dashboard -> API -> fila de comandos -> ESP32 -> confirmação -> SQLite -> dashboard
#
# Tudo real, nada "mockado" no caminho dos dados:
#   - API: uvicorn num processo separado, banco SQLite em arquivo;
#   - ESP32: firmware/boot.py + main.py executados como estão (simulador/), só o
#     hardware (botões, buzzer, Wi-Fi) é simulado;
#   - Dashboard: dashboard/app.py rodando no Streamlit (AppTest), com os
#     comandos enviados pelos BOTÕES do próprio painel.
#
# Os testes rodam em ordem e contam uma história: uso normal, comandos, queda do
# servidor, rede lenta, totem desligado e, no fim, uma auditoria de consistência.
# "Sem travamentos": o simulador mede a maior pausa do loop do firmware e cada
# espera do teste tem prazo (um travamento vira falha, nunca um teste parado).
#
#   pip install -r backend/requirements.txt -r dashboard/requirements.txt
#   python -m pytest e2e -v
# ==============================================================================
import os
import sqlite3
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import api_cliente
from simulador import ESP32Virtual

RAIZ = Path(__file__).resolve().parents[1]
DISP = "esp32-e2e-01"
TIMEOUT_HTTP_S = 1          # timeout do urequests no teste (na placa: 3 s)


class Bancada:
    """API + ESP32 virtual + dashboard, e utilidades para observar os três."""

    def __init__(self, api, pasta):
        self.api, self.pasta = api, pasta
        self.esp = None
        os.environ.update(NEXUS_DB=str(api.banco), NEXUS_API=api.url, NEXUS_TZ="America/Recife")
        self.painel = AppTest.from_file(str(RAIZ / "dashboard" / "app.py"), default_timeout=30)
        self.esperadas = []        # (sessão, senha) que o banco tem de ter, em ordem

    # ---------------------------------------------------------------- totem
    def ligar_totem(self):
        self.esp = ESP32Virtual(self.api.url, DISP, pasta_estado=self.pasta,
                                INTERVALO_STATUS_MS=3000, TIMEOUT_HTTP_S=TIMEOUT_HTTP_S)
        self.esp.ligar()
        return self.esp

    def desligar_totem(self):
        self.esp.desligar()
        assert self.esp.erro is None, self.esp.erro

    def emitir(self, botao, sessao, senha):
        self.esp.tocar(botao)
        self.esperadas.append((sessao, senha))

    # ---------------------------------------------------------------- banco
    def sql(self, consulta, params=()):
        with sqlite3.connect("file:{}?mode=ro".format(self.api.banco), uri=True) as c:
            return c.execute(consulta, params).fetchall()

    def aguardar(self, condicao, descricao, timeout=15):
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if self.esp and self.esp.erro:
                raise AssertionError("firmware parou: {!r}".format(self.esp.erro))
            if condicao():
                return
            time.sleep(0.1)
        raise AssertionError("tempo esgotado esperando: " + descricao)

    def situacao_comando(self, cid):
        r = self.sql("SELECT situacao FROM comandos WHERE id = ?", (cid,))
        return r[0][0] if r else None

    def aguardar_comando(self, cid, situacao="executado", timeout=20):
        self.aguardar(lambda: self.situacao_comando(cid) == situacao,
                      "comando #{} {}".format(cid, situacao), timeout)

    def ultimo_comando_id(self):
        return self.sql("SELECT MAX(id) FROM comandos")[0][0]

    def senhas_no_banco(self):
        return self.sql("SELECT boot, senha FROM transacoes WHERE tipo = 'senha_emitida' "
                        "ORDER BY id")

    def eventos(self, nivel=None):
        sql = "SELECT evento FROM logs_auditoria"
        if nivel:
            return [e for (e,) in self.sql(sql + " WHERE nivel = ?", (nivel,))]
        return [e for (e,) in self.sql(sql)]

    # ---------------------------------------------------------------- painel
    def ver_painel(self):
        self.painel.run()
        assert not self.painel.exception, [e.value for e in self.painel.exception]
        return self.painel

    def clicar(self, chave):
        """Clica num botão de comando do dashboard e devolve o id criado."""
        antes = self.ultimo_comando_id()
        self.painel.button(key=chave).click().run()
        assert not self.painel.exception, [e.value for e in self.painel.exception]
        assert not self.painel.error, [e.value for e in self.painel.error]
        cid = self.ultimo_comando_id()
        assert cid is not None and cid != antes, "o dashboard não criou o comando"
        return cid

    def metrica(self, rotulo):
        for m in self.painel.metric:
            if m.label == rotulo:
                return str(m.value)
        raise KeyError(rotulo)

    def textos_do_painel(self):
        partes = [str(e.value) for e in self.painel.info]
        partes += [d.value.to_string() for d in self.painel.dataframe]
        return "\n".join(partes)


@pytest.fixture(scope="module")
def bancada(api, pasta):
    b = Bancada(api, pasta)
    b.ligar_totem()
    yield b
    if b.esp and b.esp._thread and b.esp._thread.is_alive():
        b.desligar_totem()


# ============================================================================ 1
def test_01_totem_liga_e_aparece_online_no_dashboard(bancada):
    b = bancada
    b.aguardar(lambda: b.sql("SELECT COUNT(*) FROM telemetria_status")[0][0] >= 1,
               "primeiro pacote de status")
    painel = b.ver_painel()
    assert any(DISP in str(s.value) for s in painel.subheader)
    assert any(":green-badge[" in str(m.value) and "online" in str(m.value)
               for m in painel.markdown)                                # st.badge
    assert b.metrica("Trava") == "🔓 Liberado"
    assert b.metrica("Sessão / seq").startswith("1 /")
    assert b.metrica("Memória livre").endswith("KB")


# ============================================================================ 2
def test_02_senhas_do_botao_chegam_ao_banco_e_ao_dashboard(bancada):
    b = bancada
    b.emitir("convencional", 1, "N001")
    b.emitir("convencional", 1, "N002")
    b.emitir("prioritaria", 1, "P001")
    b.esp.segurar("prioritaria")                      # segurar 2 s cancela a última (P001)

    b.aguardar(lambda: b.sql("SELECT COUNT(*) FROM senhas WHERE situacao = 'cancelada'")[0][0]
               == 1, "cancelamento de P001")
    assert b.senhas_no_banco() == b.esperadas
    saldos = {cat: (e, c, w) for cat, e, c, w in b.sql(
        "SELECT categoria, emitidas, canceladas, em_espera FROM vw_saldos")}
    assert saldos == {"convencional": (2, 0, 2), "prioritaria": (1, 1, 0)}
    assert b.esp.tela[0] == "N002"                    # pilha: o topo voltou a N002

    b.ver_painel()
    assert b.metrica("Em espera — convencional") == "2"
    assert b.metrica("Em espera — prioritária") == "0"
    assert b.metrica("Emitidas (N + P)") == "3"
    assert b.metrica("Canceladas") == "1"

    # o status seguinte bate com o banco: nenhuma divergência registrada
    b.aguardar(lambda: b.sql("SELECT MAX(total_convencional) FROM telemetria_status")[0][0]
               == 2, "status com os totais novos")
    assert "divergencia_totais" not in b.eventos()


# ============================================================================ 3
def test_03_dashboard_aciona_o_alarme_do_totem(bancada):
    b = bancada
    b.ver_painel()
    b.painel.number_input(key="duracao_alarme").set_value(4)
    t0 = b.esp._ticks_sem_pausa()
    cid = b.clicar("cmd_acionar_alarme")

    b.esp.aguardar(lambda: b.esp.alarme, 8, "sirene ligada")
    assert b.esp.tela[0] == "ALARME"
    b.aguardar_comando(cid)
    b.ver_painel()
    assert b.metrica("Alarme") == "🚨 Ativo"
    assert "executado" in b.textos_do_painel()

    b.esp.aguardar(lambda: not b.esp.alarme, 8, "sirene desligar sozinha")
    tons = {f for t, f, ligado in b.esp.buzzer if t >= t0 and ligado}
    assert {1800, 2600} <= tons                       # sirene alternando dois tons
    assert b.esp.buzzer[-1][2] is False               # e terminou em silêncio
    estado = b.sql("SELECT estado_totem FROM comandos WHERE id = ?", (cid,))[0][0]
    assert '"alarme": true' in estado


# ============================================================================ 4
def test_04_trava_bloqueia_os_botoes_e_liberar_devolve(bancada):
    b = bancada
    cid = b.clicar("cmd_travar")
    b.aguardar_comando(cid)
    assert b.esp.travado and b.esp.tela[0] == "TRAVADO"
    assert (b.pasta / "trava.txt").read_text() == "1"         # gravado na "flash"

    antes = len(b.senhas_no_banco())
    b.esp.tocar("convencional")                                # cliente tenta: recusado
    time.sleep(0.5)
    assert b.esp.estado["convencional"] == 2
    assert b.esp.estado["mensagem"] == "Totem travado"
    assert len(b.senhas_no_banco()) == antes
    b.ver_painel()
    assert b.metrica("Trava") == "🔒 Travado"

    cid = b.clicar("cmd_liberar_trava")
    b.aguardar_comando(cid)
    assert not b.esp.travado
    b.emitir("convencional", 1, "N003")
    b.aguardar(lambda: b.senhas_no_banco() == b.esperadas, "N003 depois de liberar")
    b.ver_painel()
    assert b.metrica("Trava") == "🔓 Liberado"


# ============================================================================ 5
def test_05_reset_de_contador_abre_sessao_nova(bancada):
    b = bancada
    b.ver_painel()
    b.painel.checkbox(key="confirma_reset").check().run()
    cid = b.clicar("cmd_resetar_contador")
    b.aguardar_comando(cid)
    assert b.esp.estado["convencional"] == 0 and b.esp.estado["prioritaria"] == 0

    b.emitir("convencional", 2, "N001")                     # numeração recomeça
    b.aguardar(lambda: b.senhas_no_banco() == b.esperadas, "N001 da sessão 2")
    assert "contador_resetado" in b.eventos("aviso")
    b.aguardar(lambda: b.sql(
        "SELECT ts.total_convencional FROM telemetria_status ts JOIN transacoes t "
        "ON t.id = ts.transacao_id WHERE t.boot = 2 ORDER BY t.id DESC LIMIT 1") == [(1,)],
        "status da sessão 2")
    assert "divergencia_totais" not in b.eventos()          # sessão nova concilia
    b.ver_painel()
    assert b.metrica("Sessão / seq").startswith("2 /")


# ============================================================================ 6
def test_06_api_fora_do_ar_nada_se_perde_nem_trava(bancada):
    b = bancada
    b.api.parar()
    b.esp.zerar_pausas()
    for senha in ("N002", "N003", "N004"):
        b.emitir("convencional", 2, senha)
        time.sleep(0.3)
    b.esp.aguardar(lambda: b.esp.pendentes() >= 3, 5, "pacotes guardados na fila")
    assert b.esp.tela[2].endswith("F:{}".format(b.esp.pendentes()))   # display mostra a fila

    # O dashboard continua mostrando o banco; o comando avisa que a API caiu
    b.ver_painel()
    b.painel.button(key="cmd_travar").click().run()
    assert not b.painel.exception
    assert any("API fora do ar" in str(e.value) for e in b.painel.error)

    b.api.iniciar()
    b.aguardar(lambda: b.esp.pendentes() == 0, "fila de contingência esvaziar", 20)
    b.aguardar(lambda: b.senhas_no_banco() == b.esperadas, "senhas guardadas chegarem")
    assert b.esp.maior_pausa_ms < 1000, b.esp.maior_pausa_ms
    seqs = [s for (s,) in b.sql("SELECT seq FROM transacoes WHERE boot = 2 ORDER BY id")]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)     # ordem original


# ============================================================================ 7
def test_07_rede_inalcancavel_loop_nunca_trava_mais_que_um_timeout(bancada):
    b = bancada
    b.esp.rede_fora = True                      # cada requisição espera o timeout e falha
    inicio = len(b.esp.requisicoes)
    b.esp.zerar_pausas()
    for senha in ("N005", "N006", "N007"):
        b.emitir("convencional", 2, senha)
        time.sleep(TIMEOUT_HTTP_S + 0.3)
    time.sleep(4)
    consultas = [r for r in b.esp.requisicoes[inicio:] if "comandos/proximo" in r[1]]
    b.esp.rede_fora = False

    assert b.esp.estado["convencional"] == 7               # todos os toques contaram
    b.aguardar(lambda: b.esp.pendentes() == 0, "fila esvaziar após a rede voltar", 20)
    b.aguardar(lambda: b.senhas_no_banco() == b.esperadas, "N005–N007 no banco")
    # Pior caso de uma volta do loop = UM timeout (+ folga), nunca vários somados
    assert b.esp.maior_pausa_ms < TIMEOUT_HTTP_S * 1000 + 500, b.esp.maior_pausa_ms
    # Com pacotes presos na fila, o totem não insiste na consulta de comandos
    assert len(consultas) <= 1, consultas


# ============================================================================ 8
def test_08_toque_durante_requisicao_lenta_nao_se_perde(bancada):
    b = bancada
    # Ruído de 5 ms na linha com o loop rodando normal: não vira senha fantasma
    b.esp.aguardar(lambda: not b.esp.em_requisicao, 5, "loop livre")
    b.esp.apertar("convencional")
    time.sleep(0.005)
    b.esp.soltar("convencional")
    time.sleep(0.4)
    assert b.esp.estado["convencional"] == 7

    b.esp.latencia_s = 1.0                       # Wi-Fi ruim: cada requisição leva 1 s
    try:
        def requisicao_lenta_comecando():
            atual = b.esp.requisicao_desde          # (início, latência) ou None
            return atual is not None and atual[1] == 1.0 and \
                time.monotonic() - atual[0] < 0.3
        b.esp.aguardar(requisicao_lenta_comecando, 10, "uma requisição lenta começando")
        b.esp.apertar("convencional")            # toque inteiro com o loop bloqueado
        time.sleep(0.08)
        bloqueado_o_tempo_todo = b.esp.em_requisicao
        b.esp.soltar("convencional")
        b.esperadas.append((2, "N008"))
    finally:
        b.esp.latencia_s = 0.02
    assert bloqueado_o_tempo_todo
    b.aguardar(lambda: b.senhas_no_banco() == b.esperadas, "N008 (capturada pela IRQ)")


# ============================================================================ 9
def test_09_totem_desligado_comando_expira_e_trava_sobrevive_ao_reboot(bancada):
    b = bancada
    b.ver_painel()
    cid = b.clicar("cmd_travar")
    b.aguardar_comando(cid, timeout=40)          # o recuo da consulta pode estar alto
    b.desligar_totem()

    ok, cmd = api_cliente.enviar_comando(b.api.url, DISP, "acionar_alarme", 5, validade_s=5)
    assert ok, cmd
    b.aguardar_comando(cmd["id"], "expirado", 15)     # a API expira sem o totem consultar
    b.ver_painel()
    assert "expirado" in b.textos_do_painel()

    esp = b.ligar_totem()                              # reboot: mesma "flash"
    assert esp.travado and esp.tela[0] == "TRAVADO"    # trava lida do arquivo no boot
    time.sleep(5)                                       # ~2 consultas de comando
    assert not esp.alarme                               # o alarme expirado não toca
    assert b.situacao_comando(cmd["id"]) == "expirado"
    assert "novo_boot" in b.eventos("aviso")

    b.ver_painel()
    cid = b.clicar("cmd_liberar_trava")
    b.aguardar_comando(cid)
    assert not esp.travado


# =========================================================================== 10
def test_10_auditoria_final_de_consistencia(bancada):
    b = bancada
    b.aguardar(lambda: b.esp.pendentes() == 0, "fila vazia")
    assert b.senhas_no_banco() == b.esperadas           # nenhuma senha perdida/duplicada

    # Banco: saldo da fila = senhas que estão de fato aguardando
    em_espera = b.sql("SELECT SUM(em_espera) FROM saldos")[0][0]
    aguardando = b.sql("SELECT COUNT(*) FROM senhas WHERE situacao = 'aguardando'")[0][0]
    assert em_espera == aguardando == len(b.esperadas) - 1

    # Comandos: todos terminaram; nenhum ficou pendurado
    abertos = b.sql("SELECT id, situacao FROM comandos WHERE situacao IN "
                    "('pendente', 'entregue', 'sem_resposta')")
    assert abertos == []

    # Auditoria: nenhum erro e só os avisos esperados pela história do teste
    assert b.eventos("erro") == []
    esperados = {"dispositivo_novo", "contador_resetado", "novo_boot", "comando_expirado",
                 "senha_repetida"}           # N001.. de novo após o reset (documentado)
    assert set(b.eventos("aviso")) <= esperados, set(b.eventos("aviso")) - esperados
    for evento in ("seq_lacuna", "divergencia_totais", "perda_no_dispositivo"):
        assert evento not in b.eventos()

    # Firmware: nada descartado, nada recusado, nenhum erro
    fila = b.esp.globais["fila_envio"]
    assert fila.descartados == 0 and b.esp.estado["rejeitados"] == 0
    assert not any("Traceback" in linha or "ERRO" in linha for linha in b.esp.log)

    # Dashboard ainda de pé, mostrando o mesmo saldo do banco
    b.ver_painel()
    assert int(b.metrica("Em espera — convencional")) + \
        int(b.metrica("Em espera — prioritária")) == em_espera
    b.desligar_totem()
