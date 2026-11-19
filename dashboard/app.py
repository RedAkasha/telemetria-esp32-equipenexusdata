# ==============================================================================
# app.py — Dashboard do Totem ByteBank Queue (Streamlit)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
#   pip install -r dashboard/requirements.txt
#   streamlit run dashboard/app.py
#
# Leitura: direto do banco SQLite gravado pela API (somente leitura, modo WAL).
# O painel "Ao vivo" é um st.fragment com run_every: só ele é refeito a cada
# atualização (padrão 2 s), sem recarregar a página nem os controles.
# Comandos: enviados à API (POST /comandos); o ESP32 busca o comando, executa
# e confirma, e a confirmação aparece no histórico do próprio painel.
#
# Configuração por variável de ambiente (ou na barra lateral):
#   NEXUS_DB   caminho do banco      (padrão: backend/nexus.db)
#   NEXUS_API  endereço da API       (padrão: http://localhost:5000)
#   NEXUS_TZ   fuso para exibir horas (padrão: America/Recife)
# ==============================================================================
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_cliente  # noqa: E402
import dados  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]

ROTULO_COMANDO = {
    "liberar_trava": "🔓 Liberar trava",
    "travar": "🔒 Travar",
    "acionar_alarme": "🚨 Acionar alarme",
    "silenciar_alarme": "🔕 Silenciar alarme",
    "resetar_contador": "🔄 Resetar contador",
}
ICONE_SITUACAO = {
    "pendente": "⏳ pendente", "entregue": "📡 entregue", "executado": "✅ executado",
    "falhou": "❌ falhou", "expirado": "⌛ expirado", "sem_resposta": "⚠️ sem resposta",
}

# Paleta categórica fixa: a cor segue a categoria em todos os gráficos
COR_CATEGORIA = {"convencional": "#2a78d6", "prioritaria": "#eb6834"}
COR_SERIE = "#2a78d6"

st.set_page_config(page_title="NEXUS Data — Totem ByteBank", page_icon="🏦", layout="wide")
# Valores das métricas um pouco menores: cabem "🔒 Travado" e "-58 dBm" sem cortar
st.markdown("<style>[data-testid='stMetricValue']{font-size:1.65rem}</style>",
            unsafe_allow_html=True)


# ------------------------------------------------------------------ barra lateral
with st.sidebar:
    st.header("⚙️ Conexão")
    caminho_db = st.text_input(
        "Banco SQLite", os.environ.get("NEXUS_DB", str(RAIZ / "backend" / "nexus.db")),
        key="caminho_db")
    api = st.text_input("API (FastAPI)", os.environ.get("NEXUS_API", "http://localhost:5000"),
                        key="api")
    intervalo = st.slider("Atualizar a cada (s)", 1, 10, 2, key="intervalo")
    fuso = ZoneInfo(os.environ.get("NEXUS_TZ", "America/Recife"))

st.title("🏦 Totem ByteBank Queue — Telemetria ao vivo")

if not dados.banco_pronto(caminho_db):
    st.warning(
        "Banco não encontrado (ou sem as tabelas da API) em `{}`.\n\n"
        "Suba a API primeiro: `cd backend && python -m api`. Ela cria o banco e o "
        "dashboard passa a mostrar os dados assim que o totem enviar o primeiro pacote."
        .format(caminho_db))
    st.stop()

totens = dados.dispositivos(caminho_db)
if totens.empty:
    st.info("Nenhum totem enviou dados ainda. Ligue o ESP32 (ou o simulador: "
            "`python -m simulador`) e esta tela se atualiza sozinha.")

    @st.fragment(run_every=intervalo)
    def esperar_primeiro_totem():
        if not dados.dispositivos(caminho_db).empty:
            st.rerun()                      # chegou o primeiro pacote: monta o painel
    esperar_primeiro_totem()
    st.stop()

with st.sidebar:
    ident = st.selectbox("Totem", totens["identificador"].tolist(), key="totem")
    st.caption("API: " + ("🟢 no ar" if api_cliente.api_no_ar(api) else "🔴 fora do ar"))


# ------------------------------------------------------------------ utilidades
def hora_local(texto):
    d = dados.instante(texto)
    return d.astimezone(fuso).strftime("%H:%M:%S") if d else "—"


def ha_quanto(texto):
    s = dados.segundos_desde(texto)
    if s is None:
        return "—"
    s = max(0, int(s))
    return "{} s".format(s) if s < 120 else "{} min".format(s // 60)


def tabela(df, colunas_hora=(), **kw):
    if df.empty:
        st.caption("Nada por aqui ainda.")
        return
    df = df.copy()
    for c in colunas_hora:
        df[c] = df[c].map(hora_local)
    for c in df.columns[df.dtypes == object]:
        df[c] = df[c].fillna("—")
    st.dataframe(df, hide_index=True, **kw)


# ------------------------------------------------------------------ comandos
@st.fragment
def painel_comandos():
    st.subheader("🎛️ Comandos para o totem")
    st.caption("O ESP32 consulta a API a cada 2 s, executa e confirma. Um comando não "
               "buscado dentro da validade expira (o totem pode estar desligado).")
    c1, c2, c3, c4, c5 = st.columns([1, 1, 1.3, 1, 1.3])
    pedido = None
    with c1:
        if st.button(ROTULO_COMANDO["liberar_trava"], key="cmd_liberar_trava", width="stretch"):
            pedido = ("liberar_trava", None)
    with c2:
        if st.button(ROTULO_COMANDO["travar"], key="cmd_travar", width="stretch"):
            pedido = ("travar", None)
    with c3:
        duracao = st.number_input("Duração do alarme (s)", 1, 60, 10, key="duracao_alarme")
        if st.button(ROTULO_COMANDO["acionar_alarme"], key="cmd_acionar_alarme",
                     type="primary", width="stretch"):
            pedido = ("acionar_alarme", duracao)
    with c4:
        if st.button(ROTULO_COMANDO["silenciar_alarme"], key="cmd_silenciar_alarme",
                     width="stretch"):
            pedido = ("silenciar_alarme", None)
    with c5:
        confirma = st.checkbox("Confirmo zerar N/P", key="confirma_reset")
        if st.button(ROTULO_COMANDO["resetar_contador"], key="cmd_resetar_contador",
                     disabled=not confirma, width="stretch"):
            pedido = ("resetar_contador", None)
    validade = st.select_slider("Validade do comando", [15, 30, 60, 120, 300], 60,
                                format_func=lambda s: "{} s".format(s), key="validade")

    if pedido:
        tipo, dur = pedido
        ok, resposta = api_cliente.enviar_comando(api, ident, tipo, dur, validade)
        if ok:
            st.session_state["ultimo_comando"] = resposta["id"]
            st.toast("Comando #{} ({}) enviado ao {}".format(
                resposta["id"], ROTULO_COMANDO[tipo], ident), icon="📨")
        else:
            st.session_state.pop("ultimo_comando", None)
            st.error(resposta)



painel_comandos()
st.divider()


# ------------------------------------------------------------------ ao vivo
@st.fragment(run_every=intervalo)
def painel_ao_vivo():
    # acompanha o último comando enviado: pendente -> entregue -> executado
    cid = st.session_state.get("ultimo_comando")
    cmd = dados.comando(caminho_db, cid) if cid else None
    if cmd:
        st.info("Último comando: **#{} {}** → {} (tentativas: {}){}".format(
            cid, ROTULO_COMANDO.get(cmd["tipo"], cmd["tipo"]),
            ICONE_SITUACAO.get(cmd["situacao"], cmd["situacao"]), cmd["tentativas"],
            " — " + cmd["resultado"] if cmd["resultado"] else ""))
    disp = dados.dispositivo(caminho_db, ident)
    status = dados.ultimo_status(caminho_db, ident)
    atuadores = dados.estado_atuadores(caminho_db, ident)
    online = (dados.segundos_desde(disp["ultimo_contato"]) or 1e9) <= dados.ONLINE_ATE_S

    cab1, cab2 = st.columns([3, 2], vertical_alignment="bottom")
    cab1.subheader("📟 " + ident)
    with cab2:
        st.badge("online" if online else "offline", color="green" if online else "red",
                 icon=":material/wifi:" if online else ":material/wifi_off:")
        st.caption("Último contato há {} · atualizado às {}".format(
            ha_quanto(disp["ultimo_contato"]), datetime.now(fuso).strftime("%H:%M:%S")))
    topo = st.columns(6)
    trava_txt = {True: "🔒 Travado", False: "🔓 Liberado", None: "—"}[atuadores["travado"]]
    alarme_txt = {True: "🚨 Ativo", False: "🔕 Inativo", None: "—"}[atuadores["alarme"]]
    topo[0].metric("Trava", trava_txt, border=True)
    topo[1].metric("Alarme", alarme_txt, border=True)
    topo[2].metric("Sessão / seq", "{} / {}".format(disp["boot_atual"], disp["ultimo_seq"]),
                   border=True, help="A sessão sobe a cada reboot ou reset de contador.")
    if status:
        topo[3].metric("Wi-Fi (RSSI)", "{} dBm".format(status["rssi_dbm"])
                       if status["rssi_dbm"] is not None else "—", border=True)
        topo[4].metric("Memória livre", "{:.1f} KB".format(status["mem_livre"] / 1024)
                       if status["mem_livre"] else "—", border=True)
        topo[5].metric("Fila offline", status["fila_pendentes"]
                       if status["fila_pendentes"] is not None else "—",
                       help="Pacotes guardados no ESP32 esperando a rede", border=True)
    else:
        topo[3].caption("Aguardando o primeiro pacote de status (a cada 15 s).")

    # --- saldo da fila de atendimento
    sal = dados.saldos(caminho_db, ident).set_index("categoria")

    def valor(cat, campo):
        return int(sal.loc[cat, campo]) if cat in sal.index else 0

    m = st.columns(5)
    m[0].metric("Em espera — convencional", valor("convencional", "em_espera"), border=True)
    m[1].metric("Em espera — prioritária", valor("prioritaria", "em_espera"), border=True)
    m[2].metric("Emitidas (N + P)", valor("convencional", "emitidas")
                + valor("prioritaria", "emitidas"), border=True)
    m[3].metric("Canceladas", valor("convencional", "canceladas")
                + valor("prioritaria", "canceladas"), border=True)
    intervalo_medio = status["intervalo_medio_s"] if status else None
    m[4].metric("Intervalo médio", "{:.1f} s".format(intervalo_medio)
                if intervalo_medio is not None else "—", border=True,
                help="Média móvel do tempo entre senhas (janela deslizante do ESP32)")
    if status and status["historico"]:
        st.caption("Display do totem — últimas senhas: " + "  ·  ".join(status["historico"]))

    # --- gráficos (um eixo por gráfico; horas no fuso local, 24 h)
    g1, g2, g3 = st.columns(3)
    with g1:
        st.markdown("**Senhas emitidas por minuto** (30 min)")
        em = dados.emissoes_por_minuto(caminho_db, ident)
        if em.empty:
            st.caption("Nenhuma senha nos últimos 30 minutos.")
        else:
            em["minuto"] = dados.para_local(em["minuto"], fuso).dt.strftime("%H:%M")
            st.altair_chart(alt.Chart(em).mark_bar(
                cornerRadiusEnd=3, stroke="white", strokeWidth=2,
                width=alt.RelativeBandSize(0.6)).encode(
                x=alt.X("minuto:O", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("senhas:Q", title=None, stack=True, axis=alt.Axis(tickMinStep=1)),
                color=alt.Color("categoria:N", title=None, legend=alt.Legend(orient="bottom"),
                                scale=alt.Scale(domain=list(COR_CATEGORIA),
                                                range=list(COR_CATEGORIA.values()))),
                tooltip=[alt.Tooltip("minuto:O", title="Minuto"),
                         alt.Tooltip("categoria:N", title="Categoria"),
                         alt.Tooltip("senhas:Q", title="Senhas")],
            ).properties(height=220))
    serie = dados.serie_status(caminho_db, ident)
    if not serie.empty:
        serie["hora"] = dados.para_local(serie["recebido_em"], fuso)

    def linha_tempo(coluna, titulo, passo=False):
        # contagem (fila) parte do zero e só tem inteiros; RSSI usa a faixa dos dados
        eixo_y = alt.Axis(format="d", tickMinStep=1) if passo else alt.Axis()
        base = alt.Chart(serie).encode(
            x=alt.X("hora:T", title=None, axis=alt.Axis(format="%H:%M")),
            y=alt.Y(coluna + ":Q", title=None, axis=eixo_y,
                    scale=alt.Scale(zero=passo, domainMin=0 if passo else alt.Undefined)),
            tooltip=[alt.Tooltip("hora:T", title="Hora", format="%H:%M:%S"),
                     alt.Tooltip(coluna + ":Q", title=titulo)])
        linha = base.mark_line(strokeWidth=2, color=COR_SERIE,
                               interpolate="step-after" if passo else "linear")
        pontos = base.mark_circle(size=40, opacity=0, color=COR_SERIE)  # alvo do tooltip
        return (linha + pontos).properties(height=220)
    with g2:
        st.markdown("**Sinal Wi-Fi** (RSSI, dBm)")
        if serie.empty:
            st.caption("Sem pacotes de status nos últimos 30 minutos.")
        else:
            st.altair_chart(linha_tempo("rssi_dbm", "RSSI (dBm)"))
    with g3:
        st.markdown("**Fila offline do ESP32** (pacotes)")
        if serie.empty:
            st.caption("Sem pacotes de status nos últimos 30 minutos.")
        else:
            st.altair_chart(linha_tempo("fila_pendentes", "Pacotes na fila", passo=True))

    # --- tabelas
    t1, t2 = st.columns(2)
    with t1:
        st.markdown("**Senhas aguardando** (prioritárias primeiro)")
        tabela(dados.senhas_aguardando(caminho_db, ident), ["emitida_em"], height=250)
    with t2:
        st.markdown("**Últimos pacotes recebidos**")
        tabela(dados.transacoes(caminho_db, ident), ["recebido_em"], height=250)

    t3, t4 = st.columns(2)
    with t3:
        st.markdown("**Comandos enviados**")
        cmds = dados.comandos(caminho_db, ident)
        if not cmds.empty:
            cmds["tipo"] = cmds["tipo"].map(lambda t: ROTULO_COMANDO.get(t, t))
            cmds["situacao"] = cmds["situacao"].map(lambda s: ICONE_SITUACAO.get(s, s))
            cmds["parametros"] = cmds["parametros"].map(
                lambda p: ", ".join("{}={}".format(k, v) for k, v in json.loads(p).items()))
        tabela(cmds, ["criado_em", "concluido_em"], height=250)
    with t4:
        st.markdown("**Alertas da auditoria**")
        tabela(dados.auditoria(caminho_db, ident), ["ocorrido_em"], height=250)


painel_ao_vivo()
