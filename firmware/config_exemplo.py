# ==============================================================================
# config_exemplo.py — MODELO de configuração
# 1. Copie este arquivo como config.py (na MESMA pasta firmware/)
# 2. Preencha com os dados da sua rede
# 3. Envie o config.py para o ESP32 (ele NÃO vai para o GitHub: está no .gitignore)
# ==============================================================================

# Rede Wi-Fi — o ESP32 só conecta em redes de 2,4 GHz
WIFI_SSID = "NOME_DA_SUA_REDE"
WIFI_SENHA = "SENHA_DA_SUA_REDE"

# Identificador deste ESP32 nos pacotes JSON
DISPOSITIVO_ID = "esp32-nexus-01"

# Para onde o main.py envia os pacotes (urequests.post).
# Opção A — servidor de teste da equipe (backend/servidor_teste.py), rodando no
#           notebook conectado à MESMA rede Wi-Fi do ESP32. Use o IPv4 do
#           adaptador Wi-Fi (comando: ipconfig) — não o do "vEthernet (WSL)":
API_URL = "http://192.168.0.10:5000/leituras"
#
# Opção B — servidor público de teste (quando a rede bloqueia o notebook):
# API_URL = "http://httpbin.org/post"

# Pinos (só mude se ligar os fios em outros GPIOs)
PINO_BOTAO_CONVENCIONAL = 32   # botão azul
PINO_BOTAO_PRIORITARIO = 33    # botão branco
PINO_BUZZER = 25
OLED_SDA = 5                   # display embutido na placa
OLED_SCL = 4
