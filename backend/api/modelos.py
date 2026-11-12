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

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

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


Pacote = Annotated[
    Union[PacoteInicio, PacoteSenhaEmitida, PacoteSenhaCancelada, PacoteStatus],
    Field(discriminator="tipo"),
]

validador_pacote = TypeAdapter(Pacote)
