# Backend

## Servidor de teste (`servidor_teste.py`)

Recebe a telemetria do ESP32 enquanto a API definitiva não fica pronta. Só usa a biblioteca padrão do Python.

```bash
python backend/servidor_teste.py            # porta 5000
python backend/servidor_teste.py --porta 8080
```

| Método | Rota | Resposta |
|---|---|---|
| `POST` | `/leituras` | `201` e grava o pacote em `leituras_recebidas.jsonl`; `400` se não for JSON; `422` se faltar campo obrigatório |
| `GET` | `/leituras` | últimos 50 pacotes recebidos |
| `GET` | `/` | status |

Campos obrigatórios: `dispositivo`, `seq`, `adc`, `tensao_v`, `botao_pressionado`. O servidor acrescenta `recebido_em` e `ip_origem` a cada registro.

## API definitiva

API REST que recebe as leituras do ESP32 e grava no banco. *(Próximas entregas.)*
