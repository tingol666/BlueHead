"""Source entry for the headless engine; the desktop lives in desktop/."""
import runpy

if __name__ == "__main__":
    runpy.run_module("engine_launch", run_name="__main__")
