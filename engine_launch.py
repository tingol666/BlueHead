"""Console-subsystem worker, launched with private redirected pipes by the UI."""
import multiprocessing
import sys


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == "--offline-audio-check":
        from bluehead.selfcheck import run
        raise SystemExit(run(sys.argv[2]))
    if len(sys.argv) == 4 and sys.argv[1] == "--usb-voice-check":
        import asyncio
        import json
        from pathlib import Path
        from bluehead.usb_check import probe
        if "+sco=0" not in sys.argv[2]:
            raise SystemExit("Specify one USB adapter with +sco=0")
        result = asyncio.run(probe(sys.argv[2]))
        Path(sys.argv[3]).write_text(json.dumps(result, indent=2), "utf-8")
        raise SystemExit(0 if result["passed"] else 1)
    if len(sys.argv) == 3 and sys.argv[1] == "--system-audio-check":
        import json
        from pathlib import Path
        from bluehead.loopback import ProcessCapture
        result = {"passed": False, "scope": "Frozen process-excluding capture open/close; no Bluetooth or saved audio"}
        capture = None
        try:
            capture = ProcessCapture()
            capture.close()
            result["passed"] = True
        except Exception as error:
            result["error"] = str(error)
        finally:
            if capture:
                try:
                    capture.close()
                except Exception as error:
                    result["passed"], result["error"] = False, str(error)
        Path(sys.argv[2]).write_text(json.dumps(result, indent=2), "utf-8")
        raise SystemExit(0 if result["passed"] else 1)
    import argparse
    from bluehead.engine import main
    from bluehead.storage import data_directory
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", action="store_true", required=True)
    parser.add_argument("--data-dir", default=str(data_directory()))
    options = parser.parse_args()
    raise SystemExit(main(options.data_dir))
