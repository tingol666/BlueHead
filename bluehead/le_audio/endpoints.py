"""Connection-owned ASE state. No PCM, HCI ownership or device I/O.

Tokens use actual connection identity, not an address or a reusable CIS handle.
Every asynchronous native prepare/data-path result must recheck its token.
"""

from dataclasses import dataclass
from enum import IntEnum
from bumble.profiles.ascs import AseReasonCode as Reason, AseResponseCode as Response
from .configuration import ConfigurationError, Lc3Configuration, QosConfiguration


class State(IntEnum):
    IDLE = 0
    CODEC = 1
    QOS = 2
    ENABLING = 3
    STREAMING = 4
    DISABLING = 5
    RELEASING = 6


class EndpointError(ValueError):
    def __init__(self, detail, response=Response.INVALID_ASE_STATE_MACHINE_TRANSITION):
        super().__init__(detail)
        self.response = response
        self.reason = Reason.NONE


@dataclass(frozen=True, eq=False)
class Token:
    ase: int
    owner: object
    epoch: int
    source: bool


@dataclass
class Endpoint:
    ase: int
    source: bool
    state: State = State.IDLE
    token: Token | None = None
    codec: Lc3Configuration | None = None
    qos: QosConfiguration | None = None
    release: Token | None = None
    native_prepared: bool = False
    receiver_ready: bool = False


class EndpointPool:
    def __init__(self, *, sinks=(1, 2, 3, 4), sources=(5, 6)):
        identifiers = (*sinks, *sources)
        if len(set(identifiers)) != len(identifiers) or any(
            type(i) is not int or not 1 <= i <= 255 for i in identifiers
        ):
            raise ValueError("ASE identifiers must be unique nonzero octets")
        self.endpoints = {i: Endpoint(i, i in sources) for i in identifiers}
        self.epoch = 0

    def _get(self, ase):
        try:
            return self.endpoints[ase]
        except KeyError:
            raise EndpointError("Unknown ASE", Response.INVALID_ASE_ID) from None

    def _fresh(self, endpoint, owner):
        self.epoch = (self.epoch + 1) & 0xFFFFFFFF or 1
        return Token(endpoint.ase, owner, self.epoch, endpoint.source)

    def _owned(self, token):
        endpoint = self._get(token.ase)
        if endpoint.token is not token:
            raise EndpointError("Retired ASE token")
        return endpoint

    def current(self, token, *, states=None):
        endpoint = self.endpoints.get(token.ase)
        return endpoint is not None and endpoint.token is token and (states is None or endpoint.state in states)

    def configure(self, ase, owner, codec: Lc3Configuration, *, secured=False):
        endpoint = self._get(ase)
        if owner is None or not secured:
            raise EndpointError("Encrypted connection required", Response.REJECTED_CONFIGURATION_PARAMETER_VALUE)
        if endpoint.token is not None and endpoint.token.owner is not owner:
            raise EndpointError("ASE belongs to another connection", Response.INSUFFICIENT_RESOURCES)
        if endpoint.state not in (State.IDLE, State.CODEC, State.QOS):
            raise EndpointError("Cannot reconfigure an active ASE")
        if endpoint.source and codec.channels != 1:
            raise ConfigurationError("Microphone ASE is mono", unsupported=True)
        endpoint.token = self._fresh(endpoint, owner)
        endpoint.codec, endpoint.qos, endpoint.release = codec, None, None
        endpoint.state = State.CODEC
        return endpoint.token

    def configure_qos(self, token, qos: QosConfiguration):
        endpoint = self._owned(token)
        if endpoint.state not in (State.CODEC, State.QOS):
            raise EndpointError("Codec must be configured before QoS")
        qos.validate(endpoint.codec)
        for other in self.endpoints.values():
            if (
                other is not endpoint
                and other.token is not None
                and other.token.owner is token.owner
                and other.source == endpoint.source
                and other.qos is not None
                and (other.qos.cig, other.qos.cis) == (qos.cig, qos.cis)
            ):
                raise ConfigurationError(
                    "A CIS direction already belongs to another ASE", Reason.INVALID_ASE_CIS_MAPPING
                )
        endpoint.qos = qos
        endpoint.state = State.QOS

    def enable(self, token):
        endpoint = self._owned(token)
        if endpoint.state != State.QOS:
            raise EndpointError("QoS must be configured before Enable")
        endpoint.native_prepared = endpoint.receiver_ready = False
        endpoint.state = State.ENABLING

    def cis_matches(self, token, owner, cig, cis):
        if not self.current(token, states=(State.ENABLING, State.STREAMING)):
            return False
        endpoint = self.endpoints[token.ase]
        return (
            token.owner is owner
            and endpoint.qos is not None
            and (endpoint.qos.cig, endpoint.qos.cis) == (cig, cis)
        )

    def native_ready(self, token, owner, cig, cis):
        if not self.cis_matches(token, owner, cig, cis):
            return False
        endpoint = self.endpoints[token.ase]
        endpoint.native_prepared = True
        if not endpoint.source or endpoint.receiver_ready:
            endpoint.state = State.STREAMING
        return True

    def receiver_started(self, token):
        endpoint = self._owned(token)
        if not endpoint.source or endpoint.state != State.ENABLING:
            raise EndpointError("Source must be Enabling before Receiver Start Ready")
        endpoint.receiver_ready = True
        if endpoint.native_prepared:
            endpoint.state = State.STREAMING

    def disable(self, token):
        endpoint = self._owned(token)
        if endpoint.state not in (State.ENABLING, State.STREAMING):
            raise EndpointError("ASE is not enabled")
        old = endpoint.token
        endpoint.token = self._fresh(endpoint, token.owner)
        endpoint.native_prepared = endpoint.receiver_ready = False
        endpoint.state = State.DISABLING if endpoint.source else State.QOS
        return old, endpoint.token

    def receiver_stopped(self, token):
        endpoint = self._owned(token)
        if not endpoint.source or endpoint.state != State.DISABLING:
            raise EndpointError("Source must be Disabling before Receiver Stop Ready")
        endpoint.state = State.QOS

    def release(self, token):
        endpoint = self._owned(token)
        if endpoint.state in (State.IDLE, State.RELEASING):
            raise EndpointError("ASE cannot be released twice")
        endpoint.token = None
        endpoint.release = self._fresh(endpoint, token.owner)
        endpoint.state = State.RELEASING
        return endpoint.release

    def finish_release(self, ticket):
        endpoint = self._get(ticket.ase)
        if endpoint.release is not ticket or endpoint.state != State.RELEASING:
            return False
        endpoint.state = State.IDLE
        endpoint.release = None
        endpoint.codec = endpoint.qos = None
        return True

    def disconnected(self, owner):
        retired = []
        for endpoint in self.endpoints.values():
            token = endpoint.token or endpoint.release
            if token is not None and token.owner is owner:
                if endpoint.token is not None:
                    retired.append(endpoint.token)
                endpoint.token = endpoint.release = None
                endpoint.codec = endpoint.qos = None
                endpoint.state = State.IDLE
        return retired
