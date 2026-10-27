"""Small validated desktop command surface; broadcast passwords stay volatile."""

from bumble import hci


def validate(message):
    operation = message.get("operation")
    allowed = {"scan", "select", "stop", "code", "program", "gain", "refresh"}
    if operation not in allowed:
        raise ValueError("未知音频广播操作")
    result = {"operation": operation}
    if operation == "scan":
        if type(message.get("enabled")) is not bool:
            raise ValueError("无效搜索开关")
        result["enabled"] = message["enabled"]
    elif operation == "select":
        address = message.get("address")
        address_type, sid, broadcast = message.get("address_type"), message.get("sid"), message.get("broadcast_id")
        if (
            not isinstance(address, str)
            or len(address) != 17
            or any(c not in "0123456789abcdefABCDEF:" for c in address)
        ):
            raise ValueError("无效广播地址")
        if type(address_type) is not int or address_type not in (0, 1):
            raise ValueError("无效广播地址类型")
        if (
            type(sid) is not int
            or not 0 <= sid <= 15
            or type(broadcast) is not int
            or not 0 <= broadcast <= 0xFFFFFF
        ):
            raise ValueError("无效广播标识")
        # Parse once at the trust boundary; do not pass arbitrary strings to HCI.
        hci.Address(address, address_type=address_type)
        result.update(address=address, address_type=address_type, sid=sid, broadcast_id=broadcast)
    elif operation == "code":
        code = message.get("code")
        if not isinstance(code, str):
            raise ValueError("无效广播密码")
        if not 0 < len(code.encode("utf-8")) <= 16:
            raise ValueError("广播密码太长，请按发送设备显示的密码输入（最多16个英文字母或数字）。")
        result["code"] = code.encode("utf-8").ljust(16, b"\0")
    elif operation == "program":
        index = message.get("index")
        if type(index) is not int or not 0 <= index < 31:
            raise ValueError("无效广播节目")
        result["index"] = index
    elif operation == "gain":
        gain = message.get("gain")
        if type(gain) not in (int, float) or not 0 <= gain <= 2:
            raise ValueError("音量范围为0%–200%")
        result["gain"] = float(gain)
    return result


async def execute(receiver, values):
    app = receiver.le_audio
    if app is None or app.broadcast is None:
        raise OSError(
            "当前无法接收音频广播。请在“设置 → 高级音频设置”开启 LE Audio，再回音频中心点“开始接收”。若仍不可用，需要支持广播接收的蓝牙适配器。"
        )
    manager = app.broadcast
    operation = values["operation"]
    if operation == "scan":
        await manager.scan(values["enabled"])
    elif operation == "stop":
        await manager.stop()
    elif operation == "select":
        await manager.start(
            hci.Address(values["address"], address_type=values["address_type"]),
            values["sid"],
            values["broadcast_id"],
        )
    elif operation == "code":
        await manager.set_code(values["code"])
    elif operation == "program":
        await manager.set_program(values["index"])
    elif operation == "gain":
        await manager.sink.gain(values["gain"])
    manager.emit("broadcast", **manager.snapshot())
