import pytest

from api.tabela_hash import TabelaHash, fnv1a_64


def test_fnv1a_valores_conhecidos():
    assert fnv1a_64(b"") == 0xCBF29CE484222325
    assert fnv1a_64(b"a") == 0xAF63DC4C8601EC8C


def test_inserir_buscar_substituir_remover():
    t = TabelaHash()
    t.inserir("N001", 1)
    t[(3, "P002")] = 2
    assert t.buscar("N001") == 1 and t[(3, "P002")] == 2
    assert len(t) == 2
    t.inserir("N001", 10)                  # substitui, não duplica
    assert t["N001"] == 10 and len(t) == 2
    assert t.remover("N001") == 10
    assert "N001" not in t and len(t) == 1
    assert t.buscar("N001", "x") == "x"
    with pytest.raises(KeyError):
        t["N001"]


def test_valor_none_e_chaves_de_tipos_diferentes():
    t = TabelaHash()
    t["a"] = None
    assert t.contem("a")
    t[1] = "int"
    t["1"] = "str"
    t[("ab", "c")] = 1
    t[("a", "bc")] = 2
    assert (t[1], t["1"], t[("ab", "c")], t[("a", "bc")]) == ("int", "str", 1, 2)


def test_rehash_mantem_todos_os_itens_e_carga_baixa():
    t = TabelaHash(capacidade_inicial=4)
    for i in range(5000):
        t[(i, "N{:03d}".format(i % 1000))] = i
    assert len(t) == 5000
    assert t.rehashes > 0 and t.fator_carga <= TabelaHash.CARGA_MAXIMA
    assert all(t[(i, "N{:03d}".format(i % 1000))] == i for i in range(5000))
    assert t.maior_balde() < 12            # boa distribuição: baldes curtos
    for i in range(0, 5000, 2):
        t.remover((i, "N{:03d}".format(i % 1000)))
    assert len(t) == 2500 and len(list(t.itens())) == 2500


def test_chave_invalida():
    with pytest.raises(TypeError):
        TabelaHash().inserir(1.5, "x")
