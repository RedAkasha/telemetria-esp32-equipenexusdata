# ==============================================================================
# estruturas.py — Estruturas de Dados em memória para o ESP32 (MicroPython)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
# Três estruturas, todas sobre listas de TAMANHO FIXO alocadas uma única vez:
#
#   1. FilaContingencia  -> Fila FIFO (buffer circular) para pacotes que não
#                           puderam ser enviados (Wi-Fi caiu, servidor fora do
#                           ar). Quando a conexão volta, os pacotes saem na MESMA
#                           ordem em que entraram.
#   2. PilhaHistorico    -> Pilha LIFO das últimas senhas emitidas. O topo é a
#                           senha mais recente: é ela que aparece primeiro no
#                           display e é ela que pop() remove ao cancelar.
#   3. JanelaDeslizante  -> Média móvel dos últimos N intervalos entre senhas
#                           (ritmo de chegada de clientes ao totem).
#
# Por que listas de tamanho fixo e não list.append()/list.pop(0)?
#   - A lista é alocada UMA vez: a memória usada é previsível (o ESP32 tem
#     pouca RAM e list.append() pode fragmentar o heap).
#   - list.pop(0) é O(n) porque desloca todos os elementos; aqui enfileirar e
#     desenfileirar são O(1): só mexemos em índices.
# ==============================================================================


class FilaContingencia:
    """Fila FIFO de capacidade fixa (buffer circular).

    Política de estouro: se a fila estiver cheia, o pacote MAIS ANTIGO é
    descartado para dar lugar ao novo (preferimos perder o dado mais velho a
    travar a coleta). O total de descartes fica em `self.descartados`.

    Complexidade: enfileirar O(1) | desenfileirar O(1) | espiar O(1)
    Memória: O(capacidade), alocada no construtor.
    """

    def __init__(self, capacidade=50):
        if capacidade < 1:
            raise ValueError("capacidade deve ser >= 1")
        self._dados = [None] * capacidade   # alocação única
        self._capacidade = capacidade
        self._inicio = 0        # índice do primeiro elemento (cabeça da fila)
        self._tamanho = 0       # quantidade de elementos armazenados
        self.descartados = 0    # pacotes perdidos por estouro

    # --- consultas ------------------------------------------------------------
    def __len__(self):
        return self._tamanho

    def vazia(self):
        return self._tamanho == 0

    def cheia(self):
        return self._tamanho == self._capacidade

    @property
    def capacidade(self):
        return self._capacidade

    # --- operações ------------------------------------------------------------
    def enfileirar(self, item):
        """Insere no fim da fila. Retorna True se precisou descartar o mais antigo."""
        descartou = False
        if self.cheia():
            # Sobrescreve a cabeça: o mais antigo sai, o início avança uma casa.
            self._dados[self._inicio] = None
            self._inicio = (self._inicio + 1) % self._capacidade
            self._tamanho -= 1
            self.descartados += 1
            descartou = True

        fim = (self._inicio + self._tamanho) % self._capacidade
        self._dados[fim] = item
        self._tamanho += 1
        return descartou

    def espiar(self):
        """Retorna o primeiro elemento SEM removê-lo (None se vazia).

        Usado no envio: só removemos o pacote depois que o servidor confirmar
        o recebimento; se o POST falhar, ele continua na cabeça da fila.
        """
        if self.vazia():
            return None
        return self._dados[self._inicio]

    def desenfileirar(self):
        """Remove e retorna o primeiro elemento (None se vazia)."""
        if self.vazia():
            return None
        item = self._dados[self._inicio]
        self._dados[self._inicio] = None      # libera a referência para o GC
        self._inicio = (self._inicio + 1) % self._capacidade
        self._tamanho -= 1
        return item

    def __repr__(self):
        return "FilaContingencia({}/{}, descartados={})".format(
            self._tamanho, self._capacidade, self.descartados)


class PilhaHistorico:
    """Pilha LIFO de capacidade fixa.

    push() coloca no topo; pop() retira do topo (o último que entrou é o
    primeiro a sair). Se a pilha estiver cheia, o elemento mais ANTIGO (o do
    fundo) é descartado — para um histórico de "últimas N senhas" é o
    comportamento desejado.

    Complexidade: push O(1) | pop O(1) | topo O(1) | ultimos(k) O(k)
    """

    def __init__(self, capacidade=10):
        if capacidade < 1:
            raise ValueError("capacidade deve ser >= 1")
        self._dados = [None] * capacidade
        self._capacidade = capacidade
        self._fundo = 0         # índice do elemento mais antigo
        self._tamanho = 0

    def __len__(self):
        return self._tamanho

    def vazia(self):
        return self._tamanho == 0

    def push(self, item):
        """Empilha. Retorna True se precisou descartar o elemento do fundo."""
        descartou = False
        if self._tamanho == self._capacidade:
            self._dados[self._fundo] = None
            self._fundo = (self._fundo + 1) % self._capacidade
            self._tamanho -= 1
            descartou = True
        topo = (self._fundo + self._tamanho) % self._capacidade
        self._dados[topo] = item
        self._tamanho += 1
        return descartou

    def topo(self):
        """Elemento do topo sem remover (None se vazia)."""
        if self._tamanho == 0:
            return None
        return self._dados[(self._fundo + self._tamanho - 1) % self._capacidade]

    def pop(self):
        """Remove e retorna o topo (None se vazia)."""
        if self._tamanho == 0:
            return None
        i = (self._fundo + self._tamanho - 1) % self._capacidade
        item = self._dados[i]
        self._dados[i] = None
        self._tamanho -= 1
        return item

    def ultimos(self, k):
        """Lista com até k elementos, do topo (mais recente) para baixo."""
        k = min(k, self._tamanho)
        return [self._dados[(self._fundo + self._tamanho - 1 - j) % self._capacidade]
                for j in range(k)]

    def __repr__(self):
        return "PilhaHistorico({}/{}, topo={})".format(
            self._tamanho, self._capacidade, self.topo())


class JanelaDeslizante:
    """Janela deslizante de tamanho N com média móvel em O(1).

    Mantém a soma corrente: ao entrar um valor novo, somamos ele e subtraímos
    o valor que saiu da janela — não é preciso percorrer a lista inteira.

    Complexidade: adicionar O(1) | media O(1)
    """

    def __init__(self, tamanho=10):
        if tamanho < 1:
            raise ValueError("tamanho deve ser >= 1")
        self._valores = [0] * tamanho
        self._tamanho = tamanho
        self._pos = 0           # próxima posição a ser sobrescrita
        self._qtd = 0           # quantos valores já entraram (até `tamanho`)
        self._soma = 0

    def __len__(self):
        return self._qtd

    def cheia(self):
        return self._qtd == self._tamanho

    def adicionar(self, valor):
        """Insere um valor; se a janela estiver cheia, o mais antigo sai."""
        if self._qtd == self._tamanho:
            self._soma -= self._valores[self._pos]   # valor que sai da janela
        else:
            self._qtd += 1
        self._valores[self._pos] = valor
        self._soma += valor
        self._pos = (self._pos + 1) % self._tamanho

    def media(self):
        """Média dos valores presentes na janela (0 se vazia)."""
        if self._qtd == 0:
            return 0
        return self._soma / self._qtd

    def __repr__(self):
        return "JanelaDeslizante({}/{}, media={:.1f})".format(
            self._qtd, self._tamanho, self.media())
