# NEXUS Data — Totem ByteBank Queue (ESP32 + MicroPython)

Projeto da disciplina **Algoritmo e Estrutura de Dados (BD015)** — CESAR School · AV2 — Ecossistema ByteBank IoT.

**Módulo 2 — Totem de Atendimento & Gestão de Filas.** Um ESP32 com MicroPython emite senhas **convencionais** e **prioritárias** por dois botões físicos, confirma com buzzer e display OLED, organiza os dados em **estruturas de dados em memória** e envia cada evento como **JSON via Wi-Fi** (`urequests.post()`) para o backend, que grava no **banco relacional**. Um **dashboard Streamlit** mostra a telemetria em tempo real e envia **comandos** de volta ao totem (travar/liberar, alarme, resetar contador).

## 👥 Equipe

| Nome | Email |
|---|---|
| Filipe Oliveira | fon2@cesar.school |
| Paulo Bueno | pfdb@cesar.school |

## 🧱 Arquitetura

```mermaid
flowchart LR
    HW["🔌 Totem ESP32<br/>botões · buzzer · OLED<br/>(MicroPython)"]
    API["⚙️ API Backend (FastAPI)<br/>validação · tabelas hash O(1)"]
    DB[("🗄️ SQLite<br/>transações · saldos · auditoria")]
    DASH["📊 Dashboard Streamlit<br/>telemetria ao vivo · comandos"]

    HW -- "Wi-Fi · urequests.post() (JSON)" --> API
    API -- "transação atômica" --> DB
    DB -- "SELECT (tempo real)" --> DASH
    DASH -. "POST /comandos" .-> API
    HW -. "busca comando (a cada 2 s)<br/>confirma execução" .-> API
```

Fluxo interno do firmware, estruturas de dados e formato dos pacotes em [`docs/arquitetura.md`](docs/arquitetura.md).

## 🔌 Esquema de ligação

![Esquema de ligação do circuito](docs/img/esquema_ligacao.svg)

Placa **ESP32 AM-032** (display OLED 0,96" embutido). Pinos da fileira do lado do botão EN:

| Componente | GPIO | Modo | Papel |
|---|---|---|---|
| Botão azul | 32 | `Pin.IN` + `PULL_UP` (0 = pressionado) | Sensor — emite senha **convencional** (N001…) |
| Botão branco | 33 | `Pin.IN` + `PULL_UP` | Sensor — emite senha **prioritária** (P001…) |
| Buzzer | 25 | PWM 2 kHz | Atuador — bipe de confirmação |
| Display OLED SSD1306 | SDA 5 · SCL 4 (interno) | I2C, endereço 0x3C | Atuador — mostra a senha, histórico e status |
| GND da placa | GND | — | Trilho (−) da protoboard: retorno dos botões e do buzzer |

Os botões usam pernas em diagonal. Os pinos 34/35 não servem para botões porque não têm pull-up interno.

## 🧠 Estruturas de dados no ESP32

Em [`firmware/estruturas.py`](firmware/estruturas.py). Todas usam lista de **tamanho fixo alocada uma única vez** (memória previsível no ESP32) e operações **O(1)**:

| Estrutura | Papel no totem | Operações |
|---|---|---|
| **Fila de contingência offline** (`FilaContingencia`, FIFO, 50) | Todo pacote JSON entra na fila e só sai depois que o servidor responde 2xx. Sem Wi-Fi ou com o servidor fora do ar, os pacotes ficam guardados e são reenviados **na ordem original** quando a conexão volta | `enfileirar` · `espiar` · `desenfileirar` |
| **Pilha LIFO de histórico** (`PilhaHistorico`, 10) | Últimas senhas emitidas; o topo é a senha exibida no display. Segurar um botão 2 s faz `pop()` e **cancela a última senha** | `push` · `pop` · `topo` · `ultimos(k)` |
| **Janela deslizante** (`JanelaDeslizante`, 5) | Média móvel do intervalo entre senhas: o ritmo de chegada de clientes, enviado no pacote de status | `adicionar` · `media` (soma corrente) |

## ⚙️ Backend e banco de dados

A API em [`backend/api/`](backend/api/) recebe o `POST /leituras` do ESP32, valida cada pacote com Pydantic (um modelo por `tipo`) e grava tudo numa **única transação SQL** (`BEGIN IMMEDIATE … COMMIT`): livro-razão de transações, senha, saldo da fila e log de auditoria. Uma falha no meio faz `ROLLBACK` e nada do pacote fica gravado; o ESP32 recebe erro e reenvia.

| Estrutura no backend | Papel | Custo |
|---|---|---|
| `TabelaHash` própria ([`tabela_hash.py`](backend/api/tabela_hash.py), encadeamento separado + FNV-1a) | Índices em memória: chave de idempotência, dispositivos, senhas aguardando, saldos | busca/inserção/remoção O(1) médio |
| `saldos` (SQL) | `emitidas`, `canceladas`, `em_espera` por totem e categoria, incrementados no próprio `UPDATE` | O(1) por chave primária |
| `logs_auditoria` (SQL) | Toda requisição aceita, duplicada ou rejeitada e toda anomalia (reboot, lacuna de `seq`, divergência de totais) | somente-inserção |

Como o firmware reenvia o pacote quando não recebe 2xx, a API é **idempotente**: o reenvio é achado na tabela hash e respondido com `200` sem duplicar nada. Modelo do banco em [`database/`](database/README.md); rotas e testes em [`backend/README.md`](backend/README.md).

## 📊 Dashboard e comandos

[`dashboard/app.py`](dashboard/app.py) lê o banco **direto** (somente leitura, modo WAL) e se atualiza sozinho a cada 2 s (`st.fragment(run_every=…)`): online/offline, trava e alarme, sessão, RSSI, memória, fila offline do ESP32, senhas em espera por categoria, gráficos e auditoria. Detalhes em [`dashboard/README.md`](dashboard/README.md).

Prints com o **ESP32 de verdade** (`esp32-nexus-01`) na rede do campus:

| Painel de comandos | Telemetria ao vivo |
|---|---|
| ![Painel de comandos do dashboard: liberar trava, travar, alarme com duração, silenciar e resetar contador](docs/img/dashboard_tempo_real1.png) | ![Totem online, saldos da fila e gráficos de senhas por minuto, RSSI e fila offline](docs/img/dashboard_tempo_real2.png) |

![Senhas aguardando, últimos pacotes, comandos executados e alertas da auditoria](docs/img/dashboard_tempo_real3.png)

Os **comandos** fazem o caminho inverso. O ESP32 não recebe conexões, então ele **consulta** a API a cada 2 s:

```mermaid
sequenceDiagram
    participant D as Dashboard
    participant A as API
    participant E as ESP32
    D->>A: POST /comandos {travar}
    Note over A: fila FIFO do totem<br/>(TabelaHash → deque)
    E->>A: GET /dispositivos/{id}/comandos/proximo
    A-->>E: {id, tipo, parametros}
    Note over E: executa (trava, sirene,<br/>reset) sem travar o loop
    E->>A: POST /comandos/{id}/confirmacao {executado, estado}
    A-->>D: situação "executado" no banco
```

| Comando | No totem |
|---|---|
| `travar` / `liberar_trava` | travado, os botões não emitem senhas e o display mostra TRAVADO; aciona o relé/LED do `PINO_TRAVA` (opcional); o estado sobrevive a reboot |
| `acionar_alarme` (1–60 s) / `silenciar_alarme` | sirene de dois tons no buzzer, **não bloqueante** |
| `resetar_contador` | zera N/P, pilha e janela; a numeração recomeça e o servidor abre uma sessão nova |

Garantias: por totem, um comando por vez e **na ordem** (FIFO); sem confirmação em 10 s ele é reentregue (até 3 vezes) e o firmware **não executa duas vezes** o mesmo id; um comando não buscado dentro da **validade** expira (um alarme pedido com o totem desligado não toca quando ele volta); a confirmação também passa por uma fila de contingência no ESP32.

## 🧪 Teste end-to-end

[`e2e/`](e2e/README.md) sobe a **API real**, executa o **firmware real** (`boot.py` + `main.py`, sem alteração) no [ESP32 virtual](simulador/esp32_virtual.py), que simula só o hardware, e opera o **dashboard real** clicando nos botões dele. São 10 cenários em ordem: senhas, alarme, trava, reset, API fora do ar, rede com timeout, toque durante requisição lenta, totem desligado com comando expirando e reboot, e uma auditoria final de consistência. O simulador mede a maior pausa do loop do firmware: com a rede caída, nenhuma volta do loop passa de **um** timeout.

```bash
python -m pytest e2e -v        # 10 passed em ~1 min; também roda no GitHub Actions
```

## 📦 Pacotes JSON enviados

`main.py` envia via `urequests.post()` para o `API_URL` configurado. Quatro tipos de pacote, todos com o mesmo cabeçalho:

```json
{
  "dispositivo": "esp32-nexus-01",
  "seq": 12,
  "tipo": "senha_emitida",
  "timestamp": "2026-11-05T23:41:05Z",
  "uptime_ms": 184220,
  "senha": "P004",
  "categoria": "prioritaria"
}
```

| `tipo` | Quando | Campos extras |
|---|---|---|
| `inicio` | ao ligar, ou após o comando `resetar_contador` | `ip`, `motivo` (`boot` / `resetar_contador`), `comando_id` |
| `senha_emitida` | toque rápido num botão | `senha`, `categoria` |
| `senha_cancelada` | segurar um botão 2 s | `senha` |
| `status` | a cada 15 s | `total_convencional`, `total_prioritaria`, `canceladas`, `intervalo_medio_s`, `historico`, `fila_pendentes`, `descartados`, `rssi_dbm`, `mem_livre`, `travado`, `alarme` |

O `timestamp` é gerado na **coleta**, com o relógio acertado via NTP: um pacote que esperou na fila mantém o horário real do evento. O `seq` permite ao servidor detectar perdas.

## 📁 Estrutura do repositório

```
├── firmware/                 Código do ESP32 (MicroPython)
│   ├── boot.py               Conexão Wi-Fi (roda a cada boot)
│   ├── main.py               Totem: botões, buzzer, OLED, fila e envio HTTP/JSON
│   ├── estruturas.py         FilaContingencia, PilhaHistorico, JanelaDeslizante
│   ├── ssd1306.py            Driver do display OLED (micropython-lib, MIT)
│   ├── teste_hardware.py     Diagnóstico de display, buzzer, botões e envio
│   ├── config_exemplo.py     Modelo de configuração (copiar para config.py)
│   └── wokwi/diagram.json    Circuito para o simulador Wokwi
├── backend/
│   ├── api/                  API FastAPI: validação, regras de negócio, tabela hash
│   ├── tests/                Testes automatizados (pytest)
│   ├── requirements.txt      Dependências da API
│   └── servidor_teste.py     Servidor HTTP de teste das entregas anteriores
├── database/
│   └── schema.sql            Modelo relacional: transações, saldos, auditoria, comandos
├── dashboard/
│   ├── app.py                Dashboard Streamlit: telemetria ao vivo e comandos
│   ├── dados.py              Consultas ao SQLite (somente leitura)
│   └── api_cliente.py        Envio de comandos para a API
├── simulador/                ESP32 virtual: roda o firmware real sem a placa
├── e2e/                      Teste end-to-end (API + firmware + banco + dashboard)
├── .github/workflows/        Testes automáticos a cada push (GitHub Actions)
└── docs/
    ├── passo_a_passo.md      Guia completo: da bancada à entrega
    ├── arquitetura.md        Arquitetura, fluxo do firmware e formato dos dados
    ├── guia_imagens.md       Como gerar foto, GIF e prints
    └── img/                  Esquema, fotos e evidências
```

## 🚀 Como executar

> Guia detalhado para quem nunca usou ESP32, com solução de problemas: **[`docs/passo_a_passo.md`](docs/passo_a_passo.md)**.

1. **Driver USB:** instale o driver **CP210x** ([Silicon Labs](https://www.silabs.com/software-and-tools/usb-to-uart-bridge-vcp-drivers?tab=downloads)) ou **CH340**, conforme o chip da placa, e anote a porta COM no Gerenciador de Dispositivos.
2. **MicroPython (apenas uma vez):**
   ```bash
   pip install esptool
   esptool --chip esp32 --port COM5 erase_flash
   esptool --chip esp32 --port COM5 --baud 115200 write_flash -z --flash_mode dio 0x1000 ESP32_GENERIC-20260824-v1.29.0.bin
   ```
3. **API backend**, no notebook, que precisa estar na mesma rede do ESP32:
   ```bash
   cd backend
   pip install -r requirements.txt
   python -m api
   ```
   Ela cria o banco `backend/nexus.db` a partir de `database/schema.sql` e mostra o `API_URL` a usar. Saldos e auditoria em `http://localhost:5000/docs`. (O servidor de teste antigo continua disponível: `python backend/servidor_teste.py`.) Confira com `ipconfig` que é o IPv4 do adaptador **Wi-Fi** e libere a porta 5000 no firewall (PowerShell **como administrador**; sem isso o ESP32 recebe `ETIMEDOUT`):
   ```powershell
   New-NetFirewallRule -DisplayName "NEXUS API 5000" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow -Profile Any
   ```
4. **Configuração:** copie `firmware/config_exemplo.py` para `firmware/config.py` e preencha `WIFI_SSID`, `WIFI_SENHA` (rede **2,4 GHz**) e `API_URL`.
   > ⚠️ O `config.py` está no `.gitignore`: **nunca** faça commit da senha do Wi-Fi.
5. **Enviar para a placa** `boot.py`, `config.py`, `estruturas.py`, `ssd1306.py` e `main.py`:
   - **Thonny:** *Exibir → Arquivos* → selecione os arquivos → botão direito → *Enviar para /*.
   - **VS Code + MicroPico:** abra a pasta `firmware/` → *MicroPico: Connect* → *MicroPico: Upload project to board*.
6. **Diagnóstico (opcional):** abra `firmware/teste_hardware.py` no Thonny e clique em ▶.
7. **Rodar:** aperte **EN** na placa. Toque no botão azul ou branco para emitir uma senha; segure 2 s para cancelar a última. Cada pacote é gravado no banco; confira em `http://localhost:5000/saldos` e `/auditoria`.
8. **Dashboard e comandos**, num segundo terminal:
   ```bash
   pip install -r dashboard/requirements.txt
   streamlit run dashboard/app.py
   ```
   Abre `http://localhost:8501`. Use os botões **Travar**, **Acionar alarme** e **Resetar contador** e veja o totem reagir em até 2 s. Sem placa: `python -m simulador --auto 4` roda o firmware no computador.
9. **Testar a fila offline:** pare o servidor (Ctrl+C) e emita algumas senhas. O display mostra `F:1`, `F:2`… Religue o servidor: os pacotes são reenviados em ordem e a fila volta a `F:0`.

**Sem placa?** No [Wokwi](https://wokwi.com/esp32), crie um projeto ESP32 + MicroPython, adicione `boot.py`, `estruturas.py`, `ssd1306.py` e `main.py` e use as peças e ligações de [`firmware/wokwi/diagram.json`](firmware/wokwi/diagram.json). Mantenha a linha da placa do seu projeto, que define a versão do MicroPython. Sem `config.py`, o firmware usa a rede `Wokwi-GUEST` e envia para `http://httpbin.org/post`. Nesse modo os comandos do dashboard ficam desligados (o httpbin não é a API); para testar comandos sem placa, use `python -m simulador`. Projeto da equipe: https://wokwi.com/projects/479258817358709761

## 🔬 Validação na placa real

Roteiro executado em 19/11/2026 com o ESP32 AM-032 e a API num notebook, na mesma rede Wi-Fi do campus (ESP32 em `172.26.66.199`, API em `172.26.68.5:5000`). Os prints estão em [Evidências](#-evidências).

| # | Teste | Resultado |
|---|---|---|
| 1 | **Fila offline** com a API inalcançável | Cada envio falhou com `ETIMEDOUT` e o totem seguiu rodando; os pacotes de `status` acumularam na fila de contingência (`fila=1` → `fila=20`). Liberada a conexão, a fila foi reenviada **em ordem**, 5 por ciclo (`16 → 11 → 6 → 2 → 0`). O gráfico "Fila offline do ESP32" do dashboard mostra o pico de 20 e a volta a zero |
| 2 | **Senhas pelos botões** | N001–N004 e P001–P003 emitidas (7); P002 cancelada segurando o botão 2 s. O dashboard mostrou 4 convencionais + 2 prioritárias em espera, as prioritárias no topo da lista, e as senhas por minuto separadas por categoria |
| 3 | **Comandos do dashboard** | Travar, Liberar trava, Acionar alarme (10 s, duas vezes) e Resetar contador: todos ✅ **executado** pela placa |
| 4 | **Reset de contador** | Sessão 1 → 2 no painel e evento `contador_resetado` na auditoria |
| 5 | **API desligada e religada** | Os pacotes guardados chegaram juntos assim que a API voltou (8 `POST /leituras` → `201` seguidos, vindos de `172.26.66.199`); depois o totem voltou a consultar comandos a cada 2 s |
| 6 | **Telemetria** | Totem `online`, RSSI entre −58 e −66 dBm, ~93 KB de memória livre, atualização a cada 2 s |

**Lição de bancada:** o ESP32 recebia `ETIMEDOUT` mesmo com o IP certo no `config.py`. As causas eram a API desligada e o Firewall do Windows, que descarta conexões de entrada em redes classificadas como públicas. Com a API no ar e a porta 5000 liberada (passo 3 de [Como executar](#-como-executar)), a conexão funcionou.

Além da placa, o fluxo completo roda automaticamente a cada push no GitHub Actions: firmware compilado pelo `mpy-cross`, 44 testes do backend e 10 cenários end-to-end ([`e2e/`](e2e/README.md)).

## 📸 Evidências

> Como produzir e publicar estas imagens: [`docs/guia_imagens.md`](docs/guia_imagens.md).

### Circuito montado na bancada
![Circuito montado na bancada](docs/img/circuito_bancada.jpg)

### Totem funcionando
<img src="docs/img/circuito_funcionando.gif" alt="Botões emitindo senhas e display mostrando a senha atual" width="360">

### Fila de contingência na placa real (REPL)
Com a API inalcançável, cada envio falha com `ETIMEDOUT` e a fila cresce até 20 pacotes; quando a conexão volta, ela é esvaziada em ordem, 5 pacotes por ciclo.

![Shell do Thonny: falhas ETIMEDOUT, fila subindo até 20 e depois 16, 11, 6, 2, 0 pendentes](docs/img/evidencia_fila_offline_placa.png)

### Pacotes recebidos pela API
Ao religar a API, os pacotes guardados no ESP32 (`172.26.66.199`) chegam juntos com `201 Created`; em seguida o totem volta a consultar comandos.

![Terminal da API: POST /leituras 201 vindos do ESP32 e consultas GET de comandos](docs/img/evidencia_servidor.png)

### Envio dos pacotes JSON (entrega anterior, servidor de teste)
![REPL mostrando os pacotes enviados e a fila de contingência esvaziando](docs/img/evidencia_envio_repl.png)

### Dashboard
Prints na seção [Dashboard e comandos](#-dashboard-e-comandos).

### Conexão Wi-Fi (simulador Wokwi)
![REPL do Wokwi mostrando o IP obtido](docs/evidencia_wifi.jpg)

## ✅ Status das entregas

- [x] Repositório público com README inicial
- [x] Diagrama de arquitetura em blocos
- [x] `boot.py` com conexão Wi-Fi
- [x] `main.py` com leitura dos sensores (botões) e acionamento dos atuadores (buzzer, OLED)
- [x] Estruturas de dados em memória no ESP32: fila de contingência, pilha LIFO e janela deslizante
- [x] Envio de pacotes JSON via `urequests.post()` para servidor de teste
- [x] Foto do circuito montado na bancada
- [x] API backend (FastAPI) com validação dos pacotes e gravação atômica no banco
- [x] Modelagem do banco relacional (`database/schema.sql`): transações, saldos e logs de auditoria
- [x] Buscas O(1) em tabelas hash no backend (idempotência, dispositivos, senhas, saldos)
- [x] Dashboard Streamlit conectado ao banco, com a telemetria em tempo real
- [x] Comandos do dashboard para o ESP32: travar/liberar, acionar/silenciar alarme, resetar contador
- [x] Teste end-to-end do fluxo completo, sem travamentos (`e2e/`, também no GitHub Actions)
- [x] Validação na placa real: senhas, comandos, reset e fila offline com o ESP32 e o dashboard
- [x] Evidências: foto, GIF do totem, fila offline no REPL, pacotes chegando na API e prints do dashboard
- [ ] Fila de prioridade para a chamada das senhas
