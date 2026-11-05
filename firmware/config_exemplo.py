# ==============================================================================
# config_exemplo.py — MODELO de configuração
# 1. Copie este arquivo como config.py
# 2. Preencha com os dados da sua rede
# 3. Envie o config.py para o ESP32 (ele NÃO vai para o GitHub: está no .gitignore)
# ==============================================================================

WIFI_SSID = "NOME_DA_SUA_REDE"
WIFI_SENHA = "SENHA_DA_SUA_REDE"

# Para onde o main.py envia os pacotes JSON (urequests.post).
# Opção A — servidor de teste local (backend/servidor_teste.py), rodando no PC
#           conectado à MESMA rede Wi-Fi do ESP32. Use o IP do PC:
API_URL = "http://192.168.0.10:5000/leituras"
# Opção B — servidor público de eco (devolve o JSON recebido):
# API_URL = "http://httpbin.org/post"

# Identificação da placa nos pacotes
DISPOSITIVO_ID = "esp32-nexusdata-01"

# Intervalo entre pacotes, em segundos
INTERVALO_ENVIO_S = 5
