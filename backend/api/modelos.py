# ==============================================================================
# modelos.py — Validação dos pacotes JSON do ESP32 (Pydantic v2)
#
# Um modelo por "tipo" de pacote (ver docs/arquitetura.md). O campo "tipo" é o
# discriminador: o Pydantic escolhe o modelo certo sem tentar todos.
# Campos extras são aceitos (o firmware pode ganhar campos novos) e o pacote
# bruto completo fica guardado em transacoes.payload.
# ==============================================================================
from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, TypeAdapter, model_validator

PADRAO_SENHA = r"^[NP][0-9]{3}$"
PADRAO_DISPOSITIVO = r"^[A-Za-z0-9_.-]{1,64}$"

Contador = Annotated[StrictInt, Field(ge=0)]


class Cabecalho(BaseModel):
    """Campos comuns a todos os pacotes."""
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    dispositivo: Annotated[str, Field(pattern=PADRAO_DISPOSITIVO)]
    seq: Annotated[StrictInt, Field(ge=1)]
    # null quando o ESP32 ainda não sincronizou o relógio via NTP
    timestamp: Optional[datetime] = None
    uptime_ms: Optional[Contador] = None


class PacoteInicio(Cabecalho):
    tipo: Literal["inicio"]
    ip: Optional[str] = Field(default=None, max_length=45)
    # "boot" (ligou) ou "resetar_contador" (comando do dashboard zerou a sessão)
    motivo: Optional[Annotated[str, Field(max_length=40)]] = None
    comando_id: Optional[Annotated[StrictInt, Field(ge=1)]] = None


class PacoteSenhaEmitida(Cabecalho):
    tipo: Literal["senha_emitida"]
    senha: Annotated[str, Field(pattern=PADRAO_SENHA)]
    categoria: Literal["convencional", "prioritaria"]

    @model_validator(mode="after")
    def _prefixo_bate_com_categoria(self):
        esperado = "N" if self.categoria == "convencional" else "P"
        if self.senha[0] != esperado:
            raise ValueError("senha {} não corresponde à categoria {}".format(
                self.senha, self.categoria))
        return self


class PacoteSenhaCancelada(Cabecalho):
    tipo: Literal["senha_cancelada"]
    senha: Annotated[str, Field(pattern=PADRAO_SENHA)]


class PacoteStatus(Cabecalho):
    tipo: Literal["status"]
    total_convencional: Contador
    total_prioritaria: Contador
    canceladas: Optional[Contador] = None
    intervalo_medio_s: Optional[Annotated[float, Field(ge=0)]] = None
    historico: list[Annotated[str, Field(pattern=PADRAO_SENHA)]] = Field(
        default_factory=list, max_length=10)
    fila_pendentes: Optional[Contador] = None
    descartados: Optional[Contador] = None
    rssi_dbm: Optional[StrictInt] = Field(default=None, ge=-127, le=0)
    mem_livre: Optional[Contador] = None
    # estado dos atuadores controlados por comando
    travado: Optional[StrictBool] = None
    alarme: Optional[StrictBool] = None


Pacote = Annotated[
    Union[PacoteInicio, PacoteSenhaEmitida, PacoteSenhaCancelada, PacoteStatus],
    Field(discriminator="tipo"),
]

validador_pacote = TypeAdapter(Pacote)


# ==============================================================================
# Comandos: dashboard -> API -> ESP32 (sentido inverso ao da telemetria)
# ==============================================================================
TipoComando = Literal["liberar_trava", "travar", "acionar_alarme",
                      "silenciar_alarme", "resetar_contador"]


class ComandoNovo(BaseModel):
    """Corpo do POST /comandos (enviado pelo dashboard)."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dispositivo: Annotated[str, Field(pattern=PADRAO_DISPOSITIVO)]
    tipo: TipoComando
    # só para acionar_alarme: por quanto tempo a sirene toca
    duracao_s: Optional[Annotated[StrictInt, Field(ge=1, le=60)]] = None
    # prazo para o totem buscar o comando; depois disso ele expira sem executar
    validade_s: Annotated[StrictInt, Field(ge=5, le=3600)] = 60
    origem: Annotated[str, Field(min_length=1, max_length=40)] = "dashboard"

    @model_validator(mode="after")
    def _parametros_coerentes(self):
        if self.duracao_s is not None and self.tipo != "acionar_alarme":
            raise ValueError("duracao_s só vale para acionar_alarme")
        return self

    def parametros(self) -> dict:
        if self.tipo == "acionar_alarme":
            return {"duracao_s": self.duracao_s or 10}
        return {}


class ConfirmacaoComando(BaseModel):
    """Corpo do POST /comandos/{id}/confirmacao (enviado pelo ESP32)."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    dispositivo: Annotated[str, Field(pattern=PADRAO_DISPOSITIVO)]
    resultado: Literal["executado", "falhou"]
    detalhe: Optional[Annotated[str, Field(max_length=200)]] = None
    # retrato do totem logo após executar (travado, alarme, contadores...)
    estado: Optional[dict] = None
