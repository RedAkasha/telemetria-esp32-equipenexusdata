# ==============================================================================
# dados.py — Consultas do dashboard ao banco SQLite (somente leitura)
#
# O dashboard lê o MESMO arquivo que a API grava (backend/nexus.db). A conexão
# é aberta em modo somente-leitura (mode=ro): o dashboard nunca altera o banco.
# Com o modo WAL ligado pela API, a leitura não bloqueia a gravação dos pacotes.
# Cada consulta abre e fecha a sua conexão: nada fica preso entre atualizações.
# ==============================================================================
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ONLINE_ATE_S = 40          # o totem manda status a cada 15 s: 40 s sem nada = offline


def conectar(caminho):
    uri = "file:{}?mode=ro".format(Path(caminho).resolve().as_posix())
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def consultar(caminho, sql, params=()):
    """Executa um SELECT e devolve um DataFrame."""
    conn = conectar(caminho)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def linha(caminho, sql, params=()):
    conn = conectar(caminho)
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def banco_pronto(caminho):
    """True se o arquivo existe e já tem as tabelas da API (inclusive comandos)."""
    if not Path(caminho).is_file():
        return False
    try:
        r = linha(caminho, "SELECT COUNT(*) AS n FROM sqlite_master "
                           "WHERE type = 'table' AND name IN ('transacoes', 'comandos')")
        return r["n"] == 2
    except sqlite3.Error:
        return False


def instante(texto):
    """ISO 8601 do banco -> datetime com fuso (UTC). NULL (None/NaN do pandas) -> None."""
    if not isinstance(texto, str) or not texto:
        return None
    d = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def segundos_desde(texto, agora=None):
    d = instante(texto)
    if d is None:
        return None
    return ((agora or datetime.now(timezone.utc)) - d).total_seconds()


def para_local(serie, fuso):
    """Coluna de texto ISO -> datetime no fuso do usuário (sem fuso, para os gráficos)."""
    return pd.to_datetime(serie, utc=True, format="ISO8601").dt.tz_convert(fuso).dt.tz_localize(None)


# ------------------------------------------------------------------- totens
def dispositivos(caminho):
    return consultar(caminho, "SELECT identificador, ip, boot_atual, ultimo_seq, "
                              "primeiro_contato, ultimo_contato FROM dispositivos "
                              "ORDER BY ultimo_contato DESC")


def dispositivo(caminho, ident):
    return linha(caminho, "SELECT * FROM dispositivos WHERE identificador = ?", (ident,))


def ultimo_status(caminho, ident):
    """Último pacote status do totem, com os campos do JSON original."""
    r = linha(caminho,
              "SELECT ts.*, t.recebido_em, t.coletado_em, t.payload FROM telemetria_status ts "
              "JOIN transacoes t ON t.id = ts.transacao_id "
              "JOIN dispositivos d ON d.id = t.dispositivo_id "
              "WHERE d.identificador = ? ORDER BY t.id DESC LIMIT 1", (ident,))
    if r is None:
        return None
    payload = json.loads(r.pop("payload"))
    r["historico"] = payload.get("historico") or []
    r["travado"] = payload.get("travado")
    r["alarme"] = payload.get("alarme")
    return r


def estado_atuadores(caminho, ident):
    """Trava e alarme: vale a informação mais nova entre o último status e a
    última confirmação de comando (que traz o retrato do totem)."""
    st = ultimo_status(caminho, ident)
    cmd = linha(caminho,
                "SELECT id, tipo, parametros, concluido_em, estado_totem FROM vw_comandos "
                "WHERE dispositivo = ? AND situacao = 'executado' AND estado_totem IS NOT NULL "
                "ORDER BY concluido_em DESC LIMIT 1", (ident,))
    candidatos = []
    if st and st.get("travado") is not None:
        candidatos.append((instante(st["recebido_em"]), {
            "travado": st["travado"], "alarme": bool(st.get("alarme")),
            "fonte": "status", "quando": st["recebido_em"]}))
    if cmd:
        estado = json.loads(cmd["estado_totem"])
        alarme = bool(estado.get("alarme"))
        if alarme and cmd["tipo"] == "acionar_alarme":
            # a confirmação é do início do alarme: depois da duração ele já parou
            dur = json.loads(cmd["parametros"]).get("duracao_s", 10)
            alarme = segundos_desde(cmd["concluido_em"]) < dur
        candidatos.append((instante(cmd["concluido_em"]), {
            "travado": bool(estado.get("travado")), "alarme": alarme,
            "fonte": "comando #{}".format(cmd["id"]), "quando": cmd["concluido_em"]}))
    if not candidatos:
        return {"travado": None, "alarme": None, "fonte": None, "quando": None}
    return max(candidatos, key=lambda c: c[0])[1]


# ----------------------------------------------------------- fila e senhas
def saldos(caminho, ident):
    return consultar(caminho, "SELECT categoria, emitidas, canceladas, em_espera, atualizado_em "
                              "FROM vw_saldos WHERE dispositivo = ? ORDER BY categoria", (ident,))


def senhas_aguardando(caminho, ident, limite=20):
    return consultar(caminho,
                     "SELECT s.codigo AS senha, s.categoria, s.boot AS sessao, s.emitida_em "
                     "FROM senhas s JOIN dispositivos d ON d.id = s.dispositivo_id "
                     "WHERE d.identificador = ? AND s.situacao = 'aguardando' "
                     "ORDER BY (s.categoria = 'prioritaria') DESC, s.id LIMIT ?",
                     (ident, limite))


def transacoes(caminho, ident, limite=15):
    return consultar(caminho,
                     "SELECT t.id, t.recebido_em, t.seq, t.tipo, t.senha, t.categoria, "
                     "t.boot AS sessao FROM transacoes t "
                     "JOIN dispositivos d ON d.id = t.dispositivo_id "
                     "WHERE d.identificador = ? ORDER BY t.id DESC LIMIT ?", (ident, limite))


def emissoes_por_minuto(caminho, ident, minutos=30):
    return consultar(caminho,
                     "SELECT substr(t.recebido_em, 1, 16) AS minuto, t.categoria, "
                     "COUNT(*) AS senhas FROM transacoes t "
                     "JOIN dispositivos d ON d.id = t.dispositivo_id "
                     "WHERE d.identificador = ? AND t.tipo = 'senha_emitida' "
                     "AND t.recebido_em >= strftime('%Y-%m-%dT%H:%M', 'now', ?) "
                     "GROUP BY 1, 2 ORDER BY 1",
                     (ident, "-{} minutes".format(minutos)))


def serie_status(caminho, ident, minutos=30):
    return consultar(caminho,
                     "SELECT t.recebido_em, ts.rssi_dbm, ts.mem_livre, ts.fila_pendentes, "
                     "ts.intervalo_medio_s FROM telemetria_status ts "
                     "JOIN transacoes t ON t.id = ts.transacao_id "
                     "JOIN dispositivos d ON d.id = t.dispositivo_id "
                     "WHERE d.identificador = ? "
                     "AND t.recebido_em >= strftime('%Y-%m-%dT%H:%M:%S', 'now', ?) "
                     "ORDER BY t.id", (ident, "-{} minutes".format(minutos)))


# -------------------------------------------------------- comandos e logs
def comandos(caminho, ident, limite=15):
    return consultar(caminho,
                     "SELECT id, tipo, parametros, situacao, tentativas, criado_em, "
                     "concluido_em, resultado FROM vw_comandos WHERE dispositivo = ? "
                     "ORDER BY id DESC LIMIT ?", (ident, limite))


def comando(caminho, cid):
    return linha(caminho, "SELECT * FROM vw_comandos WHERE id = ?", (cid,))


def auditoria(caminho, ident=None, limite=20, so_alertas=True):
    sql = ("SELECT ocorrido_em, nivel, evento, dispositivo, seq, detalhe "
           "FROM logs_auditoria WHERE 1 = 1")
    params = []
    if ident:
        sql += " AND (dispositivo = ? OR dispositivo IS NULL)"
        params.append(ident)
    if so_alertas:
        sql += " AND nivel <> 'info'"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limite)
    return consultar(caminho, sql, params)
