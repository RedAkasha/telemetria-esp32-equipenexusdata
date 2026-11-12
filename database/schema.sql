-- =============================================================================
-- schema.sql — Modelo relacional do Totem ByteBank Queue (NEXUS Data)
-- DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
--
-- Banco: SQLite 3.35+ (o mesmo que vem com o Python). O backend executa este
-- arquivo ao iniciar; todas as instruções usam IF NOT EXISTS, então rodar de
-- novo não apaga nada. Para criar o banco à mão:
--     sqlite3 backend/nexus.db < database/schema.sql
--
-- Tabelas:
--   dispositivos       cadastro dos totens ESP32 (boot atual, último seq)
--   transacoes         LIVRO-RAZÃO: cada pacote aceito vira uma linha imutável
--   senhas             cada senha emitida e sua situação (aguardando/cancelada)
--   saldos             saldo da fila por totem e categoria (emitidas − canceladas)
--   telemetria_status  métricas dos pacotes "status" (uma linha por pacote)
--   logs_auditoria     trilha de auditoria somente-inserção de TODA requisição
--
-- Regras garantidas pelo próprio banco (não dependem do código Python):
--   - pacote reenviado não duplica: transacoes.chave_idempotencia é UNIQUE;
--   - saldo nunca fica negativo e sempre fecha: CHECK em saldos;
--   - transações e logs não podem ser alterados nem apagados: triggers.
--
-- PostgreSQL: o modelo é o mesmo. Troque INTEGER PRIMARY KEY AUTOINCREMENT por
-- BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, TEXT de data por TIMESTAMPTZ,
-- payload TEXT CHECK(json_valid(...)) por JSONB, GLOB por SIMILAR TO e escreva
-- os triggers como funções PL/pgSQL.
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- Totens (ESP32). Uma linha por DISPOSITIVO_ID do config.py.
-- boot_atual sobe a cada religamento (pacote "inicio" ou seq que volta ao início),
-- porque o firmware reinicia seq e os contadores de senha a cada boot.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dispositivos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    identificador     TEXT    NOT NULL UNIQUE
                      CHECK (length(identificador) BETWEEN 1 AND 64),
    ip                TEXT,
    boot_atual        INTEGER NOT NULL DEFAULT 1 CHECK (boot_atual >= 1),
    ultimo_seq        INTEGER NOT NULL DEFAULT 0 CHECK (ultimo_seq >= 0),
    primeiro_contato  TEXT    NOT NULL,          -- ISO 8601 UTC
    ultimo_contato    TEXT    NOT NULL
);

-- -----------------------------------------------------------------------------
-- Livro-razão de transações: todo pacote ACEITO pelo backend.
-- chave_idempotencia = "dispositivo|seq|uptime_ms|timestamp": o firmware reenvia
-- o MESMO dicionário quando não recebe 2xx, então um reenvio gera a mesma chave
-- e é reconhecido como duplicado (o UNIQUE é a garantia final).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transacoes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chave_idempotencia  TEXT    NOT NULL UNIQUE,
    dispositivo_id      INTEGER NOT NULL REFERENCES dispositivos(id),
    boot                INTEGER NOT NULL CHECK (boot >= 1),
    seq                 INTEGER NOT NULL CHECK (seq >= 1),
    tipo                TEXT    NOT NULL
                        CHECK (tipo IN ('inicio', 'senha_emitida', 'senha_cancelada', 'status')),
    senha               TEXT    CHECK (senha IS NULL OR senha GLOB '[NP][0-9][0-9][0-9]'),
    categoria           TEXT    CHECK (categoria IS NULL OR categoria IN ('convencional', 'prioritaria')),
    coletado_em         TEXT,                     -- timestamp do ESP32 (NTP); NULL se sem relógio
    uptime_ms           INTEGER CHECK (uptime_ms IS NULL OR uptime_ms >= 0),
    recebido_em         TEXT    NOT NULL,         -- relógio do servidor
    payload             TEXT    NOT NULL CHECK (json_valid(payload)),
    -- coerência entre o tipo e os campos de senha
    CHECK (tipo <> 'senha_emitida'   OR (senha IS NOT NULL AND categoria IS NOT NULL)),
    CHECK (tipo <> 'senha_cancelada' OR  senha IS NOT NULL),
    CHECK (tipo IN ('senha_emitida', 'senha_cancelada') OR (senha IS NULL AND categoria IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_transacoes_disp_boot_tipo
    ON transacoes (dispositivo_id, boot, tipo, categoria);
CREATE INDEX IF NOT EXISTS idx_transacoes_recebido_em
    ON transacoes (recebido_em);

-- -----------------------------------------------------------------------------
-- Senhas emitidas. Nasce "aguardando" com a transação de emissão; vira
-- "cancelada" quando chega o senha_cancelada correspondente.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS senhas (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    dispositivo_id             INTEGER NOT NULL REFERENCES dispositivos(id),
    boot                       INTEGER NOT NULL,
    codigo                     TEXT    NOT NULL CHECK (codigo GLOB '[NP][0-9][0-9][0-9]'),
    categoria                  TEXT    NOT NULL CHECK (categoria IN ('convencional', 'prioritaria')),
    situacao                   TEXT    NOT NULL DEFAULT 'aguardando'
                               CHECK (situacao IN ('aguardando', 'cancelada')),
    transacao_emissao_id       INTEGER NOT NULL UNIQUE REFERENCES transacoes(id),
    transacao_cancelamento_id  INTEGER UNIQUE REFERENCES transacoes(id),
    emitida_em                 TEXT    NOT NULL,
    cancelada_em               TEXT,
    CHECK ((codigo GLOB 'N*') = (categoria = 'convencional')),
    CHECK ((situacao = 'cancelada') = (transacao_cancelamento_id IS NOT NULL)),
    CHECK ((situacao = 'cancelada') = (cancelada_em IS NOT NULL))
);

-- Índice parcial: só as senhas ainda na fila (as que podem ser canceladas)
CREATE INDEX IF NOT EXISTS idx_senhas_aguardando
    ON senhas (dispositivo_id, codigo) WHERE situacao = 'aguardando';

-- -----------------------------------------------------------------------------
-- Saldo da fila por totem e categoria. Atualizado SEMPRE na mesma transação SQL
-- que grava o evento, com incremento no próprio UPDATE (sem ler-e-escrever no
-- Python). versao sobe a cada alteração (controle de concorrência otimista).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS saldos (
    dispositivo_id  INTEGER NOT NULL REFERENCES dispositivos(id),
    categoria       TEXT    NOT NULL CHECK (categoria IN ('convencional', 'prioritaria')),
    emitidas        INTEGER NOT NULL DEFAULT 0 CHECK (emitidas   >= 0),
    canceladas      INTEGER NOT NULL DEFAULT 0 CHECK (canceladas >= 0),
    em_espera       INTEGER NOT NULL DEFAULT 0 CHECK (em_espera  >= 0),
    versao          INTEGER NOT NULL DEFAULT 0,
    atualizado_em   TEXT    NOT NULL,
    PRIMARY KEY (dispositivo_id, categoria),
    CHECK (canceladas <= emitidas),
    CHECK (em_espera = emitidas - canceladas)
);

-- -----------------------------------------------------------------------------
-- Métricas dos pacotes "status" (a cada 15 s), em colunas para o dashboard.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetria_status (
    transacao_id        INTEGER PRIMARY KEY REFERENCES transacoes(id),
    total_convencional  INTEGER NOT NULL CHECK (total_convencional >= 0),
    total_prioritaria   INTEGER NOT NULL CHECK (total_prioritaria  >= 0),
    canceladas          INTEGER,
    intervalo_medio_s   REAL,
    fila_pendentes      INTEGER,
    descartados         INTEGER,
    rssi_dbm            INTEGER,
    mem_livre           INTEGER
);

-- -----------------------------------------------------------------------------
-- Trilha de auditoria: uma linha por requisição recebida (aceita, duplicada ou
-- rejeitada) e por anomalia detectada (lacuna de seq, novo boot, divergência de
-- totais, perda no dispositivo...). dispositivo é TEXTO, e não FK, porque um
-- pacote rejeitado pode vir de um dispositivo que nem existe.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs_auditoria (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ocorrido_em    TEXT    NOT NULL,
    nivel          TEXT    NOT NULL CHECK (nivel IN ('info', 'aviso', 'erro')),
    evento         TEXT    NOT NULL,
    dispositivo    TEXT,
    seq            INTEGER,
    transacao_id   INTEGER REFERENCES transacoes(id),
    ip_origem      TEXT,
    detalhe        TEXT,
    payload        TEXT
);

CREATE INDEX IF NOT EXISTS idx_auditoria_ocorrido_em ON logs_auditoria (ocorrido_em);
CREATE INDEX IF NOT EXISTS idx_auditoria_evento      ON logs_auditoria (evento, nivel);

-- -----------------------------------------------------------------------------
-- Imutabilidade: livro-razão e auditoria são somente-inserção.
-- -----------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_transacoes_sem_update
BEFORE UPDATE ON transacoes
BEGIN
    SELECT RAISE(ABORT, 'transacoes é somente-inserção (livro-razão)');
END;

CREATE TRIGGER IF NOT EXISTS trg_transacoes_sem_delete
BEFORE DELETE ON transacoes
BEGIN
    SELECT RAISE(ABORT, 'transacoes é somente-inserção (livro-razão)');
END;

CREATE TRIGGER IF NOT EXISTS trg_auditoria_sem_update
BEFORE UPDATE ON logs_auditoria
BEGIN
    SELECT RAISE(ABORT, 'logs_auditoria é somente-inserção');
END;

CREATE TRIGGER IF NOT EXISTS trg_auditoria_sem_delete
BEFORE DELETE ON logs_auditoria
BEGIN
    SELECT RAISE(ABORT, 'logs_auditoria é somente-inserção');
END;

-- -----------------------------------------------------------------------------
-- Visão para o dashboard: saldo atual de cada totem, com o nome do dispositivo.
-- -----------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS vw_saldos AS
SELECT d.identificador AS dispositivo,
       s.categoria,
       s.emitidas,
       s.canceladas,
       s.em_espera,
       s.atualizado_em
FROM saldos s
JOIN dispositivos d ON d.id = s.dispositivo_id;
