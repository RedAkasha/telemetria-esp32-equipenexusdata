# ==============================================================================
# boot.py — Inicialização e conexão Wi-Fi do ESP32 (MicroPython)
# Executado automaticamente pelo ESP32 a cada boot, ANTES do main.py.
# ==============================================================================
import network
import time

# Credenciais ficam em config.py (fora do Git). Se ele não existir,
# usa a rede aberta do simulador Wokwi.
# ATENÇÃO: na placa todos os arquivos ficam na raiz, então o import é
# "from config import ..." — se o VS Code oferecer trocar para
# "firmware.config", recuse.
try:
    from config import WIFI_SSID, WIFI_SENHA
except ImportError:
    WIFI_SSID = "Wokwi-GUEST"
    WIFI_SENHA = ""
    print("[boot] config.py não encontrado — usando rede do Wokwi.")

TIMEOUT_S = 20  # tempo máximo de espera pela conexão


def listar_redes(wlan):
    """Mostra as redes que o ESP32 enxerga (ele só enxerga redes 2,4 GHz)."""
    try:
        redes = wlan.scan()
    except OSError:
        return
    print("[boot] Redes 2,4 GHz visíveis para o ESP32:")
    for ssid, _bssid, canal, rssi, _seg, _oculta in sorted(redes, key=lambda r: -r[3])[:10]:
        print("[boot]   {:<32} canal {:>2}  sinal {} dBm".format(
            ssid.decode("utf-8", "ignore") or "(oculta)", canal, rssi))


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
            listar_redes(wlan)
            print("[boot] Se a sua rede não aparece na lista, ela é só 5 GHz:")
            print("[boot] use outra rede ou o hotspot do celular em 2,4 GHz.")
            wlan.disconnect()
            return wlan
        print(".", end="")
        time.sleep(0.5)

    ip, mascara, gateway, dns = wlan.ifconfig()
    print("\n[boot] Wi-Fi conectado com sucesso!")
    print("[boot]   IP:      ", ip)
    print("[boot]   Gateway: ", gateway)
    print("[boot]   DNS:     ", dns)
    return wlan


wlan = conectar_wifi(WIFI_SSID, WIFI_SENHA)
