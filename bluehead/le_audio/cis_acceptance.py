"""One incoming CIS attempt; command serialization stays with the HCI owner.

Register terminal events synchronously, before yielding to another packet.
Never hold a peer-wide or controller-wide lock over asynchronous RF setup.
"""

import asyncio
from bumble import hci, utils
from bumble.device import CisLink


class CisSuperseded(ConnectionError):
    """A newer controller request owns this handle; retain its ASE resources."""


class CisAcceptance:
    def __init__(self, device, link):
        self.device, self.link = device, link
        self.task = None
        self.result = asyncio.get_running_loop().create_future()
        self.watcher = utils.EventWatcher()
        self.link_watcher = None
        self.follow(link)
        self.watcher.on(link.acl_connection, 'disconnection', self.disconnected)
        if link.state == CisLink.State.ESTABLISHED:
            self.established()

    def same_request(self, link):
        return (not self.result.done() and self.link.acl_connection is link.acl_connection
                and (self.link.handle, self.link.cig_id, self.link.cis_id) ==
                (link.handle, link.cig_id, link.cis_id))

    def follow(self, link):
        # A controller can repeat a still-pending request. Bumble creates a new
        # link object, but HCI still has ONE procedure for this tuple. Observe
        # the replacement's events without issuing another Accept command.
        if self.link_watcher is not None:
            self.link_watcher.close()
        self.link_watcher = utils.EventWatcher()
        self.link = link
        self.link_watcher.on(link, CisLink.EVENT_ESTABLISHMENT, self.established)
        self.link_watcher.on(link, CisLink.EVENT_ESTABLISHMENT_FAILURE, self.failed)
        self.link_watcher.on(link, CisLink.EVENT_DISCONNECTION, self.disconnected)

    def established(self):
        if not self.result.done():
            self.result.set_result(None)

    def failed(self, status):
        if not self.result.done():
            self.result.set_exception(hci.HCI_Error(status))

    def disconnected(self, reason):
        if not self.result.done():
            self.result.set_exception(ConnectionError(f"CIS owner disconnected ({reason})"))

    def supersede(self):
        if not self.result.done():
            self.result.set_exception(CisSuperseded("A newer CIS request owns this handle"))

    async def run(self):
        if self.result.done():
            return await self.result
        if self.device.cis_links.get(self.link.handle) is not self.link:
            raise CisSuperseded("The CIS handle has already retired")
        # Bumble's convenience accept_cis_request holds a global CIS lock until
        # establishment. The original command owner already serializes HCI
        # command/status matching; RF completion is independently link-owned.
        await self.device.send_async_command(
            hci.HCI_LE_Accept_CIS_Request_Command(connection_handle=self.link.handle)
        )
        await self.result

    def close(self):
        self.watcher.close()
        self.link_watcher.close()
        if not self.result.done():
            self.result.cancel()
        elif not self.result.cancelled():
            self.result.exception()
