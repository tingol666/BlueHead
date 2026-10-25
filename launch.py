import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--engine" in sys.argv:
        import argparse
        from bluehead.engine import main
        from bluehead.storage import data_directory
        parser = argparse.ArgumentParser()
        parser.add_argument("--engine", action="store_true")
        parser.add_argument("--data-dir", default=str(data_directory()))
        arguments = parser.parse_args()
        raise SystemExit(main(arguments.data_dir))
    if "--native-trace" in sys.argv:
        sys.argv.remove("--native-trace")
        from bluehead.app import app_directory
        from bluehead.native_trace import enable
        enable(app_directory() / "data")
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        from bluehead.checks import smoke_test
        raise SystemExit(smoke_test(sys.argv[2]))
    if len(sys.argv) == 4 and sys.argv[1] == "--usb-voice-check":
        import asyncio
        import json
        from pathlib import Path
        from bluehead.usb_check import probe
        if "+sco=0" not in sys.argv[2]:
            raise SystemExit("USB voice check requires an explicit usb: adapter with +sco=0")
        report = asyncio.run(probe(sys.argv[2]))
        Path(sys.argv[3]).write_text(json.dumps(report, indent=2), "utf-8")
        raise SystemExit(0 if report["passed"] else 1)
    from bluehead.app import main
    main()
