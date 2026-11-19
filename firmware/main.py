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
# Comandos do dashboard (o ESP32 consulta a API a cada 2 s):
#   travar / liberar_trava   -> bloqueia ou libera a emissão de senhas
#                               (e o relé/LED do PINO_TRAVA, se configurado)
#   acionar_alarme           -> sirene no buzzer por N s, sem travar o loop
#   silenciar_alarme         -> interrompe a sirene
#   resetar_contador         -> zera contadores, histórico e janela; nova sessão
#   Cada comando é confirmado ao servidor (executado/falhou). A confirmação
#   também passa por uma fila FIFO e é reenviada se a rede falhar.
#
# Estruturas de dados (estruturas.py):
#   FilaContingencia  todo pacote JSON entra na fila; só sai depois que o
#                     servidor confirma. Sem rede, os pacotes ficam guardados.
#   PilhaHistorico    últimas senhas emitidas; topo = mais recente.
#   JanelaDeslizante  média móvel do intervalo entre senhas (ritmo de chegada).
#   fila_confirmacoes FilaContingencia com as confirmações de comandos.
#   executados        PilhaHistorico com os últimos comandos executados: um
#                     comando reentregue não é executado duas vezes.
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
PINO_TRAVA = None               # GPIO de um relé/LED da trava (None = só lógica)
ARQUIVO_TRAVA = "trava.txt"     # estado da trava sobrevive a um reboot
COMANDOS = True                 # consultar comandos do dashboard

INTERVALO_LOOP_MS = 20          # leitura dos botões a cada 20 ms
DEBOUNCE_MS = 40                # filtro da trepidação mecânica do botão
PRESSAO_LONGA_MS = 2000         # segurar 2 s = cancelar última senha
INTERVALO_STATUS_MS = 15000     # pacote de status (telemetria) a cada 15 s
INTERVALO_REENVIO_MS = 5000     # nova tentativa de esvaziar a fila
INTERVALO_RECONEXAO_MS = 10000  # nova tentativa de reconectar o Wi-Fi
INTERVALO_COMANDOS_MS = 2000    # consulta de comandos do dashboard
ESPERA_MAXIMA_COMANDOS_MS = 30000   # teto do recuo exponencial sem servidor
TIMEOUT_HTTP_S = 3
MAX_ENVIOS_POR_CICLO = 5

CAPACIDADE_FILA = 50            # pacotes guardados sem rede
CAPACIDADE_HISTORICO = 10       # senhas na pilha de histórico
TAMANHO_JANELA = 5              # intervalos usados na média móvel
CAPACIDADE_CONFIRMACOES = 10    # confirmações de comando aguardando envio
CAPACIDADE_EXECUTADOS = 8       # comandos lembrados para não executar duas vezes

_AJUSTAVEIS = ("API_URL", "DISPOSITIVO_ID", "PINO_BOTAO_CONVENCIONAL", "PINO_BOTAO_PRIORITARIO",
               "PINO_BUZZER", "OLED_SDA", "OLED_SCL", "PINO_TRAVA", "ARQUIVO_TRAVA", "COMANDOS",
               "INTERVALO_STATUS_MS", "INTERVALO_COMANDOS_MS", "TIMEOUT_HTTP_S")
try:
    import config                       # ATENÇÃO: "import config", nunca "firmware.config"
    for _nome in _AJUSTAVEIS:
        if hasattr(config, _nome):
            globals()[_nome] = getattr(config, _nome)
except ImportError:
    print("[main] config.py não encontrado — enviando para", API_URL)

# Rotas de comando: derivadas do API_URL ("http://ip:5000/leituras" -> "http://ip:5000").
# Com o httpbin (padrão do Wokwi) não há para onde perguntar: comandos desligados.
URL_BASE = API_URL[:-len("/leituras")] if API_URL.endswith("/leituras") else None
if COMANDOS and URL_BASE is None:
    print("[main] API_URL não termina em /leituras — comandos do dashboard desligados.")
COMANDOS = bool(COMANDOS and URL_BASE)
if COMANDOS:
    URL_PROXIMO_COMANDO = "{}/dispositivos/{}/comandos/proximo".format(URL_BASE, DISPOSITIVO_ID)
    URL_CONFIRMACAO = URL_BASE + "/comandos/{}/confirmacao"


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

    Toque durante uma requisição HTTP: o urequests bloqueia o loop por até
    TIMEOUT_HTTP_S e um toque rápido (~100 ms) inteiro passaria despercebido.
    Uma interrupção (IRQ) na borda de descida marca o toque; se o loop volta e
    o botão já está solto, o toque é contado mesmo assim. Só vale quando o loop
    de fato ficou parado: com o loop rodando a cada 20 ms, um toque real seria
    visto pela leitura normal, e uma borda isolada é ruído (o debounce filtra).
    """

    def __init__(self, pino, nome):
        self.pin = Pin(pino, Pin.IN, Pin.PULL_UP)   # solto = 1, pressionado = 0
        self.pino = pino
        self.nome = nome
        self._estavel = False
        self._bruto = False
        self._t_mudanca = time.ticks_ms()
        self._t_press = 0
        self._t_solto = self._t_mudanca
        self._avisou = False
        self._borda = False                         # IRQ viu o botão descer
        self._t_borda = 0
        self._t_leitura = self._t_mudanca           # última chamada de atualizar()
        self._encoberto = False                     # loop parado depois da borda
        try:
            self.pin.irq(trigger=Pin.IRQ_FALLING, handler=self._na_borda)
        except (AttributeError, ValueError, OSError, TypeError):
            print("[botao] GPIO {} sem IRQ — só leitura no loop.".format(pino))

    def _na_borda(self, _pin):
        # Rotina de interrupção: só anota (nada de alocar memória ou imprimir)
        self._t_borda = time.ticks_ms()
        self._borda = True

    def pressionado(self):
        return self.pin.value() == 0

    def atualizar(self, agora):
        if self._borda and time.ticks_diff(agora, self._t_leitura) > 2 * DEBOUNCE_MS:
            self._encoberto = True      # houve borda e o loop ficou parado: toque escondido
        self._t_leitura = agora
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
                self._borda = self._encoberto = False   # o loop viu este toque
                return None
            self._t_solto = agora
            duracao = time.ticks_diff(agora, self._t_press)   # acabou de soltar
            return "longo" if duracao >= PRESSAO_LONGA_MS else "curto"
        if self._borda and not bruto and not self._estavel:
            encoberto = self._encoberto
            self._borda = self._encoberto = False
            # Borda DEPOIS da última soltura = toque inteiro enquanto o loop
            # esperava a rede. Borda antes = trepidação da própria soltura.
            if encoberto and time.ticks_diff(self._t_borda, self._t_solto) > 0:
                self._t_solto = agora
                return "curto"
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

    def tocar(self, freq):
        """Som contínuo (não bloqueia): usado pela sirene do alarme."""
        if self.ok:
            self.pwm.freq(freq)
            self._volume(0.5)

    def silenciar(self):
        if self.ok:
            self._volume(0)

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
            # Senha (4 letras) em escala 3; avisos maiores ("TRAVADO") em escala 2
            escala = 3 if len(senha) <= 5 else 2
            self._texto_grande(senha, (128 - len(senha) * 8 * escala) // 2,
                               14 if escala == 3 else 18, escala)
        else:
            o.text("Aperte um botao", 4, 24)
        o.text(linha_meio[:16], 0, 44)
        o.text(linha_status[:16], 0, 56)
        o.show()


class Trava:
    """Trava lógica do totem; aciona um relé/LED se PINO_TRAVA for configurado.

    Travado: os botões não emitem nem cancelam senhas. O estado é gravado na
    flash (ARQUIVO_TRAVA) para um totem travado continuar travado após reboot.
    Saída do pino: 1 = liberado (relé energizado abre a fechadura), 0 = travado.
    """

    def __init__(self, pino, arquivo):
        self.arquivo = arquivo
        self.pin = None
        if pino is not None:
            try:
                self.pin = Pin(pino, Pin.OUT)
            except (ValueError, OSError) as e:
                print("[trava] ERRO no GPIO {}: {} — trava só lógica.".format(pino, e))
        self.travado = False
        try:
            with open(arquivo) as f:
                self.travado = f.read().strip() == "1"
        except OSError:
            pass                                    # primeira vez: liberado
        self._aplicar()

    def _aplicar(self):
        if self.pin is not None:
            self.pin.value(0 if self.travado else 1)

    def definir(self, travado):
        self.travado = travado
        self._aplicar()
        try:
            with open(self.arquivo, "w") as f:
                f.write("1" if travado else "0")
        except OSError as e:
            print("[trava] Aviso: estado não gravado na flash:", e)


botao_conv = Botao(PINO_BOTAO_CONVENCIONAL, "convencional")
botao_prio = Botao(PINO_BOTAO_PRIORITARIO, "prioritaria")
buzzer = Buzzer(PINO_BUZZER)
tela = Display(OLED_SDA, OLED_SCL)
trava = Trava(PINO_TRAVA, ARQUIVO_TRAVA)
wlan = network.WLAN(network.STA_IF)

# ------------------------------------------------------------------------------
# 2. Estruturas de dados em memória
# ------------------------------------------------------------------------------
fila_envio = FilaContingencia(CAPACIDADE_FILA)
historico = PilhaHistorico(CAPACIDADE_HISTORICO)
janela_intervalos = JanelaDeslizante(TAMANHO_JANELA)
fila_confirmacoes = FilaContingencia(CAPACIDADE_CONFIRMACOES)
executados = PilhaHistorico(CAPACIDADE_EXECUTADOS)     # (id, resultado, detalhe)

estado = {
    "seq": 0,                   # nº sequencial de pacote (o servidor detecta perdas)
    "convencional": 0,          # contador de senhas N
    "prioritaria": 0,           # contador de senhas P
    "canceladas": 0,
    "enviados": 0,
    "falhas_envio": 0,
    "rejeitados": 0,            # pacotes recusados pelo servidor (4xx): descartados
    "ultima_emissao": None,     # ticks da última senha (para a janela deslizante)
    "mensagem": "",             # linha do meio do display
    "alarme_ate": None,         # ticks em que a sirene para (None = desligada)
    "alarme_troca": 0,          # ticks da próxima troca de tom da sirene
    "alarme_tom": 0,
    "proxima_consulta": 0,      # ticks da próxima consulta de comandos
    "espera_comandos": INTERVALO_COMANDOS_MS,   # cresce sem servidor (recuo exponencial)
    "comandos": 0,              # comandos executados nesta sessão
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


def requisicao(url, corpo=None, ler_json=False):
    """GET (corpo None) ou POST JSON. Devolve (status_http, json).

    status None = falha de rede (sem resposta). Bloqueia no máximo
    TIMEOUT_HTTP_S: o loop principal nunca espera mais que isso.
    """
    resposta = None
    try:
        try:
            if corpo is None:
                resposta = urequests.get(url, timeout=TIMEOUT_HTTP_S)
            else:
                resposta = urequests.post(url, json=corpo, timeout=TIMEOUT_HTTP_S)
        except TypeError:       # urequests antigo, sem parâmetro timeout
            resposta = urequests.get(url) if corpo is None else urequests.post(url, json=corpo)
        dados = None
        if ler_json and 200 <= resposta.status_code < 300:
            try:
                dados = resposta.json()
            except ValueError:
                print("[http] Resposta sem JSON válido de", url)
        return resposta.status_code, dados
    except OSError as e:
        print("[http] Falha de rede:", e)
        return None, None
    finally:
        if resposta is not None:
            resposta.close()    # libera o socket (o ESP32 tem poucos)
        gc.collect()


def classificar(status_http):
    """"ok" (2xx), "rejeitado" (4xx definitivo: não adianta reenviar) ou "falha"."""
    if status_http is not None and 200 <= status_http < 300:
        return "ok"
    if status_http is not None and 400 <= status_http < 500 and status_http not in (408, 429):
        return "rejeitado"
    return "falha"              # sem rede, timeout, 408/429 ou 5xx: tenta de novo


def esvaziar_fila():
    """Envia pendentes em ordem FIFO: espiar -> POST -> só então desenfileirar.

    Um pacote recusado de vez (4xx) sai da fila e é contado em "rejeitados":
    se ficasse na cabeça, bloquearia todos os pacotes de trás para sempre.
    """
    if not wlan.isconnected():
        return 0
    enviados = 0
    while not fila_envio.vazia() and enviados < MAX_ENVIOS_POR_CICLO:
        codigo, _ = requisicao(API_URL, fila_envio.espiar())
        situacao = classificar(codigo)
        if situacao == "falha":
            if codigo is not None:
                print("[http] Servidor respondeu", codigo)
            estado["falhas_envio"] += 1
            break               # mantém a ordem: tenta de novo mais tarde
        pacote = fila_envio.desenfileirar()
        if situacao == "ok":
            enviados += 1
            estado["enviados"] += 1
        else:
            estado["rejeitados"] += 1
            print("[http] Pacote seq {} recusado ({}) — descartado.".format(
                pacote.get("seq"), codigo))
    if enviados:
        print("[http] {} pacote(s) enviados | pendentes na fila: {}".format(
            enviados, len(fila_envio)))
    return enviados


def registrar(pacote):
    """Coloca o pacote na fila de contingência e tenta enviar.

    Se já havia pacotes esperando, o servidor está com problema: o envio fica
    para a tentativa periódica (a cada 5 s). Assim cada toque no botão não
    custa um timeout de rede enquanto o servidor está fora do ar.
    """
    havia_pendentes = not fila_envio.vazia()
    if fila_envio.enfileirar(pacote):
        print("[fila] Cheia — pacote mais antigo descartado.")
    if not havia_pendentes:
        esvaziar_fila()


def recusar_travado():
    estado["mensagem"] = "Totem travado"
    buzzer.bip(200, 400)
    print("[trava] Totem travado — toque ignorado.")
    atualizar_tela()


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
        travado=trava.travado,
        alarme=alarme_ativo(),
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
    if alarme_ativo():
        destaque = "ALARME"
    elif trava.travado:
        destaque = "TRAVADO"
    else:
        destaque = historico.topo()
    conteudo = (destaque, estado["mensagem"], status)
    if conteudo != _tela_anterior:
        _tela_anterior = conteudo
        tela.mostrar(*conteudo)


# ------------------------------------------------------------------------------
# 3.1 Comandos do dashboard
# ------------------------------------------------------------------------------
def alarme_ativo():
    return estado["alarme_ate"] is not None


def iniciar_alarme(duracao_s):
    agora = time.ticks_ms()
    estado["alarme_ate"] = time.ticks_add(agora, duracao_s * 1000)
    estado["alarme_troca"] = agora
    estado["mensagem"] = "Alarme {} s".format(duracao_s)


def parar_alarme():
    estado["alarme_ate"] = None
    buzzer.silenciar()


def atualizar_alarme(agora):
    """Sirene sem bloquear: alterna dois tons a cada 250 ms até o prazo."""
    if not alarme_ativo():
        return
    if time.ticks_diff(agora, estado["alarme_ate"]) >= 0:
        parar_alarme()
        estado["mensagem"] = "Alarme encerrado"
        print("[alarme] Encerrado.")
        return
    if time.ticks_diff(agora, estado["alarme_troca"]) >= 0:
        estado["alarme_tom"] ^= 1
        buzzer.tocar(2600 if estado["alarme_tom"] else 1800)
        estado["alarme_troca"] = time.ticks_add(agora, 250)


def resetar_contador(comando_id):
    """Zera a sessão do totem como num boot, mas sem reiniciar a placa.

    seq também volta a 1 e um pacote "inicio" (motivo resetar_contador) entra na
    fila DEPOIS dos pacotes antigos: o servidor recebe tudo na ordem certa e
    abre uma sessão nova, com os contadores zerados.
    """
    for chave in ("convencional", "prioritaria", "canceladas", "seq"):
        estado[chave] = 0
    estado["ultima_emissao"] = None
    historico.limpar()
    janela_intervalos.limpar()
    estado["mensagem"] = "Contador zerado"
    registrar(novo_pacote("inicio", ip=wlan.ifconfig()[0] if wlan.isconnected() else None,
                          motivo="resetar_contador", comando_id=comando_id))


def retrato():
    """Estado do totem enviado junto com a confirmação do comando."""
    return {"travado": trava.travado, "alarme": alarme_ativo(),
            "convencional": estado["convencional"], "prioritaria": estado["prioritaria"],
            "canceladas": estado["canceladas"], "fila_pendentes": len(fila_envio)}


def executar_comando(cmd):
    cid = cmd.get("id")
    tipo = cmd.get("tipo")
    parametros = cmd.get("parametros") or {}
    print("[cmd] #{} {} recebido".format(cid, tipo))

    # Reentrega (a confirmação anterior se perdeu): não executa de novo
    for anterior in executados.ultimos(CAPACIDADE_EXECUTADOS):
        if anterior[0] == cid:
            print("[cmd] #{} já executado — só confirmando de novo.".format(cid))
            resultado, detalhe = anterior[1], anterior[2]
            break
    else:
        resultado, detalhe = "executado", None
        try:
            if tipo == "travar":
                trava.definir(True)
                estado["mensagem"] = "Travado (painel)"
                buzzer.bip(150, 600)
            elif tipo == "liberar_trava":
                trava.definir(False)
                estado["mensagem"] = "Liberado (painel)"
                buzzer.bip(80, 2500, vezes=2)
            elif tipo == "acionar_alarme":
                duracao = int(parametros.get("duracao_s", 10))
                iniciar_alarme(max(1, min(60, duracao)))
            elif tipo == "silenciar_alarme":
                parar_alarme()
                estado["mensagem"] = "Alarme silenciado"
            elif tipo == "resetar_contador":
                resetar_contador(cid)
                buzzer.bip(60, 1500)
                buzzer.bip(60, 2500)
            else:
                resultado, detalhe = "falhou", "tipo desconhecido: {}".format(tipo)
        except Exception as e:      # o comando falha, o totem continua rodando
            resultado, detalhe = "falhou", repr(e)[:100]
        executados.push((cid, resultado, detalhe))
        estado["comandos"] += 1
        print("[cmd] #{} {}{}".format(cid, resultado, ": " + detalhe if detalhe else ""))
        atualizar_tela()

    corpo = {"dispositivo": DISPOSITIVO_ID, "resultado": resultado, "estado": retrato()}
    if detalhe:
        corpo["detalhe"] = detalhe
    if fila_confirmacoes.enfileirar((cid, corpo)):
        print("[cmd] Fila de confirmações cheia — a mais antiga foi descartada.")


def enviar_confirmacoes():
    """Esvazia a fila de confirmações (FIFO). True se ficou vazia."""
    while not fila_confirmacoes.vazia():
        cid, corpo = fila_confirmacoes.espiar()
        codigo, _ = requisicao(URL_CONFIRMACAO.format(cid), corpo)
        if classificar(codigo) == "falha":
            return False
        fila_confirmacoes.desenfileirar()       # ok, ou recusada de vez (404/409)
    return True


def consultar_comandos(agora):
    """A cada 2 s: envia confirmações pendentes e busca o próximo comando.

    Só roda com a fila de pacotes vazia (com pacotes presos, o servidor está
    com problema e os eventos têm prioridade). Sem resposta, o intervalo dobra
    até 30 s (recuo exponencial): o loop não fica preso tentando a rede.
    """
    if not COMANDOS or not wlan.isconnected() or not fila_envio.vazia():
        return
    if time.ticks_diff(agora, estado["proxima_consulta"]) < 0:
        return
    ok = enviar_confirmacoes()
    cmd = None
    if ok:
        codigo, dados = requisicao(URL_PROXIMO_COMANDO, ler_json=True)
        ok = classificar(codigo) == "ok"
        if ok and isinstance(dados, dict):
            cmd = dados.get("comando")
    if ok:
        estado["espera_comandos"] = INTERVALO_COMANDOS_MS
    else:
        estado["espera_comandos"] = min(estado["espera_comandos"] * 2, ESPERA_MAXIMA_COMANDOS_MS)
    estado["proxima_consulta"] = time.ticks_add(time.ticks_ms(), estado["espera_comandos"])
    if cmd:
        executar_comando(cmd)
        estado["proxima_consulta"] = time.ticks_ms()   # confirma já na próxima volta


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
    parar_alarme()
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
print("Comandos:   ", ("consulta a cada {} ms".format(INTERVALO_COMANDOS_MS)) if COMANDOS
      else "desligados")
print("Trava:      ", "TRAVADO" if trava.travado else "liberado",
      "(GPIO {})".format(PINO_TRAVA) if PINO_TRAVA is not None else "(só lógica)")
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
registrar(novo_pacote("inicio", ip=wlan.ifconfig()[0] if wlan.isconnected() else None,
                      motivo="boot"))

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
            if evento in ("curto", "longo") and trava.travado:
                recusar_travado()
            elif evento == "curto":
                emitir_senha(botao.nome)
            elif evento == "aviso" and not trava.travado:
                buzzer.bip(40, 1000)        # avisa: ao soltar, vai cancelar
            elif evento == "longo":
                cancelar_ultima()

        atualizar_alarme(agora)

        if time.ticks_diff(agora, ultimo_status) >= INTERVALO_STATUS_MS:
            ultimo_status = agora
            enviar_status()

        if not fila_envio.vazia() and time.ticks_diff(agora, ultimo_reenvio) >= INTERVALO_REENVIO_MS:
            ultimo_reenvio = agora
            esvaziar_fila()

        consultar_comandos(agora)
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
          "{} na fila | {} descartados | {} recusados | {} falhas de envio | "
          "{} comandos".format(
              estado["convencional"], estado["prioritaria"], estado["canceladas"],
              estado["enviados"], len(fila_envio), fila_envio.descartados,
              estado["rejeitados"], estado["falhas_envio"], estado["comandos"]))
