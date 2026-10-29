# ==============================================================================
# boot.py — Inicialização e conexão Wi-Fi do ESP32 (MicroPython)
# Executado automaticamente pelo ESP32 a cada boot, ANTES do main.py.
# ==============================================================================
import network
import time

# Credenciais ficam em config.py (fora do Git). Se ele não existir,
# usa a rede aberta do simulador Wokwi.
try:
    from config import WIFI_SSID, WIFI_SENHA
except ImportError:
    WIFI_SSID = "Wokwi-GUEST"
    WIFI_SENHA = ""
    print("[boot] config.py não encontrado — usando rede do Wokwi.")

TIMEOUT_S = 20  # tempo máximo de espera pela conexão


def conectar_wifi(ssid, senha, timeout_s=TIMEOUT_S):
    """Conecta o ESP32 como estação (STA) e retorna o objeto WLAN."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        print("[boot] Já conectado:", wlan.ifconfig()[0])
        return wlan

    print("[boot] Conectando à rede '{}'".format(ssid), end="")
    wlan.connect(ssid, senha)

    inicio = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), inicio) > timeout_s * 1000:
            print("\n[boot] ERRO: tempo esgotado. Verifique SSID/senha.")
            wlan.active(False)
            return None
        print(".", end="")
        time.sleep(0.5)

    ip, mascara, gateway, dns = wlan.ifconfig()
    print("\n[boot] Wi-Fi conectado com sucesso!")
    print("[boot]   IP:      ", ip)
    print("[boot]   Gateway: ", gateway)
    print("[boot]   DNS:     ", dns)
    return wlan


wlan = conectar_wifi(WIFI_SSID, WIFI_SENHA)
