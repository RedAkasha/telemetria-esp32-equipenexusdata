# Inicia a API:  cd backend  &&  python -m api [porta]
import socket
import sys

import uvicorn


def ip_local():
    """Descobre o IP do computador na rede local (não envia nada pela rede)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print("=" * 70)
    print("API NEXUS Data no ar")
    print('No config.py do ESP32 use:  API_URL = "http://{}:{}/leituras"'.format(ip_local(), porta))
    print("Documentação interativa:    http://localhost:{}/docs".format(porta))
    print("=" * 70)
    uvicorn.run("api.main:app", host="0.0.0.0", port=porta)
