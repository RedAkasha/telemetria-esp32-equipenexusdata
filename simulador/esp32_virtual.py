# ==============================================================================
# esp32_virtual.py — Roda o firmware REAL (firmware/boot.py + main.py) no
# Python do computador, com o hardware simulado.
#
# Nada do firmware é copiado ou reescrito: os arquivos .py da placa são
# executados como estão. Só os módulos que existem apenas no MicroPython são
# trocados por simulações, e só para o código do firmware:
#
#   machine    Pin (botões com IRQ, saída da trava), PWM (buzzer), SoftI2C
#   network    WLAN: conecta na hora; dá para "derrubar" o Wi-Fi
#   urequests  HTTP de verdade (urllib) para a API; dá para simular a rede fora
#              (cada requisição espera o timeout e falha, como no ESP32)
#   time       ticks_ms/ticks_diff/ticks_add/sleep_ms do MicroPython
#   gc         mem_free()
#   ssd1306    display que guarda o texto da tela
#   config     gerado a partir dos parâmetros (API_URL, DISPOSITIVO_ID...)
#
# Usado pelo teste end-to-end (e2e/) e pela demonstração sem placa:
#   python -m simulador --api http://localhost:5000
# ==============================================================================
import builtins
import importlib.util
import json as _json
import random
import threading
import time as _time
import types
import urllib.error
import urllib.request
from pathlib import Path

PASTA_FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"

GPIO_CONVENCIONAL = 32
GPIO_PRIORITARIO = 33
GPIO_BUZZER = 25


class ESP32Virtual:
    def __init__(self, api_url, dispositivo="esp32-virtual-01", pasta_estado=".",
                 latencia_s=0.02, eco=False, **config):
        self.api_url = api_url.rstrip("/")
        if not self.api_url.endswith("/leituras"):
            self.api_url += "/leituras"
        self.dispositivo = dispositivo
        self.pasta_estado = Path(pasta_estado)
        self.latencia_s = latencia_s       # atraso de cada requisição (Wi-Fi real)
        self.eco = eco                     # repetir o REPL do firmware no terminal
        self.config = dict(
            WIFI_SSID="rede-simulada", WIFI_SENHA="", API_URL=self.api_url,
            DISPOSITIVO_ID=dispositivo, ARQUIVO_TRAVA=str(self.pasta_estado / "trava.txt"))
        self.config.update(config)

        self.wifi_ok = True                # o roteador está ao alcance?
        self.rede_fora = False             # a API está inalcançável (timeout)?
        self.em_requisicao = False         # há um HTTP em andamento (loop bloqueado)?
        self.requisicao_desde = None       # (início, latência) do HTTP em andamento
        self.log = []                      # tudo que o firmware imprimiu
        self.requisicoes = []              # (metodo, url, status) de cada HTTP
        self.buzzer = []                   # (ticks, freq, ligado)
        self.tela_oled = []                # textos da última tela desenhada
        self.erro = None
        self._niveis = {}                  # GPIO -> nível lógico
        self._irqs = {}                    # GPIO -> função da interrupção
        self._t0 = _time.monotonic()
        self._ultimo_tick = None
        self.maior_pausa_ms = 0            # maior tempo sem o firmware olhar o relógio
        self._parar = False
        self._interrompido = False
        self._globais = None
        self._thread = None
        self._modulos = self._criar_modulos()

    # ======================================================== ciclo de vida
    def ligar(self, espera_s=10):
        """Executa boot.py e main.py numa thread e espera o loop principal."""
        self._thread = threading.Thread(target=self._executar, name="esp32-virtual", daemon=True)
        self._thread.start()
        limite = _time.monotonic() + espera_s
        while _time.monotonic() < limite:
            if self.erro:
                raise RuntimeError("firmware falhou ao iniciar") from self.erro
            g = self._globais or {}
            if "ultima_reconexao" in g:          # variável criada logo antes do loop
                return self
            _time.sleep(0.02)
        raise TimeoutError("o firmware não chegou ao loop principal")

    def desligar(self, espera_s=10):
        """Ctrl+C no firmware: ele sai do loop pelo caminho normal (finally)."""
        self._parar = True
        if self._thread:
            self._thread.join(espera_s)
            if self._thread.is_alive():
                raise TimeoutError("o firmware não parou (loop travado?)")

    def _executar(self):
        g = {"__name__": "__main__", "__builtins__": self._builtins()}
        self._globais = g
        try:
            for arquivo in ("boot.py", "main.py"):    # como na placa: mesmo escopo global
                caminho = PASTA_FIRMWARE / arquivo
                codigo = compile(caminho.read_text(encoding="utf-8"), str(caminho), "exec")
                exec(codigo, g)
        except BaseException as e:                     # noqa: BLE001 — registrar e sair
            if not isinstance(e, KeyboardInterrupt):
                self.erro = e
                self._print("[esp32-virtual] firmware parou com erro:", repr(e))

    # ======================================================== hardware
    def _definir_nivel(self, gpio, nivel):
        anterior = self._niveis.get(gpio, 1)
        self._niveis[gpio] = nivel
        if anterior == 1 and nivel == 0 and gpio in self._irqs:
            self._irqs[gpio](None)             # borda de descida -> interrupção

    def apertar(self, botao="convencional"):
        self._definir_nivel(self._gpio(botao), 0)

    def soltar(self, botao="convencional"):
        self._definir_nivel(self._gpio(botao), 1)

    @staticmethod
    def _gpio(botao):
        return GPIO_CONVENCIONAL if botao == "convencional" else GPIO_PRIORITARIO

    def tocar(self, botao="convencional", ms=120):
        """Toque rápido: aperta, segura `ms` e solta."""
        self.apertar(botao)
        _time.sleep(ms / 1000)
        self.soltar(botao)
        _time.sleep(0.1)                       # debounce da soltura

    def segurar(self, botao="convencional", ms=2300):
        self.tocar(botao, ms)

    # ======================================================== leitura do estado
    @property
    def globais(self):
        return self._globais or {}

    @property
    def estado(self):
        return self.globais.get("estado", {})

    @property
    def tela(self):
        """(destaque, linha do meio, linha de status) da última tela desenhada."""
        return self.globais.get("_tela_anterior")

    @property
    def travado(self):
        trava = self.globais.get("trava")
        return trava.travado if trava else None

    @property
    def alarme(self):
        return self.estado.get("alarme_ate") is not None

    def pendentes(self):
        fila = self.globais.get("fila_envio")
        return len(fila) if fila is not None else None

    def aguardar(self, condicao, timeout=15, descricao="condição"):
        limite = _time.monotonic() + timeout
        while _time.monotonic() < limite:
            if self.erro:
                raise RuntimeError("firmware parou com erro") from self.erro
            if condicao():
                return True
            _time.sleep(0.05)
        raise TimeoutError("tempo esgotado esperando: " + descricao)

    def zerar_pausas(self):
        self.maior_pausa_ms = 0
        self._ultimo_tick = None

    # ======================================================== simulações
    def _ticks_ms(self):
        agora = int((_time.monotonic() - self._t0) * 1000)
        if self._ultimo_tick is not None:
            self.maior_pausa_ms = max(self.maior_pausa_ms, agora - self._ultimo_tick)
        self._ultimo_tick = agora
        return agora

    def _sleep_ms(self, ms):
        if self._parar and not self._interrompido:
            self._interrompido = True
            raise KeyboardInterrupt
        _time.sleep(max(0, ms) / 1000)

    def _print(self, *args, **kw):
        texto = kw.get("sep", " ").join(str(a) for a in args)
        self.log.append(texto)
        if self.eco and kw.get("end", "\n") == "\n":
            print("[{}] {}".format(self.dispositivo, texto), flush=True)

    def _builtins(self):
        b = dict(vars(builtins))
        real_import = builtins.__import__
        modulos = self._modulos

        def importar(nome, globais=None, locais=None, lista=(), nivel=0):
            if nome in modulos:
                return modulos[nome]
            return real_import(nome, globais, locais, lista, nivel)
        b["__import__"] = importar
        b["print"] = self._print
        return b

    def _criar_modulos(self):
        esp = self
        m = {}

        # ---------------------------------------------------------- time
        t = types.ModuleType("time")
        t.ticks_ms = self._ticks_ms
        t.ticks_diff = lambda a, b: a - b
        t.ticks_add = lambda a, b: a + b
        t.sleep_ms = self._sleep_ms
        t.sleep = lambda s: self._sleep_ms(s * 1000)
        t.localtime = lambda *a: _time.gmtime(*a)     # após o NTP o ESP32 fica em UTC
        t.time = _time.time
        m["time"] = t

        # ---------------------------------------------------------- gc
        g = types.ModuleType("gc")
        g.collect = lambda: None
        g.mem_free = lambda: 95000 + random.randint(-3000, 3000)
        g.mem_alloc = lambda: 20000
        m["gc"] = g

        # ---------------------------------------------------------- machine
        mach = types.ModuleType("machine")

        class Pin:
            IN, OUT, PULL_UP, PULL_DOWN = 1, 3, 2, 1
            IRQ_FALLING, IRQ_RISING = 2, 1

            def __init__(self, gpio, modo=None, pull=None, value=None):
                self.gpio = gpio
                esp._niveis.setdefault(gpio, 1 if pull == Pin.PULL_UP else 0)
                if value is not None:
                    esp._niveis[gpio] = value

            def value(self, v=None):
                if v is None:
                    return esp._niveis.get(self.gpio, 0)
                esp._niveis[self.gpio] = 1 if v else 0

            def irq(self, trigger=None, handler=None, **_):
                esp._irqs[self.gpio] = handler

        class PWM:
            def __init__(self, pin, freq=1000, duty_u16=0):
                self._freq, self._duty = freq, 0

            def freq(self, f=None):
                if f is None:
                    return self._freq
                self._freq = f

            def duty_u16(self, d=None):
                if d is None:
                    return self._duty
                self._duty = d
                esp.buzzer.append((esp._ticks_sem_pausa(), self._freq, d > 0))
                del esp.buzzer[:-500]

            def deinit(self):
                pass

        class SoftI2C:
            def __init__(self, sda=None, scl=None, **_):
                pass

            def scan(self):
                return [0x3C]

        mach.Pin, mach.PWM, mach.SoftI2C = Pin, PWM, SoftI2C
        m["machine"] = mach

        # ---------------------------------------------------------- display
        oled = types.ModuleType("ssd1306")

        class SSD1306_I2C:
            def __init__(self, w, h, i2c, addr=0x3C):
                self._linhas = []

            def fill(self, c):
                if c == 0:
                    self._linhas = []

            def text(self, s, x, y, c=1):
                self._linhas.append(s)

            def hline(self, *a):
                pass

            def fill_rect(self, *a):
                pass

            def show(self):
                esp.tela_oled = list(self._linhas)

        oled.SSD1306_I2C = SSD1306_I2C
        m["ssd1306"] = oled

        fb = types.ModuleType("framebuf")
        fb.MONO_HLSB = 3

        class FrameBuffer:
            def __init__(self, *a):
                pass

            def fill(self, c):
                pass

            def text(self, *a):
                pass

            def pixel(self, *a):
                return 0
        fb.FrameBuffer = FrameBuffer
        m["framebuf"] = fb

        # ---------------------------------------------------------- network
        net = types.ModuleType("network")
        net.STA_IF = 0

        class WLAN:
            _ativa = False

            def __init__(self, interface=0):
                pass

            def active(self, v=None):
                if v is None:
                    return WLAN._ativa
                WLAN._ativa = bool(v)

            def connect(self, ssid, senha):
                pass

            def disconnect(self):
                pass

            def isconnected(self):
                return WLAN._ativa and esp.wifi_ok

            def ifconfig(self):
                return ("192.168.0.77", "255.255.255.0", "192.168.0.1", "8.8.8.8")

            def status(self, param=None):
                return -55 - random.randint(0, 10) if param == "rssi" else 1010

            def scan(self):
                return []
        net.WLAN = WLAN
        m["network"] = net

        # ---------------------------------------------------------- urequests
        req = types.ModuleType("urequests")
        abridor = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        class Resposta:
            def __init__(self, status, conteudo):
                self.status_code, self.content = status, conteudo

            @property
            def text(self):
                return self.content.decode("utf-8", "replace")

            def json(self):
                return _json.loads(self.content)

            def close(self):
                pass

        def requisitar(metodo, url, json=None, timeout=None, **_):
            esp.requisicao_desde = (_time.monotonic(), esp.latencia_s)
            esp.em_requisicao = True
            try:
                return _requisitar(metodo, url, json, timeout or 5)
            finally:
                esp.em_requisicao = False
                esp.requisicao_desde = None

        def _requisitar(metodo, url, json, timeout):
            if esp.rede_fora:                       # servidor inalcançável: espera e falha
                _time.sleep(timeout)
                esp.requisicoes.append((metodo, url, None))
                raise OSError(116, "ETIMEDOUT")
            _time.sleep(esp.latencia_s)
            dados = _json.dumps(json).encode() if json is not None else None
            r = urllib.request.Request(url, data=dados, method=metodo,
                                       headers={"Content-Type": "application/json"})
            try:
                with abridor.open(r, timeout=timeout) as resp:
                    resposta = Resposta(resp.status, resp.read())
            except urllib.error.HTTPError as e:
                resposta = Resposta(e.code, e.read())
            except (urllib.error.URLError, OSError) as e:
                esp.requisicoes.append((metodo, url, None))
                raise OSError(104, "ECONNRESET: {}".format(getattr(e, "reason", e)))
            esp.requisicoes.append((metodo, url, resposta.status_code))
            del esp.requisicoes[:-2000]
            return resposta

        req.get = lambda url, **kw: requisitar("GET", url, **kw)
        req.post = lambda url, **kw: requisitar("POST", url, **kw)
        m["urequests"] = m["requests"] = req

        ntp = types.ModuleType("ntptime")
        ntp.settime = lambda: None                  # o relógio do PC já está certo
        m["ntptime"] = ntp

        # ---------------------------------------------------------- config
        cfg = types.ModuleType("config")
        for chave, valor in self.config.items():
            setattr(cfg, chave, valor)
        m["config"] = cfg

        # ---------------------------------------------------------- estruturas
        spec = importlib.util.spec_from_file_location(
            "estruturas", PASTA_FIRMWARE / "estruturas.py")
        estr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(estr)
        m["estruturas"] = estr
        return m

    def _ticks_sem_pausa(self):
        return int((_time.monotonic() - self._t0) * 1000)
