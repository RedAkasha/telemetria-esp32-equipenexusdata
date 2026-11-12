# ==============================================================================
# tabela_hash.py — Tabela Hash com encadeamento separado (separate chaining)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
# Usada pelo backend como ÍNDICE EM MEMÓRIA na frente do banco relacional:
#   - chave de idempotência  -> id da transação   (detecta pacote reenviado)
#   - identificador do ESP32 -> estado do dispositivo (boot, último seq)
#   - (dispositivo, senha)   -> id da senha ativa (cancelamento sem varrer tabela)
#   - (dispositivo, categoria) -> saldo da fila
#
# Como funciona:
#   - Um vetor de "baldes" (buckets). Cada balde é uma lista de pares [chave, valor].
#   - índice = hash(chave) mod capacidade. O hash é o FNV-1a de 64 bits, calculado
#     aqui mesmo (determinístico: o mesmo índice em qualquer execução, ao
#     contrário do hash() de str do Python, que é aleatorizado por processo).
#   - Colisão: as chaves que caem no mesmo balde ficam na lista daquele balde.
#   - Fator de carga = n / capacidade. Passou de 0,75, a capacidade DOBRA e todas
#     as chaves são redistribuídas (rehash). Isso mantém os baldes curtos.
#
# Complexidade:
#   inserir / buscar / remover : O(1) em média (O(1) amortizado no inserir, por
#                                causa do rehash ocasional); O(n) no pior caso
#                                (todas as chaves no mesmo balde).
#   rehash                     : O(n), raro (só quando a carga passa de 0,75).
# ==============================================================================

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIMO = 0x100000001B3
_MASCARA_64 = 0xFFFFFFFFFFFFFFFF

_AUSENTE = object()   # sentinela: diferencia "não existe" de valor None


def fnv1a_64(dados: bytes) -> int:
    """Hash FNV-1a de 64 bits: XOR com o byte e multiplica pelo primo FNV."""
    h = _FNV_OFFSET
    for byte in dados:
        h ^= byte
        h = (h * _FNV_PRIMO) & _MASCARA_64
    return h


def _bytes_da_chave(chave) -> bytes:
    """Converte a chave para bytes de forma estável.

    Aceita str, int e tuplas desses tipos (ex.: (3, "N001")). O prefixo de tipo
    evita que 1 e "1" caiam como a mesma chave.
    """
    if isinstance(chave, str):
        return b"s" + chave.encode("utf-8")
    if isinstance(chave, bool):
        raise TypeError("bool não é aceito como chave")
    if isinstance(chave, int):
        return b"i" + str(chave).encode("ascii")
    if isinstance(chave, tuple):
        partes = [_bytes_da_chave(p) for p in chave]
        # comprimento de cada parte evita ambiguidade: ("ab","c") != ("a","bc")
        return b"t" + b"".join(str(len(p)).encode() + b":" + p for p in partes)
    raise TypeError("tipo de chave não suportado: " + type(chave).__name__)


class TabelaHash:
    """Dicionário chave -> valor com encadeamento separado e rehash automático."""

    CARGA_MAXIMA = 0.75

    def __init__(self, capacidade_inicial: int = 16):
        if capacidade_inicial < 1:
            raise ValueError("capacidade_inicial deve ser >= 1")
        self._baldes = [[] for _ in range(capacidade_inicial)]
        self._tamanho = 0
        self.colisoes = 0     # inserções que caíram num balde já ocupado
        self.rehashes = 0

    # --- consultas -------------------------------------------------------------
    def __len__(self):
        return self._tamanho

    @property
    def capacidade(self):
        return len(self._baldes)

    @property
    def fator_carga(self):
        return self._tamanho / len(self._baldes)

    def _indice(self, chave) -> int:
        return fnv1a_64(_bytes_da_chave(chave)) % len(self._baldes)

    def buscar(self, chave, padrao=None):
        """O(1) médio: calcula o balde e percorre só a lista (curta) dele."""
        for par in self._baldes[self._indice(chave)]:
            if par[0] == chave:
                return par[1]
        return padrao

    def contem(self, chave) -> bool:
        return self.buscar(chave, _AUSENTE) is not _AUSENTE

    __contains__ = contem

    def __getitem__(self, chave):
        valor = self.buscar(chave, _AUSENTE)
        if valor is _AUSENTE:
            raise KeyError(chave)
        return valor

    # --- alterações ------------------------------------------------------------
    def inserir(self, chave, valor) -> None:
        """Insere ou substitui. O(1) amortizado."""
        balde = self._baldes[self._indice(chave)]
        for par in balde:
            if par[0] == chave:
                par[1] = valor          # chave já existe: só troca o valor
                return
        if balde:
            self.colisoes += 1
        balde.append([chave, valor])
        self._tamanho += 1
        if self.fator_carga > self.CARGA_MAXIMA:
            self._redimensionar(2 * len(self._baldes))

    __setitem__ = inserir

    def remover(self, chave, padrao=None):
        """Remove e devolve o valor (ou `padrao` se a chave não existe). O(1) médio."""
        balde = self._baldes[self._indice(chave)]
        for i, par in enumerate(balde):
            if par[0] == chave:
                balde[i] = balde[-1]    # troca com o último e encurta: O(1)
                balde.pop()
                self._tamanho -= 1
                return par[1]
        return padrao

    def _redimensionar(self, nova_capacidade: int) -> None:
        antigos = self._baldes
        self._baldes = [[] for _ in range(nova_capacidade)]
        for balde in antigos:
            for chave, valor in balde:
                self._baldes[self._indice(chave)].append([chave, valor])
        self.rehashes += 1

    # --- iteração --------------------------------------------------------------
    def itens(self):
        for balde in self._baldes:
            for chave, valor in balde:
                yield chave, valor

    def maior_balde(self) -> int:
        """Comprimento do maior encadeamento (diagnóstico da distribuição)."""
        return max((len(b) for b in self._baldes), default=0)
