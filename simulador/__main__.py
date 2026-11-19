# ==============================================================================
# Demonstração sem placa: o firmware real conversando com a API e o dashboard.
#
#   python -m simulador                          # API em http://localhost:5000
#   python -m simulador --api http://192.168.0.10:5000 --auto 4
#
# Teclas (digite e tecle Enter):
#   n  senha convencional     p  senha prioritária     c  cancela a última
#   w  derruba/religa o Wi-Fi s  mostra o estado        q  desliga o totem
# Com --auto N, um "cliente" aperta um botão a cada ~N segundos.
# ==============================================================================
import argparse
import random
import tempfile
import threading
import time

from .esp32_virtual import ESP32Virtual


def main():
    ap = argparse.ArgumentParser(description="ESP32 virtual do Totem ByteBank Queue")
    ap.add_argument("--api", default="http://localhost:5000", help="endereço da API")
    ap.add_argument("--dispositivo", default="esp32-virtual-01")
    ap.add_argument("--auto", type=float, default=0,
                    help="aperta um botão sozinho a cada ~N s (0 = desligado)")
    args = ap.parse_args()

    pasta = tempfile.mkdtemp(prefix="esp32-virtual-")
    esp = ESP32Virtual(args.api, args.dispositivo, pasta_estado=pasta, eco=True)
    esp.ligar()
    print("\nTotem virtual no ar. Teclas: n, p, c, w, s, q (+ Enter)\n")

    parar = threading.Event()

    def cliente_automatico():
        while not parar.wait(random.uniform(0.5, 1.5) * args.auto):
            esp.tocar("prioritaria" if random.random() < 0.3 else "convencional")
    if args.auto:
        threading.Thread(target=cliente_automatico, daemon=True).start()

    try:
        while True:
            tecla = input().strip().lower()
            if tecla == "n":
                esp.tocar("convencional")
            elif tecla == "p":
                esp.tocar("prioritaria")
            elif tecla == "c":
                esp.segurar("convencional")
            elif tecla == "w":
                esp.wifi_ok = not esp.wifi_ok
                print("Wi-Fi", "religado" if esp.wifi_ok else "DERRUBADO")
            elif tecla == "s":
                print("tela:", esp.tela, "| travado:", esp.travado, "| alarme:", esp.alarme,
                      "| fila:", esp.pendentes())
            elif tecla == "q":
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        parar.set()
        esp.desligar()
        time.sleep(0.1)


if __name__ == "__main__":
    main()
