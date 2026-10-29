"""Strict ASCS control model, per-connection ASE namespaces, no PCM or I/O.

Bluetooth ASCS 1.0 sections 4/5. The complete wire operation is checked before
state changes; the adapter sends its response before notifications/effects.
"""

from dataclasses import dataclass, field
import struct
from bumble.profiles.ascs import AseReasonCode as Reason, AseResponseCode as Response
from .configuration import ConfigurationError, Lc3Configuration, QosConfiguration
from .endpoints import EndpointError, EndpointPool, State, Token


class WireError(ValueError):
    def __init__(self, response=Response.INVALID_LENGTH):
        super().__init__(response.name)
        self.response = response


@dataclass(frozen=True)
class Entry:
    ase: int
    values: tuple = ()


@dataclass(frozen=True)
class Operation:
    opcode: int
    entries: tuple[Entry, ...]


def parse_operation(data: bytes) -> Operation:
    if not isinstance(data, bytes) or not data or len(data) > 512:
        raise WireError()
    opcode = data[0]
    if not 1 <= opcode <= 8:
        raise WireError(Response.UNSUPPORTED_OPCODE)
    if len(data) < 2 or not 1 <= data[1] <= 170:
        raise WireError()
    offset, entries = 2, []

    def take(size):
        nonlocal offset
        end = offset + size
        if end > len(data):
            raise WireError()
        value, offset = data[offset:end], end
        return value

    for _ in range(data[1]):
        ase = take(1)[0]
        if opcode == 1:
            latency, phy = take(2)
            codec = take(5)
            config = take(take(1)[0])
            values = (latency, phy, codec, config)
        elif opcode == 2:
            value = take(15)
            values = (
                QosConfiguration(
                    value[0],
                    value[1],
                    int.from_bytes(value[2:5], "little"),
                    value[5],
                    value[6],
                    int.from_bytes(value[7:9], "little"),
                    value[9],
                    int.from_bytes(value[10:12], "little"),
                    int.from_bytes(value[12:15], "little"),
                ),
            )
        elif opcode in (3, 7):
            values = (take(take(1)[0]),)
        else:
            values = ()
        entries.append(Entry(ase, values))
    if offset != len(data):
        raise WireError()
    return Operation(opcode, tuple(entries))


def metadata(data: bytes, supported_contexts=0x0FFF):
    offset, seen = 0, set()
    while offset < len(data):
        length = data[offset]
        end = offset + length + 1
        tag = data[offset + 1] if length and offset + 1 < len(data) else 0
        valid = length > 0 and end <= len(data)
        value = data[offset + 2 : end]
        widths = {1: 2, 2: 2, 4: 3, 6: 1, 8: 1, 9: 0}
        if tag in widths:
            valid &= tag not in seen and len(value) == widths[tag]
        if tag == 2 and len(value) == 2:
            context = int.from_bytes(value, "little")
            valid &= context != 0 and context & ~supported_contexts == 0
        if tag == 8 and len(value) == 1:
            valid &= value[0] in (0, 1)
        if tag in (0xFE, 0xFF):
            valid &= len(value) >= 2
        if not valid:
            error = EndpointError("Invalid audio metadata", Response.INVALID_METADATA)
            error.reason = tag
            raise error
        seen.add(tag)
        offset = end
    return data


@dataclass
class Payload:
    configuration: bytes = b""
    metadata: bytes = b""
    delay: tuple = (10000, 80000, 20000, 60000)
    transport_ms: int = 10
    preferred_phy: int = 2


@dataclass
class Peer:
    owner: object
    identity: int
    pool: EndpointPool = field(default_factory=lambda: EndpointPool(sinks=(1, 2), sources=(3,)))
    payloads: dict[int, Payload] = field(default_factory=lambda: {i: Payload() for i in (1, 2, 3)})


@dataclass(frozen=True)
class Effect:
    action: str
    token: Token
    ticket: Token | None = None


@dataclass
class Result:
    response: bytes
    notifications: list[tuple[int, bytes]] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)


def uint24(value):
    return value.to_bytes(3, "little")


class StreamControl:
    def __init__(self, maximum_peers=3, *, microphone=True, source_octets=400, source_blocks=2, supported_phys=3):
        if not 1 <= maximum_peers <= 4:
            raise ValueError("Unsupported LE peer budget")
        self.maximum_peers = maximum_peers
        if type(supported_phys) is not int or supported_phys not in (1, 3):
            raise ValueError("Invalid local CIS PHY capabilities")
        self.supported_phys = supported_phys
        self.delay_caps = None
        self.microphone = bool(microphone)
        if not 20 <= source_octets <= 400 or source_blocks not in (1, 2):
            raise ValueError("Invalid published microphone capability limits")
        self.source_octets, self.source_blocks = source_octets, source_blocks
        self.peers: dict[int, Peer] = {}
        self.next_peer = 0

    def peer(self, owner, *, create=False):
        state = self.peers.get(id(owner))
        if state is not None and state.owner is owner:
            return state
        if not create:
            raise EndpointError("Retired LE peer", Response.INVALID_ASE_ID)
        if owner is None or len(self.peers) >= self.maximum_peers:
            raise EndpointError("LE peer capacity exceeded", Response.INSUFFICIENT_RESOURCES)
        self.next_peer += 1
        state = Peer(
            owner, self.next_peer, pool=EndpointPool(sinks=(1, 2), sources=(3,) if self.microphone else ())
        )
        self.peers[id(owner)] = state
        return state

    def value(self, owner, ase):
        peer = self.peer(owner, create=True)
        endpoint = peer.pool._get(ase)
        result = bytes([ase, endpoint.state])
        if endpoint.state in (State.IDLE, State.RELEASING):
            return result
        if endpoint.state == State.CODEC:
            payload = peer.payloads[ase]
            cfg = payload.configuration
            return (
                result
                + struct.pack("<BBBH", 0, payload.preferred_phy, 2, payload.transport_ms)
                + b"".join(uint24(n) for n in peer.payloads[ase].delay)
                + b"\x06\x00\x00\x00\x00"
                + bytes([len(cfg)])
                + cfg
            )
        qos = endpoint.qos
        if endpoint.state == State.QOS:
            return (
                result
                + bytes([qos.cig, qos.cis])
                + uint24(qos.interval_us)
                + struct.pack("<BBHBH", qos.framing, qos.phy, qos.max_sdu, qos.retransmissions, qos.max_latency_ms)
                + uint24(qos.presentation_us)
            )
        md = peer.payloads[ase].metadata
        return result + bytes([qos.cig, qos.cis, len(md)]) + md

    def current(self, token):
        peer = self.peers.get(id(token.owner))
        return bool(peer is not None and peer.owner is token.owner and peer.pool.current(token))

    def _operate(self, peer, opcode, entry):
        endpoint = peer.pool._get(entry.ase)
        token = endpoint.token
        if opcode == 1:
            latency, phy, coding, config = entry.values
            if latency not in (1, 2, 3) or phy not in (1, 2, 3):
                raise ConfigurationError("Invalid target preference")
            if coding != b"\x06\x00\x00\x00\x00":
                raise ConfigurationError("Only LC3 is exposed", Reason.CODEC_ID, unsupported=True)
            codec = Lc3Configuration.parse(config, source=endpoint.source)
            if endpoint.source and (codec.octets > self.source_octets or codec.blocks > self.source_blocks):
                raise EndpointError(
                    "Configuration exceeds published microphone capabilities",
                    Response.UNSUPPORTED_AUDIO_CAPABILITIES,
                )
            delay = self.delay_caps(endpoint.source, codec) if self.delay_caps else (10000, 80000, 20000, 60000)
            if (
                len(delay) != 4
                or any(type(value) is not int for value in delay)
                or not 0 <= delay[0] <= delay[2] <= delay[3] <= delay[1] <= 0xFFFFFF
            ):
                raise ConfigurationError(
                    "Invalid presentation capability range", Reason.PRESENTATION_DELAY, unsupported=True
                )
            peer.pool.configure(entry.ase, peer.owner, codec, secured=True)
            preferred_phy = phy if phy in (1, 2) and self.supported_phys & (1 << (phy - 1)) else (2 if self.supported_phys & 2 else 1)
            peer.payloads[entry.ase] = Payload(config, delay=delay, transport_ms=codec.preferred_transport_ms,
                                            preferred_phy=preferred_phy)
            return [Effect("stop", token)] if token else []
        if token is None:
            raise EndpointError("Configure ASE before using it")
        if opcode == 2:
            qos = entry.values[0]
            if qos.phy in (1, 2) and not self.supported_phys & (1 << (qos.phy - 1)):
                raise ConfigurationError("This controller cannot use the requested CIS PHY", Reason.PHY, unsupported=True)
            delay = peer.payloads[entry.ase].delay
            if not delay[0] <= qos.presentation_us <= delay[1]:
                raise ConfigurationError(
                    "Presentation delay is outside the advertised range",
                    Reason.PRESENTATION_DELAY,
                    unsupported=True,
                )
            for other in peer.pool.endpoints.values():
                if (
                    other is not endpoint
                    and other.qos
                    and other.source == endpoint.source
                    and other.qos.cig == qos.cig
                ):
                    if other.qos.presentation_us != qos.presentation_us:
                        raise ConfigurationError(
                            "One CIG direction requires one presentation delay",
                            Reason.PRESENTATION_DELAY,
                            unsupported=True,
                        )
                    if other.qos.interval_us != qos.interval_us:
                        raise ConfigurationError(
                            "One CIG direction requires one SDU interval", Reason.SDU_INTERVAL, unsupported=True
                        )
            peer.pool.configure_qos(token, qos)
        elif opcode == 3:
            md = metadata(entry.values[0])
            peer.pool.enable(token)
            peer.payloads[entry.ase].metadata = md
            return [Effect("prepare", token)]
        elif opcode == 4:
            if not endpoint.source:
                raise EndpointError("Invalid Receiver Start Ready direction", Response.INVALID_ASE_DIRECTION)
            peer.pool.receiver_started(token)
        elif opcode == 5:
            old, _ = peer.pool.disable(token)
            return [Effect("stop", old)]
        elif opcode == 6:
            if not endpoint.source:
                raise EndpointError("Invalid Receiver Stop Ready direction", Response.INVALID_ASE_DIRECTION)
            peer.pool.receiver_stopped(token)
        elif opcode == 7:
            if endpoint.state not in (State.ENABLING, State.STREAMING):
                raise EndpointError("Metadata requires an enabled ASE")
            peer.payloads[entry.ase].metadata = metadata(entry.values[0])
        elif opcode == 8:
            ticket = peer.pool.release(token)
            return [Effect("stop", token, ticket)]
        return []

    def write(self, owner, data, *, secured=False):
        opcode = data[0] if isinstance(data, bytes) and data else 0
        try:
            operation = parse_operation(data)
        except WireError as error:
            return Result(bytes([opcode, 0xFF, 0, error.response, 0]))
        if not secured:
            return Result(
                bytes([opcode, len(operation.entries)])
                + b"".join(
                    bytes([entry.ase, Response.REJECTED_CONFIGURATION_PARAMETER_VALUE, 0])
                    for entry in operation.entries
                )
            )
        try:
            peer = self.peer(owner, create=True)
        except EndpointError as error:
            return Result(
                bytes([opcode, len(operation.entries)])
                + b"".join(bytes([entry.ase, error.response, error.reason]) for entry in operation.entries)
            )
        result, responses, seen = Result(b""), [], set()
        for entry in operation.entries:
            try:
                if entry.ase in seen:
                    raise EndpointError("Duplicate ASE in one operation", Response.INVALID_ASE_ID)
                seen.add(entry.ase)
                result.effects.extend(self._operate(peer, opcode, entry))
                result.notifications.append((entry.ase, self.value(owner, entry.ase)))
                response, reason = Response.SUCCESS, Reason.NONE
            except (EndpointError, ConfigurationError) as error:
                response, reason = error.response, error.reason
            responses.append(bytes([entry.ase, response, reason]))
        result.response = bytes([opcode, len(responses)]) + b"".join(responses)
        return result

    def native_ready(self, token, owner, cig, cis):
        if not self.current(token):
            return False
        return self.peer(owner).pool.native_ready(token, owner, cig, cis)

    def finish_release(self, ticket):
        peer = self.peers.get(id(ticket.owner))
        return bool(peer is not None and peer.owner is ticket.owner and peer.pool.finish_release(ticket))

    def disconnected(self, owner):
        peer = self.peers.get(id(owner))
        if peer is None or peer.owner is not owner:
            return []
        self.peers.pop(id(owner))
        return peer.pool.disconnected(owner)
