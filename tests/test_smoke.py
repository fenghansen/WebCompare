from pathlib import Path

import pyvips
from fastapi.testclient import TestClient

from webcompare.app import create_app
from webcompare.config import Settings


def test_compare_demo_images(tmp_path: Path) -> None:
    calibration = Path(__file__).resolve().parents[1] / "examples" / "calibration"
    names = ["01-reference.webp", "06-low-light.webp"]
    sources = [{"path": str(calibration / name)} for name in names]

    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        session = client.get("/api/auth/session")
        assert session.status_code == 200, session.text
        comparison = client.post(
            "/api/comparisons",
            headers={"X-CSRF-Token": session.json()["csrf"]},
            json={"sources": sources},
        )
        assert comparison.status_code == 200, comparison.text
        manifest = comparison.json()
        reference, low_light = manifest["images"]
        assert [manifest["assets"][asset]["name"] for asset in (reference, low_light)] == names

        rendered = client.get("/api/diff", params={
            "left": low_light, "right": reference, "x": 400, "y": 90, "width": 10, "height": 8,
        })
        assert rendered.status_code == 200, rendered.text
        assert rendered.headers["content-type"] == "image/png"
        difference = pyvips.Image.new_from_buffer(rendered.content, "")
        assert (difference.width, difference.height) == (10, 8)
        assert difference.getpoint(0, 0) == [52, 20, 15]
