# Arquitetura do Sistema

Diagrama em blocos do fluxo de dados, do sensor até a visualização.

```mermaid
flowchart LR
    subgraph HW["🔌 Hardware — ESP32 (MicroPython)"]
        direction TB
        S1["Botão<br/>GPIO 15 · PULL_UP"]
        S2["Potenciômetro<br/>GPIO 34 · ADC"]
        A1["LED digital<br/>GPIO 2"]
        A2["LED PWM<br/>GPIO 4"]
        MCU["ESP32<br/>boot.py · main.py"]
        S1 --> MCU
        S2 --> MCU
        MCU --> A1
        MCU --> A2
    end

    API["⚙️ API Backend<br/>REST (HTTP/JSON)<br/>recebe e valida leituras"]
    DB[("🗄️ BD Relacional<br/>tabelas de dispositivos<br/>e leituras")]
    DASH["📊 Dashboard<br/>gráficos e indicadores"]

    MCU -- "Wi-Fi · HTTP POST<br/>{dispositivo, adc, tensão,<br/>botão, timestamp}" --> API
    API -- "INSERT (SQL)" --> DB
    DB -- "SELECT (SQL)" --> DASH
```

## Responsabilidade de cada bloco

| Bloco | Responsabilidade | Tecnologia (proposta) |
|---|---|---|
| **Hardware** | Ler sensores, acionar atuadores e enviar as leituras pela rede | ESP32 + MicroPython |
| **API Backend** | Receber as leituras, validar e gravar no banco; expor dados para o dashboard | A definir (ex.: Flask / FastAPI) |
| **BD Relacional** | Armazenar dispositivos e histórico de leituras | A definir (ex.: PostgreSQL / MySQL) |
| **Dashboard** | Exibir leituras em tempo quase real e histórico | A definir (ex.: Streamlit / Grafana) |

## Mapeamento dos pinos

| Componente | GPIO | Modo | Faixa |
|---|---|---|---|
| LED digital | 2 (onboard) | `Pin.OUT` | 0 / 1 |
| LED analógico | 4 | PWM 5000 Hz | duty 0–1023 |
| Botão | 15 | `Pin.IN` + `PULL_UP` | 0 = pressionado, 1 = solto |
| Potenciômetro | 34 (ADC1_CH6) | ADC `ATTN_11DB` | 0–4095 (0–3,3 V) |
