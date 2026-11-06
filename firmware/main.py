# ==============================================================================
# main.py — Totem de senhas ByteBank Queue (ESP32 / MicroPython)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
# EQUIPE: NEXUS Data — Módulo 2 (Totem de Atendimento & Gestão de Filas)
#
# Executado automaticamente após o boot.py (que já conectou o Wi-Fi).
#
# Hardware:
#   Sensores  -> Botão AZUL  (senha CONVENCIONAL)   GPIO 32
#                Botão BRANCO (senha PRIORITÁRIA)   GPIO 33
#   Atuadores -> Buzzer (bipe de confirmação)       GPIO 25
#                Display OLED embutido (I2C)        SDA 5 / SCL 4
#
# Uso:
#   - Toque rápido no botão  -> emite uma senha (N001, N002... ou P001...)
#   - Segurar um botão 2 s   -> cancela a ÚLTIMA senha emitida (pop da pilha)
#
# Estruturas de dados (estruturas.py):
#   FilaContingencia  todo pacote JSON entra na fila; só sai depois que o
#                     servidor confirma. Sem rede, os pacotes ficam guardados.
#   PilhaHistorico    últimas senhas emitidas; topo = mais recente.
#   JanelaDeslizante  média móvel do intervalo entre senhas (ritmo de chegada).
#
# Arquivos na placa: boot.py, main.py, estruturas.py, ssd1306.py, config.py
# ==============================================================================
from machine import Pin, PWM, SoftI2C
import network
import time
import gc

from estruturas import FilaContingencia, PilhaHistorico, JanelaDeslizante

# urequests vem embutido no firmware oficial do ESP32 (também como "requests")
try:
    import urequests
except ImportError:
    import requests as urequests

# ------------------------------------------------------------------------------
# 0. Configuração (valores do config.py substituem estes padrões)
# ------------------------------------------------------------------------------
API_URL = "http://httpbin.org/post"     # servidor público de teste (padrão)
DISPOSITIVO_ID = "esp32-nexus-01"
PINO_BOTAO_CONVENCIONAL = 32
PINO_BOTAO_PRIORITARIO = 33
PINO_BUZZER = 25
OLED_SDA = 5
OLED_SCL = 4

try:
    import config                       # ATENÇÃO: "import config", nunca "firmware.config"
    API_URL = getattr(config, "API_URL", API_URL)
    DISPOSITIVO_ID = getattr(config, "DISPOSITIVO_ID", DISPOSITIVO_ID)
    PINO_BOTAO_CONVENCIONAL = getattr(config, "PINO_BOTAO_CONVENCIONAL", PINO_BOTAO_CONVENCIONAL)
    PINO_BOTAO_PRIORITARIO = getattr(config, "PINO_BOTAO_PRIORITARIO", PINO_BOTAO_PRIORITARIO)
    PINO_BUZZER = getattr(config, "PINO_BUZZER", PINO_BUZZER)
    OLED_SDA = getattr(config, "OLED_SDA", OLED_SDA)
    OLED_SCL = getattr(config, "OLED_SCL", OLED_SCL)
except ImportError:
    print("[main] config.py não encontrado — enviando para", API_URL)

INTERVALO_LOOP_MS = 20          # leitura dos botões a cada 20 ms
DEBOUNCE_MS = 40                # filtro da trepidação mecânica do botão
PRESSAO_LONGA_MS = 2000         # segurar 2 s = cancelar última senha
INTERVALO_STATUS_MS = 15000     # pacote de status (telemetria) a cada 15 s
INTERVALO_REENVIO_MS = 5000     # nova tentativa de esvaziar a fila
INTERVALO_RECONEXAO_MS = 10000  # nova tentativa de reconectar o Wi-Fi
TIMEOUT_HTTP_S = 3
MAX_ENVIOS_POR_CICLO = 5

CAPACIDADE_FILA = 50            # pacotes guardados sem rede
CAPACIDADE_HISTORICO = 10       # senhas na pilha de histórico
TAMANHO_JANELA = 5              # intervalos usados na média móvel


# ------------------------------------------------------------------------------
# 1. Periféricos
# ------------------------------------------------------------------------------
class Botao:
    """Botão com PULL_UP interno, debounce e detecção de toque curto/longo.

    atualizar() devolve um evento por vez:
      "curto"   ao SOLTAR depois de um toque rápido
      "longo"   ao SOLTAR depois de segurar >= PRESSAO_LONGA_MS
      "aviso"   uma vez, no instante em que o toque vira longo (para bipar)
      None      nada aconteceu
    """

    def __init__(self, pino, nome):
        self.pin = Pin(pino, Pin.IN, Pin.PULL_UP)   # solto = 1, pressionado = 0
        self.pino = pino
        self.nome = nome
        self._estavel = False
        self._bruto = False
        self._t_mudanca = time.ticks_ms()
        self._t_press = 0
        self._avisou = False

    def pressionado(self):
        return self.pin.value() == 0

    def atualizar(self, agora):
        bruto = self.pressionado()
        if bruto != self._bruto:                    # mudou: espera estabilizar
            self._bruto = bruto
            self._t_mudanca = agora
            return None
        if bruto != self._estavel and time.ticks_diff(agora, self._t_mudanca) >= DEBOUNCE_MS:
            self._estavel = bruto
            if bruto:                               # acabou de apertar
                self._t_press = agora
                self._avisou = False
                return None
            duracao = time.ticks_diff(agora, self._t_press)   # acabou de soltar
            return "longo" if duracao >= PRESSAO_LONGA_MS else "curto"
        if self._estavel and not self._avisou and \
                time.ticks_diff(agora, self._t_press) >= PRESSAO_LONGA_MS:
            self._avisou = True
            return "aviso"
        return None


class Buzzer:
    """Buzzer acionado por PWM (funciona com buzzer ativo ou passivo)."""

    def __init__(self, pino):
        self.ok = False
        try:
            self.pwm = PWM(Pin(pino))
            self.pwm.freq(2000)
            self._volume(0)
            self.ok = True
        except (ValueError, OSError) as e:
            print("[buzzer] ERRO no GPIO {}: {} — seguindo sem som.".format(pino, e))

    def _volume(self, fracao):
        try:
            self.pwm.duty_u16(int(fracao * 65535))
        except AttributeError:                      # firmwares antigos
            self.pwm.duty(int(fracao * 1023))

    def bip(self, ms=80, freq=2000, vezes=1, pausa_ms=80):
        if not self.ok:
            return
        for i in range(vezes):
            self.pwm.freq(freq)
            self._volume(0.5)
            time.sleep_ms(ms)
            self._volume(0)
            if i < vezes - 1:
                time.sleep_ms(pausa_ms)

    def desligar(self):
        if self.ok:
            self._volume(0)
            self.pwm.deinit()


class Display:
    """OLED SSD1306 128x64. Se não for encontrado, o programa segue sem tela."""

    def __init__(self, sda, scl):
        self.oled = None
        try:
            import ssd1306
        except ImportError:
            print("[oled] ssd1306.py não está na placa — display desativado.")
            return
        # Tenta os pinos configurados, depois invertidos, depois o padrão 21/22
        for p_sda, p_scl in ((sda, scl), (scl, sda), (21, 22)):
            try:
                i2c = SoftI2C(sda=Pin(p_sda), scl=Pin(p_scl))
                enderecos = i2c.scan()
                for addr in (0x3C, 0x3D):
                    if addr in enderecos:
                        self.oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=addr)
                        print("[oled] Display encontrado: SDA={} SCL={} endereço {}".format(
                            p_sda, p_scl, hex(addr)))
                        return
            except Exception:
                pass
        print("[oled] Display não encontrado — seguindo sem tela.")

    def _texto_grande(self, texto, x, y, escala):
        """Desenha texto ampliado (a fonte nativa tem só 8x8 pixels)."""
        import framebuf
        buf = bytearray(8)
        letra = framebuf.FrameBuffer(buf, 8, 8, framebuf.MONO_HLSB)
        for c in texto:
            letra.fill(0)
            letra.text(c, 0, 0, 1)
            for yy in range(8):
                for xx in range(8):
                    if letra.pixel(xx, yy):
                        self.oled.fill_rect(x + xx * escala, y + yy * escala, escala, escala, 1)
            x += 8 * escala

    def mostrar(self, senha, linha_meio, linha_status):
        if self.oled is None:
            return
        o = self.oled
        o.fill(0)
        o.text("BYTEBANK QUEUE", 8, 0)
        o.hline(0, 10, 128, 1)
        if senha:
            self._texto_grande(senha, (128 - len(senha) * 24) // 2, 14, 3)
        else:
            o.text("Aperte um botao", 4, 24)
        o.text(linha_meio[:16], 0, 44)
        o.text(linha_status[:16], 0, 56)
        o.show()


botao_conv = Botao(PINO_BOTAO_CONVENCIONAL, "convencional")
botao_prio = Botao(PINO_BOTAO_PRIORITARIO, "prioritaria")
buzzer = Buzzer(PINO_BUZZER)
tela = Display(OLED_SDA, OLED_SCL)
wlan = network.WLAN(network.STA_IF)

# ------------------------------------------------------------------------------
# 2. Estruturas de dados em memória
# ------------------------------------------------------------------------------
fila_envio = FilaContingencia(CAPACIDADE_FILA)
historico = PilhaHistorico(CAPACIDADE_HISTORICO)
janela_intervalos = JanelaDeslizante(TAMANHO_JANELA)

estado = {
    "seq": 0,                   # nº sequencial de pacote (o servidor detecta perdas)
    "convencional": 0,          # contador de senhas N
    "prioritaria": 0,           # contador de senhas P
    "canceladas": 0,
    "enviados": 0,
    "falhas_envio": 0,
    "ultima_emissao": None,     # ticks da última senha (para a janela deslizante)
    "mensagem": "",             # linha do meio do display
}


# ------------------------------------------------------------------------------
# 3. Funções auxiliares
# ------------------------------------------------------------------------------
def sincronizar_relogio():
    """Acerta o relógio via NTP para gerar timestamps reais (UTC)."""
    if not wlan.isconnected():
        return
    try:
        import ntptime
        ntptime.settime()
        print("[main] Relógio sincronizado via NTP (UTC).")
    except Exception as e:
        print("[main] Aviso: NTP indisponível ({}). Usando uptime.".format(e))


def timestamp_iso():
    t = time.localtime()
    if t[0] < 2024:             # relógio ainda não sincronizado
        return None
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(*t[:6])


def novo_pacote(tipo, **campos):
    """Cabeçalho comum + campos do evento. O timestamp é o da COLETA."""
    estado["seq"] += 1
    pacote = {
        "dispositivo": DISPOSITIVO_ID,
        "seq": estado["seq"],
        "tipo": tipo,
        "timestamp": timestamp_iso(),
        "uptime_ms": time.ticks_ms(),
    }
    pacote.update(campos)
    return pacote


def post_json(pacote):
    """urequests.post() de um pacote. True se o servidor respondeu 2xx."""
    resposta = None
    try:
        try:
            resposta = urequests.post(API_URL, json=pacote, timeout=TIMEOUT_HTTP_S)
        except TypeError:       # urequests antigo, sem parâmetro timeout
            resposta = urequests.post(API_URL, json=pacote)
        if 200 <= resposta.status_code < 300:
            return True
        print("[http] Servidor respondeu", resposta.status_code)
        return False
    except OSError as e:
        print("[http] Falha de rede:", e)
        return False
    finally:
        if resposta is not None:
            resposta.close()    # libera o socket (o ESP32 tem poucos)
        gc.collect()


def esvaziar_fila():
    """Envia pendentes em ordem FIFO: espiar -> POST -> só então desenfileirar."""
    if not wlan.isconnected():
        return 0
    enviados = 0
    while not fila_envio.vazia() and enviados < MAX_ENVIOS_POR_CICLO:
        if post_json(fila_envio.espiar()):
            fila_envio.desenfileirar()
            enviados += 1
            estado["enviados"] += 1
        else:
            estado["falhas_envio"] += 1
            break               # mantém a ordem: tenta de novo mais tarde
    if enviados:
        print("[http] {} pacote(s) enviados | pendentes na fila: {}".format(
            enviados, len(fila_envio)))
    return enviados


def registrar(pacote):
    """Coloca o pacote na fila de contingência e tenta enviar."""
    if fila_envio.enfileirar(pacote):
        print("[fila] Cheia — pacote mais antigo descartado.")
    esvaziar_fila()


def emitir_senha(categoria):
    agora = time.ticks_ms()
    estado[categoria] += 1
    prefixo = "N" if categoria == "convencional" else "P"
    senha = "{}{:03d}".format(prefixo, estado[categoria] % 1000)

    # Janela deslizante: intervalo desde a senha anterior (em segundos)
    if estado["ultima_emissao"] is not None:
        janela_intervalos.adicionar(time.ticks_diff(agora, estado["ultima_emissao"]) / 1000)
    estado["ultima_emissao"] = agora

    historico.push(senha)       # Pilha: a nova senha vai para o topo
    anteriores = historico.ultimos(4)[1:]          # as 3 abaixo do topo
    estado["mensagem"] = ("Ult: " + " ".join(anteriores)) if anteriores else ""

    if categoria == "prioritaria":
        buzzer.bip(70, 2500, vezes=2)
    else:
        buzzer.bip(120, 2000)
    print("[senha] {} emitida ({})".format(senha, categoria))
    atualizar_tela()            # mostra a senha antes do envio pela rede
    registrar(novo_pacote("senha_emitida", senha=senha, categoria=categoria))


def cancelar_ultima():
    senha = historico.pop()     # Pilha LIFO: sai a mais recente
    if senha is None:
        buzzer.bip(300, 400)
        estado["mensagem"] = "Nada a cancelar"
        print("[senha] Nada para cancelar.")
        atualizar_tela()
        return
    estado["canceladas"] += 1
    estado["mensagem"] = "Cancelada " + senha
    buzzer.bip(150, 800)
    buzzer.bip(250, 500)
    print("[senha] {} cancelada (pop da pilha).".format(senha))
    atualizar_tela()
    registrar(novo_pacote("senha_cancelada", senha=senha))


def enviar_status():
    """Telemetria periódica do totem."""
    rssi = None
    if wlan.isconnected():
        try:
            rssi = wlan.status("rssi")
        except Exception:
            pass
    media = janela_intervalos.media() if len(janela_intervalos) else None
    pacote = novo_pacote(
        "status",
        total_convencional=estado["convencional"],
        total_prioritaria=estado["prioritaria"],
        canceladas=estado["canceladas"],
        intervalo_medio_s=round(media, 1) if media is not None else None,
        historico=historico.ultimos(5),
        fila_pendentes=len(fila_envio),
        descartados=fila_envio.descartados,
        rssi_dbm=rssi,
        mem_livre=gc.mem_free(),
    )
    print("[status] N={} P={} canceladas={} | intervalo médio={} s | fila={} | Wi-Fi={}".format(
        estado["convencional"], estado["prioritaria"], estado["canceladas"],
        pacote["intervalo_medio_s"], len(fila_envio),
        "OK" if wlan.isconnected() else "SEM REDE"))
    registrar(pacote)


_tela_anterior = None


def atualizar_tela():
    """Redesenha o OLED só quando algo mudou."""
    global _tela_anterior
    rede = "WiFi ok" if wlan.isconnected() else "SEM WiFi"
    status = "{} F:{}".format(rede, len(fila_envio))
    conteudo = (historico.topo(), estado["mensagem"], status)
    if conteudo != _tela_anterior:
        _tela_anterior = conteudo
        tela.mostrar(*conteudo)


def verificar_wifi(ultima_tentativa, agora):
    """Dispara uma reconexão (sem bloquear) se o Wi-Fi caiu."""
    if wlan.isconnected() or time.ticks_diff(agora, ultima_tentativa) < INTERVALO_RECONEXAO_MS:
        return ultima_tentativa
    print("[wifi] Desconectado — tentando reconectar...")
    try:
        try:
            from config import WIFI_SSID, WIFI_SENHA
        except ImportError:
            WIFI_SSID, WIFI_SENHA = "Wokwi-GUEST", ""
        wlan.active(True)
        wlan.connect(WIFI_SSID, WIFI_SENHA)
    except OSError as e:
        print("[wifi] Erro ao reconectar:", e)
    return agora


def desligar():
    buzzer.desligar()
    if tela.oled is not None:
        tela.oled.fill(0)
        tela.oled.text("Totem parado", 16, 28)
        tela.oled.show()


# ------------------------------------------------------------------------------
# 4. Inicialização
# ------------------------------------------------------------------------------
print("-" * 70)
print("NEXUS Data — Totem ByteBank Queue")
print("Dispositivo:", DISPOSITIVO_ID)
print("Destino:    ", API_URL)
print("Botões:      convencional GPIO {} | prioritária GPIO {} | buzzer GPIO {}".format(
    PINO_BOTAO_CONVENCIONAL, PINO_BOTAO_PRIORITARIO, PINO_BUZZER))
print("Wi-Fi:      ", ("conectado " + wlan.ifconfig()[0]) if wlan.isconnected()
      else "DESCONECTADO (pacotes irão para a fila)")
print("-" * 70)

for botao in (botao_conv, botao_prio):
    if botao.pressionado():
        print("[aviso] Botão {} (GPIO {}) lê 'pressionado' sem ninguém apertar.".format(
            botao.nome, botao.pino))
        print("        Provável causa: as duas pernas usadas já são ligadas entre si.")
        print("        Use pernas em DIAGONAL do botão (veja docs/passo_a_passo.md).")

sincronizar_relogio()
buzzer.bip(60, 1500)
buzzer.bip(60, 2500)
atualizar_tela()
registrar(novo_pacote("inicio", ip=wlan.ifconfig()[0] if wlan.isconnected() else None))

# ------------------------------------------------------------------------------
# 5. Loop principal
# ------------------------------------------------------------------------------
agora = time.ticks_ms()
ultimo_status = agora
ultimo_reenvio = agora
ultima_reconexao = agora

try:
    while True:
        agora = time.ticks_ms()

        for botao in (botao_conv, botao_prio):
            evento = botao.atualizar(agora)
            if evento == "curto":
                emitir_senha(botao.nome)
            elif evento == "aviso":
                buzzer.bip(40, 1000)        # avisa: ao soltar, vai cancelar
            elif evento == "longo":
                cancelar_ultima()

        if time.ticks_diff(agora, ultimo_status) >= INTERVALO_STATUS_MS:
            ultimo_status = agora
            enviar_status()

        if not fila_envio.vazia() and time.ticks_diff(agora, ultimo_reenvio) >= INTERVALO_REENVIO_MS:
            ultimo_reenvio = agora
            esvaziar_fila()

        ultima_reconexao = verificar_wifi(ultima_reconexao, agora)
        atualizar_tela()

        gasto = time.ticks_diff(time.ticks_ms(), agora)
        if gasto < INTERVALO_LOOP_MS:
            time.sleep_ms(INTERVALO_LOOP_MS - gasto)

except KeyboardInterrupt:
    print("\n[main] Interrompido pelo usuário (Ctrl+C).")
finally:
    desligar()
    print("[main] Resumo: {} N + {} P emitidas | {} canceladas | {} pacotes enviados | "
          "{} na fila | {} descartados | {} falhas de envio".format(
              estado["convencional"], estado["prioritaria"], estado["canceladas"],
              estado["enviados"], len(fila_envio), fila_envio.descartados,
              estado["falhas_envio"]))
