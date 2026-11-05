# ==============================================================================
# main.py — Sensores, atuadores e telemetria HTTP/JSON (ESP32 / MicroPython)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
# Executado automaticamente após o boot.py (que já conectou o Wi-Fi).
#
# Fluxo de cada ciclo (a cada 300 ms):
#   1. Lê o botão (GPIO 15) e o potenciômetro (GPIO 34, ADC 12 bits).
#   2. Insere o ADC na JANELA DESLIZANTE e calcula a média móvel.
#   3. Aciona os atuadores: LED digital (GPIO 2) segue o botão e o brilho
#      do LED PWM (GPIO 4) segue a média móvel do potenciômetro.
#   4. A cada INTERVALO_ENVIO_S monta um pacote JSON e o coloca na
#      FILA DE CONTINGÊNCIA; em seguida tenta esvaziar a fila com
#      urequests.post(). Sem rede, os pacotes ficam guardados e são
#      reenviados em ordem (FIFO) quando a conexão voltar.
#
# Dica de demonstração: SEGURE o botão por 3 s para simular a queda da
# rede (modo offline). Segure de novo por 3 s para voltar ao modo online.
# ==============================================================================
from machine import Pin, ADC, PWM
import network
import time
import gc

try:
    import urequests                 # nome clássico do módulo
except ImportError:
    import requests as urequests     # nome atual no micropython-lib

from estruturas import FilaCircular, JanelaMovel

# ------------------------------------------------------------------------------
# 1. Configuração (config.py é opcional; sem ele, valem os padrões abaixo)
# ------------------------------------------------------------------------------
try:
    import config
except ImportError:
    config = None


def _cfg(nome, padrao):
    return getattr(config, nome, padrao) if config else padrao


WIFI_SSID = _cfg("WIFI_SSID", "Wokwi-GUEST")
WIFI_SENHA = _cfg("WIFI_SENHA", "")
# Sem config.py (ex.: no Wokwi) envia para o httpbin, que devolve o JSON recebido.
API_URL = _cfg("API_URL", "http://httpbin.org/post")
DISPOSITIVO_ID = _cfg("DISPOSITIVO_ID", "esp32-nexusdata-01")

INTERVALO_LEITURA_MS = 300
INTERVALO_ENVIO_MS = int(_cfg("INTERVALO_ENVIO_S", 5) * 1000)
INTERVALO_LOG_MS = 1000           # frequência das linhas de leitura no REPL
INTERVALO_RECONEXAO_MS = 15000    # espera entre tentativas de reconectar
TEMPO_SEGURAR_MS = 3000           # botão segurado = alterna modo offline
CAPACIDADE_FILA = 50              # ~4 min de pacotes com envio a cada 5 s
TAMANHO_JANELA = 10               # média das 10 últimas leituras (~3 s)
MAX_ENVIOS_POR_CICLO = 10         # não travar o loop drenando a fila
TIMEOUT_HTTP_S = 5

# ------------------------------------------------------------------------------
# 2. Mapeamento dos pinos
# ------------------------------------------------------------------------------
led_digital = Pin(2, Pin.OUT)                 # LED onboard
led_pwm = PWM(Pin(4), freq=5000)              # LED com brilho controlado
botao = Pin(15, Pin.IN, Pin.PULL_UP)          # 0 = pressionado, 1 = solto
potenciometro = ADC(Pin(34))                  # ADC1_CH6
potenciometro.atten(ADC.ATTN_11DB)            # faixa 0–3,3 V (0–4095)

# ------------------------------------------------------------------------------
# 3. Estruturas de dados em memória
# ------------------------------------------------------------------------------
fila = FilaCircular(CAPACIDADE_FILA)          # contingência offline (FIFO)
janela = JanelaMovel(TAMANHO_JANELA)          # média móvel do ADC

wlan = network.WLAN(network.STA_IF)

estado = {
    "seq": 0,                 # número sequencial do pacote
    "offline_simulado": False,
    "inicio_pressao": None,   # ticks_ms em que o botão foi pressionado
    "alternou_nesta_pressao": False,
    "ultima_reconexao": None,
    "enviados": 0,
    "falhas": 0,
}


# ------------------------------------------------------------------------------
# 4. Rede
# ------------------------------------------------------------------------------
def rede_disponivel():
    return (not estado["offline_simulado"]) and wlan.isconnected()


def tentar_reconectar(agora):
    """Reconexão não bloqueante: dispara wlan.connect() de tempos em tempos."""
    if estado["offline_simulado"] or wlan.isconnected():
        return
    ultima = estado["ultima_reconexao"]
    if ultima is not None and time.ticks_diff(agora, ultima) < INTERVALO_RECONEXAO_MS:
        return
    estado["ultima_reconexao"] = agora
    print("[wifi] Sem conexão — tentando reconectar a '{}'...".format(WIFI_SSID))
    try:
        wlan.active(True)
        wlan.connect(WIFI_SSID, WIFI_SENHA)
    except OSError as e:
        print("[wifi] Falha ao iniciar reconexão:", e)


def sincronizar_relogio():
    """Acerta o relógio via NTP (UTC) para os pacotes levarem data/hora."""
    if not wlan.isconnected():
        return
    try:
        import ntptime
        ntptime.settime()
        print("[ntp] Relógio sincronizado (UTC).")
    except Exception as e:
        print("[ntp] Não foi possível sincronizar:", e)


def timestamp_iso():
    """Data/hora UTC em ISO 8601, ou None se o relógio não foi acertado."""
    t = time.gmtime()
    if t[0] < 2024:
        return None
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % tuple(t[:6])


def enviar(pacote):
    """Faz o POST do pacote em JSON. Retorna (sucesso, status_ou_erro)."""
    resposta = None
    try:
        try:
            resposta = urequests.post(API_URL, json=pacote, timeout=TIMEOUT_HTTP_S)
        except TypeError:
            # Versões antigas do urequests não aceitam o argumento timeout.
            resposta = urequests.post(API_URL, json=pacote)
        status = resposta.status_code
        return (200 <= status < 300), status
    except Exception as e:  # OSError (timeout, DNS, conexão recusada...)
        return False, repr(e)
    finally:
        if resposta is not None:
            resposta.close()  # essencial no ESP32: libera o socket
        gc.collect()


def esvaziar_fila():
    """Envia os pacotes pendentes em ordem FIFO.

    O pacote só sai da fila (desenfileirar) DEPOIS que a API respondeu 2xx.
    Se o POST falhar, ele continua no início da fila para a próxima tentativa.
    """
    enviados_agora = 0
    while (not fila.vazia()) and enviados_agora < MAX_ENVIOS_POR_CICLO:
        if not rede_disponivel():
            break
        pacote = fila.espiar()
        ok, info = enviar(pacote)
        if ok:
            fila.desenfileirar()
            enviados_agora += 1
            estado["enviados"] += 1
            print("[http] POST seq={} -> {} OK | pendentes na fila: {}".format(
                pacote["seq"], info, len(fila)))
        else:
            estado["falhas"] += 1
            print("[http] POST seq={} falhou ({}) — mantido na fila ({} pendentes)".format(
                pacote["seq"], info, len(fila)))
            break


# ------------------------------------------------------------------------------
# 5. Sensores, atuadores e pacote
# ------------------------------------------------------------------------------
def tratar_pressao_longa(pressionado, agora):
    """Segurar o botão por TEMPO_SEGURAR_MS alterna o modo offline simulado."""
    if not pressionado:
        estado["inicio_pressao"] = None
        estado["alternou_nesta_pressao"] = False
        return
    if estado["inicio_pressao"] is None:
        estado["inicio_pressao"] = agora
        return
    if (not estado["alternou_nesta_pressao"]
            and time.ticks_diff(agora, estado["inicio_pressao"]) >= TEMPO_SEGURAR_MS):
        estado["offline_simulado"] = not estado["offline_simulado"]
        estado["alternou_nesta_pressao"] = True
        if estado["offline_simulado"]:
            print("[modo] OFFLINE simulado: pacotes vão se acumular na fila.")
        else:
            print("[modo] ONLINE: reenviando os pacotes acumulados.")


def montar_pacote(leitura):
    estado["seq"] += 1
    return {
        "dispositivo": DISPOSITIVO_ID,
        "seq": estado["seq"],
        "timestamp": timestamp_iso(),
        "uptime_ms": time.ticks_ms(),
        "botao_pressionado": leitura["pressionado"],
        "led_digital": leitura["led"],
        "adc": leitura["adc"],
        "adc_media_movel": round(leitura["media"], 1),
        "tensao_v": round(leitura["tensao"], 3),
        "tensao_media_v": round(leitura["tensao_media"], 3),
        "pwm_duty": leitura["duty"],
        "gerado_offline": not rede_disponivel(),
        "fila_pendentes": len(fila),
    }


def ler_e_acionar():
    """Lê os sensores, atualiza a janela móvel e aciona os atuadores."""
    pressionado = botao.value() == 0           # PULL_UP: 0 = pressionado
    led_digital.value(1 if pressionado else 0)

    valor_adc = potenciometro.read()           # 0–4095
    media = janela.adicionar(valor_adc)        # média móvel O(1)
    duty = int(media) // 4                     # 0–4095 -> 0–1023
    led_pwm.duty(duty)

    return {
        "pressionado": pressionado,
        "led": 1 if pressionado else 0,
        "adc": valor_adc,
        "media": media,
        "tensao": (valor_adc / 4095.0) * 3.3,
        "tensao_media": (media / 4095.0) * 3.3,
        "duty": duty,
    }


# ------------------------------------------------------------------------------
# 6. Loop principal
# ------------------------------------------------------------------------------
def executar(max_ciclos=None):
    print("-" * 70)
    print("NEXUS Data — telemetria ESP32 | dispositivo:", DISPOSITIVO_ID)
    print("API:", API_URL)
    print("Fila de contingência: {} pacotes | Janela móvel: {} leituras".format(
        CAPACIDADE_FILA, TAMANHO_JANELA))
    print("Segure o botão por 3 s para simular queda/retorno da rede.")
    print("-" * 70)

    sincronizar_relogio()
    ultimo_envio = time.ticks_ms()
    ultimo_log = ultimo_envio
    ciclo = 0

    try:
        while max_ciclos is None or ciclo < max_ciclos:
            ciclo += 1
            agora = time.ticks_ms()

            leitura = ler_e_acionar()
            tratar_pressao_longa(leitura["pressionado"], agora)

            if time.ticks_diff(agora, ultimo_log) >= INTERVALO_LOG_MS:
                ultimo_log = agora
                print("[{:05d}] Botão: {:<5} | ADC: {:04d} ({:.2f} V) | "
                      "Média({}): {:7.1f} | PWM: {:04d}/1023 | Fila: {:02d} | {}".format(
                          ciclo, "ON" if leitura["pressionado"] else "OFF",
                          leitura["adc"], leitura["tensao"], len(janela),
                          leitura["media"], leitura["duty"], len(fila),
                          "online" if rede_disponivel() else "OFFLINE"))

            if time.ticks_diff(agora, ultimo_envio) >= INTERVALO_ENVIO_MS:
                ultimo_envio = agora
                pacote = montar_pacote(leitura)
                descartado = fila.enfileirar(pacote)
                if descartado is not None:
                    print("[fila] Cheia! Pacote seq={} descartado (total: {}).".format(
                        descartado["seq"], fila.descartados))
                if rede_disponivel():
                    esvaziar_fila()
                else:
                    print("[fila] Sem rede: pacote seq={} guardado ({} pendentes).".format(
                        pacote["seq"], len(fila)))
                    tentar_reconectar(agora)

            time.sleep_ms(INTERVALO_LEITURA_MS)
    except KeyboardInterrupt:
        print("\n[main] Interrompido pelo usuário.")
    finally:
        led_digital.value(0)
        led_pwm.duty(0)
        print("[main] Enviados: {} | Falhas: {} | Pendentes: {} | Descartados: {}".format(
            estado["enviados"], estado["falhas"], len(fila), fila.descartados))


if __name__ == "__main__":
    executar()
