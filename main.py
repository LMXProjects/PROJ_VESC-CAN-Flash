import argparse
import sys

from PySide6.QtWidgets import QApplication

from mic import set_debug
from ui import MainWindow

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VESC CAN Flash Tool")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show verbose debug-level logs in the console and write a full-detail log file. Off by default for speed.",
    )
    args = parser.parse_args()

    set_debug(args.debug)

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
