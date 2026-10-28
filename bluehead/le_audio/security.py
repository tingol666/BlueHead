"""Dual-mode SMP policy and stable, controller-scoped local identity.

Only signalling setup runs here. Bumble retains its original peer bond store.
Changing transport preference never erases another device's pairing keys.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
from bumble.pairing import PairingConfig, PairingDelegate
from bumble import smp
from bumble.core import PhysicalTransport


class AudioPairingSession(smp.Session):
    """Validate CTKD prerequisites before deriving or replacing any bond keys."""
    def ctkd_error(self, command):
        local = (self.pairing_config.delegate.local_initiator_key_distribution
                 | self.pairing_config.delegate.local_responder_key_distribution)
        requested = command.initiator_key_distribution | command.responder_key_distribution
        if (self.connection.transport != PhysicalTransport.LE
                or not local & smp.KeyDistribution.LINK_KEY
                or not requested & smp.KeyDistribution.LINK_KEY):
            return None
        # Support for SC is not the same as having negotiated SC. The peer's
        # wire message must permit it, and CTKD may not use a shortened key.
        if not self.pairing_config.sc or not command.auth_req & smp.AuthReq.SC:
            return smp.ErrorCode.AUTHENTICATION_REQUIREMENTS
        if command.maximum_encryption_key_size != 16:
            return smp.ErrorCode.ENCRYPTION_KEY_SIZE
        return None

    async def on_smp_pairing_request_command_async(self, command):
        if (reason := self.ctkd_error(command)) is not None:
            self.send_pairing_failed(reason)
            return
        await super().on_smp_pairing_request_command_async(command)

    def on_smp_pairing_response_command(self, command):
        if (reason := self.ctkd_error(command)) is not None:
            self.send_pairing_failed(reason)
            return
        super().on_smp_pairing_response_command(command)


def pairing_config(*, dual_mode):
    flags = PairingDelegate.DEFAULT_KEY_DISTRIBUTION
    if dual_mode:
        # BAP 9.1.1: derive a BR/EDR link key when requested during LE pairing.
        # ENC_KEY also permits the reverse procedure over encrypted BR/EDR.
        flags |= PairingDelegate.KeyDistribution.DISTRIBUTE_LINK_KEY
    return PairingConfig(
        mitm=False, sc=True, bonding=True,
        identity_address_type=PairingConfig.AddressType.PUBLIC,
        delegate=PairingDelegate(local_initiator_key_distribution=flags,
                                 local_responder_key_distribution=flags),
    )


def local_irk(directory, address):
    # Never distribute DeviceConfiguration's all-zero default IRK. Bind the
    # local identity to the physical public address when adapters are changed.
    namespace = hashlib.sha256(bytes(address)).hexdigest()
    folder = Path(directory) / 'identities'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (namespace + '.json')
    if path.exists():
        try:
            if path.stat().st_size > 1024:
                raise ValueError('Oversized identity')
            record = json.loads(path.read_text('utf-8'))
            irk = bytes.fromhex(record['irk'])
            if record['version'] != 1 or len(irk) != 16 or irk == bytes(16):
                raise ValueError('Invalid identity')
            return irk
        except (KeyError, TypeError, ValueError) as error:
            raise OSError('本机 LE 配对身份文件损坏，请保留数据目录并通过帮助与诊断导出报告。') from error
    irk = secrets.token_bytes(16)
    temporary = folder / (namespace + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(dict(version=1, irk=irk.hex()), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return irk


async def prepare_identity(device, directory):
    device.irk = await asyncio.to_thread(local_irk, directory, device.public_address)
    device.config.irk = device.irk
