# Backend

| Arquivo | Papel |
|---|---|
| `api/` | **API definitiva** (FastAPI): valida os pacotes do ESP32 e grava no banco SQLite de forma atômica |
| `servidor_teste.py` | Servidor de teste das entregas anteriores (só biblioteca padrão, grava em `.jsonl`) |
| `tests/` | Testes automatizados da API e da tabela hash (`pytest`) |

## API — `api/`

```bash
cd backend
pip install -r requirements.txt
python -m api              # porta 5000 (ou: python -m api 8080)
```

Ela mostra o `API_URL` para o `config.py` do ESP32 (mesma rota do servidor de teste: `/leituras`). Documentação interativa em `http://localhost:5000/docs`. O banco fica em `backend/nexus.db` (ou no caminho da variável `NEXUS_DB`) e é criado a partir de [`database/schema.sql`](../database/schema.sql) na primeira execução.

| Módulo | Conteúdo |
|---|---|
| `api/main.py` | Rotas HTTP, leitura do corpo, códigos de resposta |
| `api/modelos.py` | Validação Pydantic: um modelo por `tipo` de pacote |
| `api/servico.py` | Regras de negócio, transação SQL atômica e índices em memória |
| `api/tabela_hash.py` | `TabelaHash` própria (encadeamento separado, FNV-1a, rehash em carga 0,75) |

### Rotas

| Método | Rota | Resposta |
|---|---|---|
| `POST` | `/leituras` | `201` gravado · `200` reenvio já gravado (idempotente) · `400` JSON inválido · `413` corpo > 8 KB · `422` campos inválidos · `500` falha na gravação (nada salvo) |
| `GET` | `/` | Totais: transações, dispositivos, senhas aguardando, logs |
| `GET` | `/saldos` | Saldo da fila de todos os totens (view `vw_saldos`) |
| `GET` | `/saldos/{dispositivo}` | Saldo de um totem, por busca O(1) nas tabelas hash |
| `GET` | `/dispositivos` | Totens cadastrados (boot atual, último `seq`, IP) |
| `GET` | `/dispositivos/{dispositivo}/senhas/{codigo}` | Senha aguardando, por busca O(1) |
| `GET` | `/transacoes?limite=&dispositivo=&tipo=` | Livro-razão, mais recentes primeiro |
| `GET` | `/auditoria?limite=&nivel=&evento=` | Logs de auditoria |
| `GET` | `/indices` | Itens, capacidade, fator de carga, colisões e maior balde de cada tabela hash |

### Validação

Cada `tipo` tem seu modelo (`inicio`, `senha_emitida`, `senha_cancelada`, `status`), escolhido pelo próprio campo `tipo`. Além de presença e tipo dos campos, a API confere: `seq` inteiro ≥ 1 (a string `"1"` é recusada), senha no formato `N001`/`P001`, prefixo coerente com a categoria (`N` = convencional, `P` = prioritária), contadores não negativos, `rssi_dbm` entre −127 e 0, `historico` com até 10 senhas e `dispositivo` só com letras, números, `-`, `_` e `.`. Todo pacote recusado vai para `logs_auditoria` com o corpo original.

### Tabelas hash — buscas O(1)

O banco é a fonte da verdade; as tabelas hash são índices em memória carregados do banco quando a API inicia, para que as decisões do caminho de gravação não precisem de consulta SQL:

| Índice | Chave → valor | Uso |
|---|---|---|
| `idx_idempotencia` | `dispositivo\|seq\|uptime_ms\|timestamp` → id da transação | Reconhecer o **reenvio** do ESP32 (que reenvia o mesmo pacote quando não recebe 2xx) e responder `200` sem gravar de novo |
| `idx_dispositivos` | identificador → boot, último `seq`, senhas recebidas no boot | Detectar reboot e pacotes perdidos (lacuna de `seq`); conciliar os totais do `status` |
| `idx_senhas_ativas` | (dispositivo, código) → id da senha aguardando | Achar a senha a cancelar sem varrer a tabela |
| `idx_saldos` | (dispositivo, categoria) → saldo | `GET /saldos/{dispositivo}` |

### Atomicidade

Cada pacote é gravado numa única transação `BEGIN IMMEDIATE … COMMIT`: dispositivo, livro-razão (`transacoes`), senha, saldo e auditoria. Qualquer erro faz `ROLLBACK` e nada do pacote fica no banco; o ESP32 recebe `500`, mantém o pacote na fila de contingência e reenvia. Os saldos são incrementados pelo próprio banco (`emitidas = emitidas + 1`, com `ON CONFLICT … DO UPDATE` e `RETURNING`), sem ler, somar e gravar no Python. Os índices em memória só mudam **depois** do `COMMIT`, para nunca indicarem algo que o banco não tem. Um `Lock` serializa as gravações (o SQLite tem um escritor por vez), e o modo WAL deixa o dashboard ler enquanto a API grava.

### Avisos (`logs_auditoria`, nível `aviso`)

Pacotes com estas situações são aceitos (`201`, para não travar a fila do firmware) e a resposta traz a lista em `avisos`:

| Evento | Quando |
|---|---|
| `dispositivo_novo` | Primeiro pacote de um `DISPOSITIVO_ID` |
| `novo_boot` | Pacote `inicio` ou `seq` que voltou ao começo (o firmware zera `seq` e os contadores a cada boot) |
| `seq_lacuna` | `seq` pulou números: pacotes perdidos |
| `senha_repetida` | Código já aguardando (ex.: N001 de novo após um reboot) |
| `cancelamento_sem_emissao` | Cancelamento de uma senha que o servidor nunca recebeu; o saldo não muda |
| `divergencia_totais` | Totais do `status` diferentes das senhas recebidas no boot (registrado só quando a diferença muda) |
| `perda_no_dispositivo` | `descartados > 0`: a fila de contingência do ESP32 transbordou |

### Testes

```bash
cd backend
python -m pytest -q
```

Cobrem validação, idempotência, saldo, reboot e lacuna de `seq`, conciliação, **rollback completo numa falha simulada no meio da transação**, imutabilidade do livro-razão e da auditoria, 4 totens gravando em paralelo sem perder incrementos e a recarga dos índices quando o servidor reinicia.

Teste rápido com `curl` (API rodando):
```bash
curl -X POST http://localhost:5000/leituras -H "Content-Type: application/json" -d "{\"dispositivo\":\"teste\",\"seq\":1,\"tipo\":\"senha_emitida\",\"senha\":\"N001\",\"categoria\":\"convencional\"}"
```

## `servidor_teste.py`

Servidor de teste das entregas anteriores. Recebe os pacotes, valida os campos obrigatórios, mostra cada um no terminal e grava em `leituras_recebidas.jsonl` (ignorado pelo Git). Usa só a biblioteca padrão do Python 3.

```bash
python backend/servidor_teste.py        # porta 5000 (ou: python backend/servidor_teste.py 8080)
```
