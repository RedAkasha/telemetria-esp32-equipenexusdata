# ==============================================================================
# estruturas.py — Estruturas de dados em memória para o ESP32 (MicroPython)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
# Duas estruturas, ambas sobre um VETOR DE TAMANHO FIXO (buffer circular):
#
#   1. FilaCircular  — fila FIFO de contingência offline. Guarda os pacotes
#                      JSON que ainda não chegaram à API. Se o Wi-Fi cair, os
#                      pacotes se acumulam aqui e são reenviados, NA ORDEM em
#                      que foram gerados, quando a rede voltar.
#
#   2. JanelaMovel   — janela deslizante de média móvel. Suaviza o ruído do
#                      ADC do potenciômetro usando as N últimas leituras.
#
# Por que vetor fixo e não list.append()/list.pop(0)?
#   - O ESP32 tem pouca RAM (~100 KB livres). Alocar tudo no início evita
#     fragmentação do heap e garante que a fila nunca cresça sem limite.
#   - list.pop(0) é O(n) (desloca todos os elementos). Com dois índices
#     (inicio/fim) andando em círculo, enfileirar e desenfileirar são O(1).
#
# Este arquivo não depende de nada do ESP32: roda também no Python do PC,
# o que permite testá-lo (ver tests/test_estruturas.py).
# ==============================================================================


class FilaCircular:
    """Fila FIFO de capacidade fixa (buffer circular).

    Política quando cheia: descarta o item MAIS ANTIGO para dar lugar ao
    novo (o dado mais recente é o mais valioso para telemetria). O número
    de descartes fica registrado em `descartados`.

    Complexidade: enfileirar, desenfileirar, espiar e len() são O(1).
    Memória: O(capacidade), alocada uma única vez no construtor.
    """

    def __init__(self, capacidade):
        if capacidade < 1:
            raise ValueError("capacidade deve ser >= 1")
        self._dados = [None] * capacidade
        self._cap = capacidade
        self._inicio = 0      # índice do primeiro (mais antigo) elemento
        self._tamanho = 0     # quantos elementos estão na fila
        self.descartados = 0  # itens perdidos por estouro de capacidade

    def __len__(self):
        return self._tamanho

    @property
    def capacidade(self):
        return self._cap

    def vazia(self):
        return self._tamanho == 0

    def cheia(self):
        return self._tamanho == self._cap

    def enfileirar(self, item):
        """Insere no fim. Retorna o item descartado (ou None)."""
        descartado = None
        if self.cheia():
            # Sobrescreve o mais antigo e avança o início.
            descartado = self._dados[self._inicio]
            self._inicio = (self._inicio + 1) % self._cap
            self._tamanho -= 1
            self.descartados += 1
        fim = (self._inicio + self._tamanho) % self._cap
        self._dados[fim] = item
        self._tamanho += 1
        return descartado

    def espiar(self):
        """Retorna o primeiro item SEM removê-lo (None se vazia).

        Usado no reenvio: só removemos da fila depois que a API confirmou
        o recebimento — assim nenhum pacote se perde se o POST falhar.
        """
        if self._tamanho == 0:
            return None
        return self._dados[self._inicio]

    def desenfileirar(self):
        """Remove e retorna o primeiro item. Lança IndexError se vazia."""
        if self._tamanho == 0:
            raise IndexError("fila vazia")
        item = self._dados[self._inicio]
        self._dados[self._inicio] = None  # libera a referência para o GC
        self._inicio = (self._inicio + 1) % self._cap
        self._tamanho -= 1
        return item

    def para_lista(self):
        """Cópia dos itens em ordem FIFO (para depuração/testes)."""
        return [self._dados[(self._inicio + i) % self._cap]
                for i in range(self._tamanho)]


class JanelaMovel:
    """Janela deslizante de tamanho N com média móvel simples.

    Mantém a soma corrente: ao entrar um valor novo com a janela cheia,
    subtrai o valor que sai e soma o que entra. Assim a média é O(1),
    sem percorrer a janela a cada leitura.

    Complexidade: adicionar() e media() são O(1). Memória: O(N).
    """

    def __init__(self, tamanho):
        if tamanho < 1:
            raise ValueError("tamanho deve ser >= 1")
        self._valores = [0] * tamanho
        self._n = tamanho
        self._pos = 0       # próxima posição a ser sobrescrita
        self._qtd = 0       # quantos valores válidos (<= n)
        self._soma = 0

    def __len__(self):
        return self._qtd

    def cheia(self):
        return self._qtd == self._n

    def adicionar(self, valor):
        """Insere um valor e retorna a nova média."""
        if self._qtd == self._n:
            self._soma -= self._valores[self._pos]  # valor que sai
        else:
            self._qtd += 1
        self._valores[self._pos] = valor
        self._soma += valor
        self._pos = (self._pos + 1) % self._n
        return self.media()

    def media(self):
        if self._qtd == 0:
            return 0.0
        return self._soma / self._qtd

    def minimo(self):
        return min(self._valores[:self._qtd]) if self._qtd else None

    def maximo(self):
        return max(self._valores[:self._qtd]) if self._qtd else None
