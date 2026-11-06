# ==============================================================================
# teste_hardware.py — Diagnóstico do circuito, passo a passo
# NÃO precisa ser salvo na placa: abra no Thonny e clique em Executar (▶),
# ou no VS Code use "MicroPico: Run current file on Pico".
#
# Testa, nesta ordem:
#   1. Display OLED embutido
#   2. Buzzer
#   3. Botões (mostra qual GPIO muda quando você aperta cada botão)
#   4. Wi-Fi + envio de um pacote JSON de teste para o API_URL
# ==============================================================================
from machine import Pin, PWM, SoftI2C
import time
import network

PINO_BUZZER = 25
OLED_SDA, OLED_SCL = 5, 4
API_URL = "http://httpbin.org/post"
try:
    import config
    PINO_BUZZER = getattr(config, "PINO_BUZZER", PINO_BUZZER)
    OLED_SDA = getattr(config, "OLED_SDA", OLED_SDA)
    OLED_SCL = getattr(config, "OLED_SCL", OLED_SCL)
    API_URL = getattr(config, "API_URL", API_URL)
except ImportError:
    print("(config.py não está na placa — usando valores padrão)")


def titulo(texto):
    print("\n" + "=" * 60 + "\n" + texto + "\n" + "=" * 60)


# ------------------------------------------------------------------------------
titulo("1/4  DISPLAY OLED")
oled = None
try:
    import ssd1306
    for sda, scl in ((OLED_SDA, OLED_SCL), (OLED_SCL, OLED_SDA), (21, 22)):
        i2c = SoftI2C(sda=Pin(sda), scl=Pin(scl))
        achados = i2c.scan()
        print("SDA={:>2} SCL={:>2} -> dispositivos I2C: {}".format(
            sda, scl, [hex(a) for a in achados] or "nenhum"))
        if 0x3C in achados or 0x3D in achados:
            oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C if 0x3C in achados else 0x3D)
            oled.fill(0)
            oled.text("TESTE OK", 32, 20)
            oled.text("SDA={} SCL={}".format(sda, scl), 16, 40)
            oled.show()
            print("OK: o display deve mostrar 'TESTE OK'.")
            if (sda, scl) != (OLED_SDA, OLED_SCL):
                print("-> Coloque OLED_SDA = {} e OLED_SCL = {} no config.py".format(sda, scl))
            break
    if oled is None:
        print("FALHA: display não encontrado (o totem funciona sem ele).")
except ImportError:
    print("FALHA: ssd1306.py não está na placa. Envie o arquivo firmware/ssd1306.py.")

# ------------------------------------------------------------------------------
titulo("2/4  BUZZER (GPIO {})".format(PINO_BUZZER))
try:
    bz = PWM(Pin(PINO_BUZZER))
    for freq in (1000, 2000, 3000):
        bz.freq(freq)
        bz.duty_u16(32768)
        time.sleep_ms(200)
        bz.duty_u16(0)
        time.sleep_ms(150)
    bz.deinit()
    print("Você deve ter ouvido 3 bipes (grave, médio, agudo).")
    print("Sem som? Confira: perna + do buzzer no GPIO {} e perna - no trilho GND.".format(
        PINO_BUZZER))
except Exception as e:
    print("FALHA no buzzer:", e)

# ------------------------------------------------------------------------------
titulo("3/4  BOTÕES — aperte e solte cada botão nos próximos 20 s")
# GPIOs seguros para botão com PULL_UP interno (fora display, buzzer e USB)
candidatos = [13, 14, 16, 17, 18, 19, 21, 22, 23, 26, 27, 32, 33]
candidatos = [p for p in candidatos if p not in (PINO_BUZZER, OLED_SDA, OLED_SCL)]
pinos = {p: Pin(p, Pin.IN, Pin.PULL_UP) for p in candidatos}
anterior = {p: pin.value() for p, pin in pinos.items()}
presos = [p for p, v in anterior.items() if v == 0]
if presos:
    print("ATENÇÃO: GPIO {} já está em 0 (pressionado) sem ninguém apertar.".format(presos))
    print("Se um botão está nesse pino, use as pernas em DIAGONAL do botão.")
vistos = set()
fim = time.ticks_add(time.ticks_ms(), 20000)
while time.ticks_diff(fim, time.ticks_ms()) > 0:
    for p, pin in pinos.items():
        v = pin.value()
        if v != anterior[p]:
            anterior[p] = v
            vistos.add(p)
            print("GPIO {:>2}: {}".format(p, "PRESSIONADO" if v == 0 else "solto"))
    time.sleep_ms(20)
if vistos:
    print("Botões detectados nos GPIOs:", sorted(vistos))
    print("O main.py espera: azul (convencional) = 32, branco (prioritária) = 33.")
    print("Se for diferente, mude PINO_BOTAO_CONVENCIONAL/PRIORITARIO no config.py.")
else:
    print("FALHA: nenhum botão mudou. Confira:")
    print(" - fio do GND da placa até o trilho azul (-) da protoboard;")
    print(" - a outra perna do botão ligada no trilho azul (-);")
    print(" - os botões não podem estar nos pinos 34/35/36/39 (não têm pull-up).")

# ------------------------------------------------------------------------------
titulo("4/4  WI-FI + ENVIO JSON para " + API_URL)
wlan = network.WLAN(network.STA_IF)
if not wlan.isconnected():
    print("FALHA: Wi-Fi desconectado. Reinicie a placa (EN) e veja as mensagens [boot].")
else:
    print("Wi-Fi OK — IP do ESP32:", wlan.ifconfig()[0])
    try:
        import urequests
    except ImportError:
        import requests as urequests
    try:
        r = urequests.post(API_URL, json={
            "dispositivo": "teste_hardware", "seq": 0, "tipo": "teste"})
        print("Resposta do servidor:", r.status_code)
        r.close()
        print("OK: envio funcionando!" if 200 <= r.status_code < 300 else
              "Servidor respondeu com erro — veja o terminal do servidor.")
    except OSError as e:
        print("FALHA de rede:", e)
        print(" - O servidor_teste.py está rodando no notebook?")
        print(" - O IP do API_URL é o do adaptador Wi-Fi (ipconfig)?")
        print(" - Firewall do Windows liberado para Python (redes privadas E públicas)?")
        print(" - Redes de faculdade podem bloquear aparelho-aparelho: use o hotspot.")

print("\nDiagnóstico concluído.")
