# ==============================================================================
# main.py — Controle de periféricos e telemetria serial (ESP32 / MicroPython)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
# Executado automaticamente após o boot.py.
# ==============================================================================
from machine import Pin, ADC, PWM
import time

# ------------------------------------------------------------------------------
# 1. Mapeamento dos pinos
# ------------------------------------------------------------------------------
led_digital = Pin(2, Pin.OUT)                 # LED onboard
led_pwm = PWM(Pin(4), freq=5000)              # LED com brilho controlado
botao = Pin(15, Pin.IN, Pin.PULL_UP)          # 0 = pressionado, 1 = solto
potenciometro = ADC(Pin(34))                  # ADC1_CH6
potenciometro.atten(ADC.ATTN_11DB)            # faixa 0–3,3 V (0–4095)

print("-" * 66)
print("Sistema MicroPython inicializado no ESP32")
print("Botão alterna o LED digital | potenciômetro ajusta o LED PWM")
print("-" * 66)

# ------------------------------------------------------------------------------
# 2. Loop principal
# ------------------------------------------------------------------------------
contador = 0
while True:
    contador += 1

    # Leitura digital (PULL_UP: 0 = pressionado)
    if botao.value() == 0:
        led_digital.value(1)
        status_botao = "PRESSIONADO [LED LIGADO]"
    else:
        led_digital.value(0)
        status_botao = "SOLTO [LED DESLIGADO]"

    # Leitura analógica (12 bits) e conversão para volts
    valor_adc = potenciometro.read()
    tensao = (valor_adc / 4095.0) * 3.3

    # Mapeia ADC (0–4095) para duty do PWM (0–1023)
    duty = valor_adc // 4
    led_pwm.duty(duty)

    print(f"[{contador:04d}] Botão: {status_botao:<25} | "
          f"ADC: {valor_adc:04d} ({tensao:.2f}V) | "
          f"Duty PWM: {duty:04d}/1023")

    time.sleep(0.3)
