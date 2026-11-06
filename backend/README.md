# Backend

## `servidor_teste.py` — servidor de teste da telemetria

Recebe os pacotes JSON que o totem ESP32 envia com `urequests.post()`, valida os campos obrigatórios de cada tipo (`inicio`, `senha_emitida`, `senha_cancelada`, `status`), mostra cada pacote no terminal e grava em `leituras_recebidas.jsonl` (ignorado pelo Git). Usa apenas a biblioteca padrão do Python 3.

```bash
python backend/servidor_teste.py        # porta 5000 (ou: python backend/servidor_teste.py 8080)
```

| Método | Rota | Resposta |
|---|---|---|
| `POST` | `/leituras` | `201 {"status": "ok", "seq": N, "total": T}` ou `400` com a lista de erros |
| `GET` | `/leituras` | Os 20 últimos pacotes recebidos |
| `GET` | `/` | Resumo: total recebido e último pacote |

O servidor também avisa quando a sequência `seq` pula números (pacotes perdidos).

Teste rápido sem o ESP32:
```bash
curl -X POST http://localhost:5000/leituras -H "Content-Type: application/json" -d "{\"dispositivo\":\"teste\",\"seq\":1,\"tipo\":\"senha_emitida\",\"senha\":\"N001\",\"categoria\":\"convencional\"}"
```

A API definitiva (fila de prioridade para a chamada das senhas, pilha de histórico de chamadas e gravação no banco relacional) vem nas próximas entregas.
