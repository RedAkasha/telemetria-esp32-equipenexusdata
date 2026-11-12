# ==============================================================================
# servico.py — Regras de negócio da telemetria do totem
#
# Fluxo de um pacote já validado (modelos.py):
#
#   1. Chave de idempotência -> busca O(1) na TabelaHash. Achou: é um REENVIO do
#      ESP32 (ele reenvia quando não recebe 2xx). Responde 200 sem gravar nada
#      de novo, para o firmware tirar o pacote da fila de contingência.
#   2. BEGIN IMMEDIATE — abre UMA transação SQL que reserva a escrita no banco.
#   3. Dispositivo (busca O(1)), detecção de reboot e de lacuna de seq.
#   4. INSERT no livro-razão (transacoes) + efeito do tipo do pacote:
#        senha_emitida   -> INSERT senhas + saldo += 1 (UPSERT com incremento no SQL)
#        senha_cancelada -> senha ativa por busca O(1) -> UPDATE senhas + saldo -= 1
#        status          -> INSERT telemetria_status + conciliação O(1) dos totais
#   5. INSERT no log de auditoria.
#   6. COMMIT. Qualquer erro em 3–5 -> ROLLBACK: nada do pacote fica gravado.
#   7. Só DEPOIS do COMMIT os índices em memória são atualizados. Assim, um
#      ROLLBACK nunca deixa a TabelaHash dizendo algo que o banco não tem.
#
# Concorrência: o FastAPI atende requisições em várias threads. Um Lock deixa um
# pacote por vez nesta seção (o SQLite também só aceita um escritor por vez), o
# que mantém banco e índices consistentes. Leitores externos (dashboard) usam o
# modo WAL e não bloqueiam a gravação.
# ==============================================================================
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .modelos import PacoteSenhaCancelada, PacoteSenhaEmitida, PacoteStatus
from .tabela_hash import TabelaHash

RAIZ = Path(__file__).resolve().parents[2]
SCHEMA_PADRAO = RAIZ / "database" / "schema.sql"

CATEGORIAS = ("convencional", "prioritaria")
LIMITE_PAYLOAD_AUDITORIA = 2000   # caracteres do corpo rejeitado guardados no log


def agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def chave_idempotencia(pacote) -> str:
    """Identifica um evento de forma única.

    seq sozinho não basta: ele volta a 1 quando o ESP32 reinicia. uptime_ms e o
    timestamp da coleta são gravados no pacote no momento do evento e o reenvio
    manda o mesmo dicionário, então a chave do reenvio é idêntica.
    """
    ts = pacote.timestamp.isoformat() if pacote.timestamp else "-"
    uptime = "-" if pacote.uptime_ms is None else str(pacote.uptime_ms)
    return "{}|{}|{}|{}".format(pacote.dispositivo, pacote.seq, uptime, ts)


class ErroConsistencia(RuntimeError):
    """O banco não está no estado que o índice em memória indicava."""


class ServicoTelemetria:
    def __init__(self, caminho_banco=":memory:", caminho_schema=SCHEMA_PADRAO):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(caminho_banco), check_same_thread=False,
            isolation_level=None)          # autocommit: BEGIN/COMMIT explícitos
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if str(caminho_banco) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(Path(caminho_schema).read_text(encoding="utf-8"))

        # --- Índices em memória (Tabelas Hash, busca O(1)) ---------------------
        # identificador -> {"id", "boot", "ultimo_seq", "emitidas_boot": {cat: n}}
        self.idx_dispositivos = TabelaHash()
        # chave de idempotência -> id da transação
        self.idx_idempotencia = TabelaHash(capacidade_inicial=1024)
        # (dispositivo_id, código da senha) -> id da senha "aguardando" mais recente
        self.idx_senhas_ativas = TabelaHash(capacidade_inicial=256)
        # (dispositivo_id, categoria) -> {"emitidas", "canceladas", "em_espera", "versao"}
        self.idx_saldos = TabelaHash()
        # dispositivo_id -> última anomalia de status registrada (evita log repetido a cada 15 s)
        self.idx_ultimo_alerta = TabelaHash()
        self._carregar_indices()

    def fechar(self):
        self._conn.close()

    # ==========================================================================
    # Carga dos índices a partir do banco (ao iniciar o servidor)
    # ==========================================================================
    def _carregar_indices(self):
        c = self._conn
        for d in c.execute("SELECT id, identificador, boot_atual, ultimo_seq FROM dispositivos"):
            emitidas = dict.fromkeys(CATEGORIAS, 0)
            for linha in c.execute(
                    "SELECT categoria, COUNT(*) AS n FROM transacoes "
                    "WHERE dispositivo_id = ? AND boot = ? AND tipo = 'senha_emitida' "
                    "GROUP BY categoria", (d["id"], d["boot_atual"])):
                emitidas[linha["categoria"]] = linha["n"]
            self.idx_dispositivos[d["identificador"]] = {
                "id": d["id"], "boot": d["boot_atual"], "ultimo_seq": d["ultimo_seq"],
                "emitidas_boot": emitidas}
        for t in c.execute("SELECT id, chave_idempotencia FROM transacoes"):
            self.idx_idempotencia[t["chave_idempotencia"]] = t["id"]
        # ORDER BY id: se houver código repetido, a senha mais recente fica no índice
        for s in c.execute("SELECT id, dispositivo_id, codigo FROM senhas "
                           "WHERE situacao = 'aguardando' ORDER BY id"):
            self.idx_senhas_ativas[(s["dispositivo_id"], s["codigo"])] = s["id"]
        for s in c.execute("SELECT * FROM saldos"):
            self.idx_saldos[(s["dispositivo_id"], s["categoria"])] = self._saldo_de(s)

    @staticmethod
    def _saldo_de(linha):
        return {k: linha[k] for k in ("emitidas", "canceladas", "em_espera", "versao")}

    # ==========================================================================
    # Processamento de um pacote válido
    # ==========================================================================
    def processar(self, pacote, ip_origem=None):
        """Grava o pacote atomicamente. Devolve (status_http, corpo_resposta)."""
        chave = chave_idempotencia(pacote)
        with self._lock:
            # 1. Reenvio? Busca O(1) na tabela hash, sem tocar no banco.
            tid = self.idx_idempotencia.buscar(chave)
            if tid is not None:
                return self._responder_duplicado(pacote, tid, ip_origem)

            recebido_em = agora_utc()
            avisos = []          # (evento, detalhe) -> viram linhas 'aviso' na auditoria
            pos_commit = []      # atualizações dos índices, aplicadas só após o COMMIT
            resposta = {"status": "ok", "seq": pacote.seq}
            c = self._conn
            try:
                c.execute("BEGIN IMMEDIATE")
                disp = self._atualizar_dispositivo(pacote, ip_origem, recebido_em,
                                                   avisos, pos_commit)
                tid = self._inserir_transacao(pacote, chave, disp, recebido_em)
                resposta["transacao_id"] = tid

                if isinstance(pacote, PacoteSenhaEmitida):
                    resposta["saldo"] = self._emitir_senha(
                        pacote, disp, tid, recebido_em, avisos, pos_commit)
                elif isinstance(pacote, PacoteSenhaCancelada):
                    saldo = self._cancelar_senha(pacote, disp, tid, recebido_em,
                                                 avisos, pos_commit)
                    if saldo is not None:
                        resposta["saldo"] = saldo
                elif isinstance(pacote, PacoteStatus):
                    self._registrar_status(pacote, disp, tid, avisos, pos_commit)

                self._auditar(recebido_em, "info", "pacote_aceito", pacote.dispositivo,
                              pacote.seq, tid, ip_origem, pacote.tipo)
                for evento, detalhe in avisos:
                    self._auditar(recebido_em, "aviso", evento, pacote.dispositivo,
                                  pacote.seq, tid, ip_origem, detalhe)
                c.execute("COMMIT")
            except sqlite3.IntegrityError as erro:
                c.execute("ROLLBACK")
                if "chave_idempotencia" in str(erro):
                    # Índice desatualizado (não deveria ocorrer): o UNIQUE do banco
                    # é a garantia final contra duplicidade.
                    tid = c.execute("SELECT id FROM transacoes WHERE chave_idempotencia = ?",
                                    (chave,)).fetchone()["id"]
                    self.idx_idempotencia[chave] = tid
                    return self._responder_duplicado(pacote, tid, ip_origem)
                self._auditar_isolado("erro", "falha_gravacao", pacote.dispositivo,
                                      pacote.seq, ip_origem, str(erro))
                raise
            except Exception as erro:
                c.execute("ROLLBACK")
                self._auditar_isolado("erro", "falha_gravacao", pacote.dispositivo,
                                      pacote.seq, ip_origem, repr(erro))
                raise

            # 7. COMMIT feito: agora sim os índices em memória refletem o banco.
            self.idx_idempotencia[chave] = tid
            for atualizar in pos_commit:
                atualizar()
            if avisos:
                resposta["avisos"] = [evento for evento, _ in avisos]
            return 201, resposta

    def _responder_duplicado(self, pacote, tid, ip_origem):
        self._auditar_isolado("info", "pacote_duplicado", pacote.dispositivo, pacote.seq,
                              ip_origem, "reenvio da transação {}".format(tid), tid)
        return 200, {"status": "duplicado", "seq": pacote.seq, "transacao_id": tid}

    # --------------------------------------------------------------------------
    def _atualizar_dispositivo(self, p, ip, agora, avisos, pos_commit):
        """Cadastra ou atualiza o totem; detecta reboot e pacotes perdidos."""
        c = self._conn
        atual = self.idx_dispositivos.buscar(p.dispositivo)      # O(1)
        ip_novo = getattr(p, "ip", None) or ip
        if atual is None:
            cur = c.execute(
                "INSERT INTO dispositivos (identificador, ip, boot_atual, ultimo_seq, "
                "primeiro_contato, ultimo_contato) VALUES (?, ?, 1, ?, ?, ?)",
                (p.dispositivo, ip_novo, p.seq, agora, agora))
            novo = {"id": cur.lastrowid, "boot": 1, "ultimo_seq": p.seq,
                    "emitidas_boot": dict.fromkeys(CATEGORIAS, 0)}
            avisos.append(("dispositivo_novo", "primeiro contato de " + p.dispositivo))
            if p.tipo != "inicio" and p.seq > 1:
                avisos.append(("seq_lacuna", "{} pacote(s) anteriores não recebidos".format(
                    p.seq - 1)))
        else:
            novo = dict(atual, emitidas_boot=dict(atual["emitidas_boot"]))
            reiniciou = (p.tipo == "inicio" and atual["ultimo_seq"] > 0) \
                or p.seq <= atual["ultimo_seq"]
            if reiniciou:
                novo["boot"] += 1
                novo["emitidas_boot"] = dict.fromkeys(CATEGORIAS, 0)
                motivo = "pacote inicio" if p.tipo == "inicio" else \
                    "seq voltou de {} para {} sem pacote inicio".format(atual["ultimo_seq"], p.seq)
                avisos.append(("novo_boot", "boot {} ({})".format(novo["boot"], motivo)))
                anterior = 0
            else:
                anterior = atual["ultimo_seq"]
            if p.seq > anterior + 1:
                avisos.append(("seq_lacuna", "{} pacote(s) faltando entre seq {} e {}".format(
                    p.seq - anterior - 1, anterior, p.seq)))
            novo["ultimo_seq"] = p.seq
            c.execute(
                "UPDATE dispositivos SET boot_atual = ?, ultimo_seq = ?, ultimo_contato = ?, "
                "ip = COALESCE(?, ip) WHERE id = ?",
                (novo["boot"], novo["ultimo_seq"], agora, ip_novo, novo["id"]))
        pos_commit.append(lambda: self.idx_dispositivos.inserir(p.dispositivo, novo))
        return novo

    def _inserir_transacao(self, p, chave, disp, recebido_em):
        bruto = p.model_dump(mode="json", exclude_none=False)
        cur = self._conn.execute(
            "INSERT INTO transacoes (chave_idempotencia, dispositivo_id, boot, seq, tipo, "
            "senha, categoria, coletado_em, uptime_ms, recebido_em, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chave, disp["id"], disp["boot"], p.seq, p.tipo,
             getattr(p, "senha", None), getattr(p, "categoria", None),
             bruto.get("timestamp"), p.uptime_ms, recebido_em,
             json.dumps(bruto, ensure_ascii=False)))
        return cur.lastrowid

    def _emitir_senha(self, p, disp, tid, agora, avisos, pos_commit):
        c = self._conn
        chave_senha = (disp["id"], p.senha)
        if self.idx_senhas_ativas.contem(chave_senha):                     # O(1)
            avisos.append(("senha_repetida", "{} já estava aguardando; o índice passa a "
                                             "apontar para a emissão nova".format(p.senha)))
        senha_id = c.execute(
            "INSERT INTO senhas (dispositivo_id, boot, codigo, categoria, "
            "transacao_emissao_id, emitida_em) VALUES (?, ?, ?, ?, ?, ?)",
            (disp["id"], disp["boot"], p.senha, p.categoria, tid,
             p.timestamp.isoformat() if p.timestamp else agora)).lastrowid
        # Incremento feito PELO BANCO, na mesma transação (sem ler-somar-gravar no Python)
        linha = c.execute(
            "INSERT INTO saldos (dispositivo_id, categoria, emitidas, canceladas, em_espera, "
            "versao, atualizado_em) VALUES (?, ?, 1, 0, 1, 1, ?) "
            "ON CONFLICT (dispositivo_id, categoria) DO UPDATE SET "
            "  emitidas = emitidas + 1, em_espera = em_espera + 1, "
            "  versao = versao + 1, atualizado_em = excluded.atualizado_em "
            "RETURNING emitidas, canceladas, em_espera, versao",
            (disp["id"], p.categoria, agora)).fetchone()
        saldo = self._saldo_de(linha)
        disp["emitidas_boot"][p.categoria] += 1   # `disp` é cópia; vai ao índice após o COMMIT
        pos_commit.append(lambda: self.idx_senhas_ativas.inserir(chave_senha, senha_id))
        pos_commit.append(lambda: self.idx_saldos.inserir((disp["id"], p.categoria), saldo))
        return dict(saldo, categoria=p.categoria)

    def _cancelar_senha(self, p, disp, tid, agora, avisos, pos_commit):
        c = self._conn
        chave_senha = (disp["id"], p.senha)
        senha_id = self.idx_senhas_ativas.buscar(chave_senha)               # O(1)
        if senha_id is None:
            # A emissão nunca chegou (ex.: descartada na fila cheia do ESP32). O
            # cancelamento aconteceu de verdade no totem, então é aceito (2xx, para
            # não travar a fila do firmware), mas não mexe no saldo.
            avisos.append(("cancelamento_sem_emissao",
                           "{} não está aguardando; saldo inalterado".format(p.senha)))
            return None
        linha = c.execute(
            "UPDATE senhas SET situacao = 'cancelada', transacao_cancelamento_id = ?, "
            "cancelada_em = ? WHERE id = ? AND situacao = 'aguardando' RETURNING categoria",
            (tid, p.timestamp.isoformat() if p.timestamp else agora, senha_id)).fetchone()
        if linha is None:
            raise ErroConsistencia("senha {} (id {}) não está aguardando no banco".format(
                p.senha, senha_id))
        categoria = linha["categoria"]
        saldo_linha = c.execute(
            "UPDATE saldos SET canceladas = canceladas + 1, em_espera = em_espera - 1, "
            "versao = versao + 1, atualizado_em = ? "
            "WHERE dispositivo_id = ? AND categoria = ? "
            "RETURNING emitidas, canceladas, em_espera, versao",
            (agora, disp["id"], categoria)).fetchone()
        if saldo_linha is None:
            raise ErroConsistencia("saldo inexistente para {} / {}".format(disp["id"], categoria))
        saldo = self._saldo_de(saldo_linha)
        # Se o mesmo código tinha uma emissão anterior ainda aguardando, ela volta ao índice
        restante = c.execute(
            "SELECT id FROM senhas WHERE dispositivo_id = ? AND codigo = ? "
            "AND situacao = 'aguardando' ORDER BY id DESC LIMIT 1",
            (disp["id"], p.senha)).fetchone()

        def atualizar_indices():
            if restante is None:
                self.idx_senhas_ativas.remover(chave_senha)
            else:
                self.idx_senhas_ativas.inserir(chave_senha, restante["id"])
            self.idx_saldos.inserir((disp["id"], categoria), saldo)
        pos_commit.append(atualizar_indices)
        return dict(saldo, categoria=categoria)

    def _registrar_status(self, p, disp, tid, avisos, pos_commit):
        self._conn.execute(
            "INSERT INTO telemetria_status (transacao_id, total_convencional, total_prioritaria, "
            "canceladas, intervalo_medio_s, fila_pendentes, descartados, rssi_dbm, mem_livre) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, p.total_convencional, p.total_prioritaria, p.canceladas,
             p.intervalo_medio_s, p.fila_pendentes, p.descartados, p.rssi_dbm, p.mem_livre))

        # Conciliação O(1): contadores do totem x senhas recebidas neste boot
        recebidas = disp["emitidas_boot"]
        diferenca = (p.total_convencional - recebidas["convencional"],
                     p.total_prioritaria - recebidas["prioritaria"])
        alerta = (diferenca, p.descartados or 0)
        if alerta == self.idx_ultimo_alerta.buscar(disp["id"]):
            return               # mesma situação do último status: não repete o log
        if diferenca != (0, 0):
            avisos.append(("divergencia_totais",
                           "totem emitiu N={} P={}, servidor recebeu N={} P={}".format(
                               p.total_convencional, p.total_prioritaria,
                               recebidas["convencional"], recebidas["prioritaria"])))
        if p.descartados:
            avisos.append(("perda_no_dispositivo",
                           "{} pacote(s) descartados pela fila cheia do ESP32".format(
                               p.descartados)))
        pos_commit.append(lambda: self.idx_ultimo_alerta.inserir(disp["id"], alerta))

    # ==========================================================================
    # Auditoria
    # ==========================================================================
    def _auditar(self, quando, nivel, evento, dispositivo=None, seq=None,
                 transacao_id=None, ip=None, detalhe=None, payload=None):
        self._conn.execute(
            "INSERT INTO logs_auditoria (ocorrido_em, nivel, evento, dispositivo, seq, "
            "transacao_id, ip_origem, detalhe, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (quando, nivel, evento, dispositivo, seq, transacao_id, ip, detalhe,
             payload[:LIMITE_PAYLOAD_AUDITORIA] if payload else None))

    def _auditar_isolado(self, nivel, evento, dispositivo=None, seq=None, ip=None,
                         detalhe=None, transacao_id=None, payload=None):
        """Log fora de uma transação de negócio (um INSERT em autocommit é atômico)."""
        self._auditar(agora_utc(), nivel, evento, dispositivo, seq, transacao_id, ip,
                      detalhe, payload)

    def registrar_rejeicao(self, evento, detalhe, corpo_bruto, ip=None):
        """Pacote que não passou na validação: só a auditoria registra."""
        dispositivo = seq = None
        try:
            dados = json.loads(corpo_bruto)
            if isinstance(dados, dict):
                d, s = dados.get("dispositivo"), dados.get("seq")
                dispositivo = d[:64] if isinstance(d, str) else None
                seq = s if isinstance(s, int) and not isinstance(s, bool) else None
        except (ValueError, TypeError):
            pass
        texto = corpo_bruto.decode("utf-8", "replace") if isinstance(corpo_bruto, bytes) \
            else str(corpo_bruto)
        with self._lock:
            self._auditar_isolado("erro", evento, dispositivo, seq, ip, detalhe, payload=texto)

    # ==========================================================================
    # Consultas
    # ==========================================================================
    def _consultar(self, sql, params=()):
        with self._lock:
            return [dict(l) for l in self._conn.execute(sql, params)]

    def saldo_do_dispositivo(self, identificador):
        """Duas buscas O(1): dispositivo -> id, depois (id, categoria) -> saldo."""
        with self._lock:
            disp = self.idx_dispositivos.buscar(identificador)
            if disp is None:
                return None
            vazio = {"emitidas": 0, "canceladas": 0, "em_espera": 0, "versao": 0}
            por_categoria = {cat: dict(self.idx_saldos.buscar((disp["id"], cat), vazio))
                             for cat in CATEGORIAS}
            return {"dispositivo": identificador, "boot": disp["boot"],
                    "ultimo_seq": disp["ultimo_seq"], "saldos": por_categoria,
                    "em_espera_total": sum(s["em_espera"] for s in por_categoria.values())}

    def senha_ativa(self, identificador, codigo):
        """Busca O(1) da senha aguardando + leitura da linha pela chave primária."""
        with self._lock:
            disp = self.idx_dispositivos.buscar(identificador)
            if disp is None:
                return None
            senha_id = self.idx_senhas_ativas.buscar((disp["id"], codigo))
            if senha_id is None:
                return None
            linha = self._conn.execute("SELECT * FROM senhas WHERE id = ?", (senha_id,)).fetchone()
            return dict(linha) if linha else None

    def saldos(self):
        return self._consultar("SELECT * FROM vw_saldos ORDER BY dispositivo, categoria")

    def dispositivos(self):
        return self._consultar("SELECT * FROM dispositivos ORDER BY identificador")

    def transacoes(self, limite=50, dispositivo=None, tipo=None):
        sql = ("SELECT t.id, d.identificador AS dispositivo, t.boot, t.seq, t.tipo, t.senha, "
               "t.categoria, t.coletado_em, t.recebido_em FROM transacoes t "
               "JOIN dispositivos d ON d.id = t.dispositivo_id WHERE 1 = 1")
        params = []
        if dispositivo:
            sql += " AND d.identificador = ?"
            params.append(dispositivo)
        if tipo:
            sql += " AND t.tipo = ?"
            params.append(tipo)
        sql += " ORDER BY t.id DESC LIMIT ?"
        params.append(limite)
        return self._consultar(sql, params)

    def auditoria(self, limite=50, nivel=None, evento=None):
        sql = ("SELECT id, ocorrido_em, nivel, evento, dispositivo, seq, transacao_id, "
               "ip_origem, detalhe FROM logs_auditoria WHERE 1 = 1")
        params = []
        if nivel:
            sql += " AND nivel = ?"
            params.append(nivel)
        if evento:
            sql += " AND evento = ?"
            params.append(evento)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limite)
        return self._consultar(sql, params)

    def resumo(self):
        with self._lock:
            c = self._conn
            return {
                "transacoes": c.execute("SELECT COUNT(*) FROM transacoes").fetchone()[0],
                "dispositivos": len(self.idx_dispositivos),
                "senhas_aguardando": len(self.idx_senhas_ativas),
                "logs_auditoria": c.execute("SELECT COUNT(*) FROM logs_auditoria").fetchone()[0],
            }

    def estatisticas_indices(self):
        with self._lock:
            tabelas = {
                "dispositivos": self.idx_dispositivos,
                "idempotencia": self.idx_idempotencia,
                "senhas_ativas": self.idx_senhas_ativas,
                "saldos": self.idx_saldos,
            }
            return {nome: {"itens": len(t), "capacidade": t.capacidade,
                           "fator_carga": round(t.fator_carga, 3), "colisoes": t.colisoes,
                           "maior_balde": t.maior_balde(), "rehashes": t.rehashes}
                    for nome, t in tabelas.items()}
