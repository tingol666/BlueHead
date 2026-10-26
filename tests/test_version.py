"""A release cannot display or export a stale handwritten version."""
from pathlib import Path
import tomllib
import xml.etree.ElementTree as ET

from bluehead import __version__
from bluehead.engine import Engine
from io import StringIO
import json


def test_version_agrees_across_python_desktop_and_engine(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    build = ET.parse(root / "desktop/Directory.Build.props").getroot()
    assert any(item.attrib.get('Project') == '$(MSBuildThisFileDirectory)BlueHead.Version.props'
               for item in build.findall('Import'))
    props = ET.parse(root / "desktop/BlueHead.Version.props").getroot()
    assert __version__ == project["project"]["version"] == props.findtext("PropertyGroup/Version")
    assert props.findtext("PropertyGroup/InformationalVersion") == __version__
    stream = StringIO()
    engine = Engine(tmp_path, stream)
    engine.command({"protocol": 1, "command": "hello"})
    assert json.loads(stream.getvalue())["version"] == __version__
    xaml = (root / "desktop/BlueHead.Desktop/MainWindow.xaml").read_text("utf-8")
    assert "AppVersion.Label" in xaml and "AppVersion.Title" in xaml
    assert "1.2.2" not in xaml and "1.2.3" not in xaml
