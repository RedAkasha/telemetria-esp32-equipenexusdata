# ==============================================================================
# main.py — API REST do Totem ByteBank Queue (FastAPI)
# DISCIPLINA: Algoritmo e Estrutura de Dados (BD015) — CESAR School
#
# Recebe os pacotes do ESP32 (urequests.post -> POST /leituras), valida,
# aplica as regras de negócio (servico.py) e grava no banco SQLite.
#
#   cd backend
#   pip install -r requirements.txt
#   python -m api                  # porta 5000 (ou: python -m api 8080)
#   documentação interativa: http://localhost:5000/docs
#
# Códigos de resposta do POST /leituras (o firmware só tira o pacote da fila
# de contingência com 2xx):
#   201  pacote novo gravado
#   200  reenvio de um pacote já gravado (idempotente: nada é duplicado)
#   400  corpo não é JSON          413  corpo grande demais
#   422  JSON fora do formato      500  falha ao gravar (ROLLBACK feito)
# ==============================================================================
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .modelos import validador_pacote
from .servico import ServicoTelemetria

TAMANHO_MAXIMO = 8 * 1024          # bytes; o maior pacote do totem (status) tem ~400
BANCO_PADRAO = Path(__file__).resolve().parents[1] / "nexus.db"


def _campo_do_erro(erro) -> str:
    """Caminho do campo inválido; "tipo" quando o próprio tipo é desconhecido."""
    if erro["type"].startswith("union_tag"):
        return "tipo"
    return ".".join(str(p) for p in erro["loc"]) or "corpo"


def criar_app(caminho_banco=None) -> FastAPI:
    caminho = caminho_banco or os.environ.get("NEXUS_DB") or BANCO_PADRAO

    @asynccontextmanager
    async def ciclo_de_vida(app: FastAPI):
        app.state.servico = ServicoTelemetria(caminho)
        yield
        app.state.servico.fechar()

    app = FastAPI(
        title="NEXUS Data — API do Totem ByteBank Queue",
        version="1.0.0",
        description="Recebe e valida a telemetria do totem ESP32 e grava no banco relacional.",
        lifespan=ciclo_de_vida,
    )

    def servico(request: Request) -> ServicoTelemetria:
        return request.app.state.servico

    def ip_cliente(request: Request) -> Optional[str]:
        return request.client.host if request.client else None

    # ------------------------------------------------------------------ escrita
    # Corpo lido "à mão" (e não como parâmetro do FastAPI) para registrar na
    # auditoria também os pacotes rejeitados, com o corpo original.
    @app.post("/leituras", tags=["ESP32"], summary="Recebe um pacote do totem")
    async def receber_leitura(request: Request):
        svc, ip = servico(request), ip_cliente(request)

        async def rejeitar(motivo, detalhe, bruto, codigo, corpo_resposta):
            await run_in_threadpool(svc.registrar_rejeicao, motivo, detalhe, bruto, ip)
            return JSONResponse(corpo_resposta, codigo)

        corpo = await request.body()
        if len(corpo) > TAMANHO_MAXIMO:
            return await rejeitar("pacote_rejeitado", "corpo com {} bytes".format(len(corpo)),
                                  corpo[:TAMANHO_MAXIMO], 413,
                                  {"erro": "corpo maior que {} bytes".format(TAMANHO_MAXIMO)})
        try:
            dados = json.loads(corpo)
        except (ValueError, UnicodeDecodeError):
            return await rejeitar("pacote_rejeitado", "JSON inválido", corpo, 400,
                                  {"erro": "JSON inválido"})
        try:
            pacote = validador_pacote.validate_python(dados)
        except ValidationError as erro:
            erros = [{"campo": _campo_do_erro(e), "erro": e["msg"]}
                     for e in erro.errors(include_url=False)]
            return await rejeitar("pacote_rejeitado", json.dumps(erros, ensure_ascii=False),
                                  corpo, 422, {"erro": erros})

        try:
            # processar() é síncrono (SQLite + Lock): roda fora do loop de eventos
            status, resposta = await run_in_threadpool(svc.processar, pacote, ip)
        except Exception:
            return JSONResponse({"erro": "falha ao gravar; nada foi salvo, reenvie"}, 500)
        return JSONResponse(resposta, status)

    # ------------------------------------------------------------------ leitura
    # Rotas "def" (síncronas): o FastAPI as executa num pool de threads.
    @app.get("/", tags=["Consulta"], summary="Resumo do servidor")
    def raiz(request: Request):
        return {"servidor": "NEXUS Data — API do totem", **servico(request).resumo()}

    @app.get("/saldos", tags=["Consulta"], summary="Saldo da fila de todos os totens")
    def listar_saldos(request: Request):
        return servico(request).saldos()

    @app.get("/saldos/{dispositivo}", tags=["Consulta"],
             summary="Saldo de um totem (busca O(1) na tabela hash)")
    def saldo_dispositivo(dispositivo: str, request: Request):
        saldo = servico(request).saldo_do_dispositivo(dispositivo)
        if saldo is None:
            raise HTTPException(404, "dispositivo não encontrado")
        return saldo

    @app.get("/dispositivos", tags=["Consulta"])
    def listar_dispositivos(request: Request):
        return servico(request).dispositivos()

    @app.get("/dispositivos/{dispositivo}/senhas/{codigo}", tags=["Consulta"],
             summary="Senha aguardando (busca O(1) na tabela hash)")
    def consultar_senha(dispositivo: str, codigo: str, request: Request):
        senha = servico(request).senha_ativa(dispositivo, codigo.upper())
        if senha is None:
            raise HTTPException(404, "senha não está aguardando neste totem")
        return senha

    @app.get("/transacoes", tags=["Consulta"], summary="Livro-razão (mais recentes primeiro)")
    def listar_transacoes(
        request: Request,
        limite: int = Query(50, ge=1, le=500),
        dispositivo: Optional[str] = None,
        tipo: Optional[Literal["inicio", "senha_emitida", "senha_cancelada", "status"]] = None,
    ):
        return servico(request).transacoes(limite, dispositivo, tipo)

    @app.get("/auditoria", tags=["Consulta"], summary="Logs de auditoria (mais recentes primeiro)")
    def listar_auditoria(
        request: Request,
        limite: int = Query(50, ge=1, le=500),
        nivel: Optional[Literal["info", "aviso", "erro"]] = None,
        evento: Optional[str] = None,
    ):
        return servico(request).auditoria(limite, nivel, evento)

    @app.get("/indices", tags=["Consulta"],
             summary="Estatísticas das tabelas hash em memória")
    def indices(request: Request):
        return servico(request).estatisticas_indices()

    return app


app = criar_app()
