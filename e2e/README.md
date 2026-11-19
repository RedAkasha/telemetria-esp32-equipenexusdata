# Teste end-to-end (E2E)

Valida o **fluxo completo, nos dois sentidos, sem travamentos**:

```
botão → firmware → HTTP → API → SQLite → dashboard
dashboard → API → fila de comandos → firmware → confirmação → SQLite → dashboard
```

```bash
pip install -r backend/requirements.txt -r dashboard/requirements.txt
python -m pytest e2e -v          # ~1 min
```

## O que é real e o que é simulado

| Peça | No teste |
|---|---|
| API | **real**: `uvicorn api.main:app` num processo separado, banco SQLite em arquivo |
| Banco | **real**: o mesmo `schema.sql`, com WAL, triggers e `CHECK` |
| Firmware | **real**: `firmware/boot.py` e `main.py` executados sem alteração pelo [ESP32 virtual](../simulador/esp32_virtual.py) |
| Hardware | simulado: botões (com IRQ), buzzer (PWM), display, Wi-Fi e a rede (atraso, queda, timeout) |
| Dashboard | **real**: `dashboard/app.py` no Streamlit (`AppTest`); os comandos são enviados clicando nos **botões do painel** |

O ESP32 virtual troca só os módulos que existem apenas no MicroPython (`machine`, `network`, `urequests`, `time.ticks_ms`…), e só para o código do firmware. Ele também mede a **maior pausa do loop** (o maior intervalo em que o firmware ficou sem olhar o relógio), que é a forma objetiva de verificar "sem travamentos".

## Cenários (rodam em ordem e contam uma história)

| # | Cenário | O que é verificado |
|---|---|---|
| 1 | Totem liga | `inicio` e `status` chegam ao banco; painel mostra **online**, trava liberada, memória |
| 2 | Senhas pelos botões | N001, N002, P001 e o cancelamento de P001 (segurar 2 s) no banco, no saldo e nas métricas do painel; o `status` concilia sem divergência |
| 3 | **Alarme pelo dashboard** | clique → API → ESP32 toca a sirene (alternando 1800/2600 Hz) → confirmação `executado` com o retrato do totem → painel mostra "🚨 Ativo" → a sirene para sozinha |
| 4 | **Trava pelo dashboard** | travado: o toque é recusado (nenhum pacote), display "TRAVADO", estado gravado na flash; liberar: a próxima senha (N003) chega |
| 5 | **Reset de contador pelo dashboard** | contadores zerados; a numeração recomeça em N001 numa **sessão nova** (auditoria `contador_resetado`); o `status` da sessão nova concilia |
| 6 | API fora do ar | os toques vão para a fila de contingência (display `F:3`); o painel continua mostrando o banco e avisa "API fora do ar" ao tentar um comando; com a API de volta, tudo chega **na ordem original**; maior pausa do loop < 1 s |
| 7 | Rede inalcançável (cada requisição espera o timeout) | todos os toques contam; pior volta do loop ≤ **um** timeout (nunca vários somados); com pacotes presos o totem não insiste na consulta de comandos |
| 8 | Toque durante requisição lenta | um ruído de 5 ms com o loop livre **não** vira senha; um toque de 80 ms dado inteiro enquanto o loop estava bloqueado no HTTP é capturado pela **IRQ** (sem ela, se perde: conferido desligando a IRQ) |
| 9 | Totem desligado | um alarme pedido com o totem fora **expira** sem tocar; no reboot o totem volta **travado** (estado lido da flash) e o alarme expirado não toca; liberar funciona |
| 10 | Auditoria final | nenhuma senha perdida ou duplicada; saldo = senhas aguardando; nenhum comando pendurado; nenhum erro na auditoria e só os avisos esperados; nada descartado nem recusado no firmware; painel de pé |

## Resultado

```
e2e/test_fluxo_completo.py::test_01_totem_liga_e_aparece_online_no_dashboard PASSED
e2e/test_fluxo_completo.py::test_02_senhas_do_botao_chegam_ao_banco_e_ao_dashboard PASSED
e2e/test_fluxo_completo.py::test_03_dashboard_aciona_o_alarme_do_totem PASSED
e2e/test_fluxo_completo.py::test_04_trava_bloqueia_os_botoes_e_liberar_devolve PASSED
e2e/test_fluxo_completo.py::test_05_reset_de_contador_abre_sessao_nova PASSED
e2e/test_fluxo_completo.py::test_06_api_fora_do_ar_nada_se_perde_nem_trava PASSED
e2e/test_fluxo_completo.py::test_07_rede_inalcancavel_loop_nunca_trava_mais_que_um_timeout PASSED
e2e/test_fluxo_completo.py::test_08_toque_durante_requisicao_lenta_nao_se_perde PASSED
e2e/test_fluxo_completo.py::test_09_totem_desligado_comando_expira_e_trava_sobrevive_ao_reboot PASSED
e2e/test_fluxo_completo.py::test_10_auditoria_final_de_consistencia PASSED
============================= 10 passed in 56.70s ==============================
```

O mesmo teste roda no GitHub Actions a cada push (aba **Actions** do repositório).

## Problemas que o E2E encontrou e que foram corrigidos

- **Pacote recusado travava a fila para sempre.** O firmware só tirava um pacote da fila com resposta 2xx; um pacote que o servidor recusa (4xx) ficaria na cabeça da fila bloqueando todos os de trás. Agora 4xx definitivo sai da fila e é contado em `rejeitados`; sem rede, 408, 429 e 5xx continuam sendo reenviados.
- **Cada toque custava um timeout com o servidor fora do ar.** Agora, se já há pacotes esperando, o toque só entra na fila e o envio fica para a tentativa periódica (5 s).
- **Toque perdido durante o HTTP ou o bipe.** O `urequests` e o bipe bloqueiam o loop; um toque rápido nesse intervalo não era visto. Rodando o firmware anterior no simulador, dois toques rápidos seguidos mais um toque durante uma requisição lenta viravam **1 senha em vez de 3**. A IRQ na borda de descida resolve (cenários 2 e 8), e só vale quando o loop de fato ficou parado: com o loop livre, uma borda isolada é ruído e continua filtrada pelo debounce.
- **Dashboard quebrava com comando ainda não concluído** (data nula virava `NaN` no pandas).

## Na placa de verdade

O E2E garante a lógica; na bancada, o roteiro equivalente para evidência é: subir a API e o dashboard, ligar o ESP32 e repetir os cenários 2 a 6 pelo painel (passo a passo na [Etapa 8.2](../docs/passo_a_passo.md#82-dashboard-e-comandos)).
