"""Generate all packaged icons from the application's shared vinyl icon."""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crate_transfer.ui import app_icon


def png(size):
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not app_icon(size).pixmap(size, size).save(buffer, "PNG"):
        raise RuntimeError("Could not render app icon.")
    return bytes(buffer.data())


def main():
    app = QApplication.instance() or QApplication([])
    directory = Path(__file__).resolve().parent
    sizes = (16, 32, 48, 64, 128, 256)
    images = [png(size) for size in sizes]
    offset = 6 + 16 * len(images)
    entries = []
    for size, image in zip(sizes, images):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    (directory / "CrateTransfer.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(images)
    )
    chunks = []
    for kind, size in ((b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128),
                       (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024)):
        image = png(size)
        chunks.append(kind + struct.pack(">I", len(image) + 8) + image)
    body = b"".join(chunks)
    (directory / "CrateTransfer.icns").write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
    app.quit()


if __name__ == "__main__":
    main()
