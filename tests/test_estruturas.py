# ==============================================================================
# test_estruturas.py — Testes das estruturas de dados (rodam no PC)
#   python tests/test_estruturas.py        (ou: pytest tests/)
# ==============================================================================
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "firmware"))

from estruturas import FilaCircular, JanelaMovel  # noqa: E402


def test_fila_fifo_basico():
    f = FilaCircular(3)
    assert f.vazia() and len(f) == 0 and f.espiar() is None
    for i in (1, 2, 3):
        assert f.enfileirar(i) is None
    assert f.cheia() and len(f) == 3
    assert f.espiar() == 1            # espiar não remove
    assert len(f) == 3
    assert [f.desenfileirar() for _ in range(3)] == [1, 2, 3]
    assert f.vazia()


def test_fila_da_volta_no_vetor():
    f = FilaCircular(3)
    f.enfileirar("a"); f.enfileirar("b")
    f.desenfileirar()
    f.enfileirar("c"); f.enfileirar("d")   # "d" ocupa a posição 0 de novo
    assert f.para_lista() == ["b", "c", "d"]


def test_fila_cheia_descarta_mais_antigo():
    f = FilaCircular(3)
    for i in range(1, 6):
        f.enfileirar(i)
    assert f.para_lista() == [3, 4, 5]
    assert f.descartados == 2
    assert f.enfileirar(6) == 3


def test_fila_vazia_lanca_erro():
    f = FilaCircular(1)
    try:
        f.desenfileirar()
    except IndexError:
        return
    raise AssertionError("deveria lançar IndexError")


def test_janela_media_movel():
    j = JanelaMovel(3)
    assert j.media() == 0.0
    assert j.adicionar(10) == 10
    assert j.adicionar(20) == 15
    assert j.adicionar(30) == 20
    assert j.cheia()
    assert j.adicionar(40) == 30      # sai o 10: (20+30+40)/3
    assert j.adicionar(50) == 40      # sai o 20
    assert (j.minimo(), j.maximo()) == (30, 50)


def test_janela_soma_corrente_igual_a_recalculo():
    import random
    random.seed(1)
    j = JanelaMovel(10)
    historico = []
    for _ in range(500):
        v = random.randint(0, 4095)
        historico.append(v)
        ultimos = historico[-10:]
        assert abs(j.adicionar(v) - sum(ultimos) / len(ultimos)) < 1e-9


if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in testes:
        t()
        print("ok  ", t.__name__)
    print("\n{} testes passaram.".format(len(testes)))
