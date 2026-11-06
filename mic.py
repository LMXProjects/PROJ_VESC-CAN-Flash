import pybldc
import logging
import platform
import can
import os
import sys
from datetime import datetime

LOGGER_NAME = "pybldc"

if getattr(sys, "frozen", False):
    # Packaged build (see MIC_Flash.spec, console=False -> no visible stdout/
    # stderr), so put logs next to the .exe rather than a temp extraction dir.
    _base_dir = os.path.dirname(sys.executable)
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(_base_dir, "logs")

logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.DEBUG)  # let everything through to handlers; handlers filter individually

_console_handler = None
_file_handler = None

if not logger.handlers:
    # Configured once at import time (not per-MIC-instance) so repeated
    # MIC(...) construction never piles up duplicate handlers, which used to
    # make every log line print once per handler ever added.
    #
    # Console-only, INFO level, by default -- no file handler. pybldc logs a
    # DEBUG line per CAN frame, and a firmware write is thousands of frames;
    # writing all of that to disk on every run has a real cost, and this tool
    # should stay fast for normal/production use. Pass --debug to raise the
    # console to DEBUG and start writing a full-detail log file.
    _console_handler = logging.StreamHandler()
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_console_handler)


def set_debug(enabled):
    """Toggle debug mode: raises the console to DEBUG and enables a
    full-detail file log (creating a fresh timestamped file the first time
    it's turned on). Disabling it again drops the console back to INFO and
    stops file logging. Off by default -- see the module-level comment above
    for why file logging isn't unconditional."""
    global _file_handler

    if _console_handler is not None:
        _console_handler.setLevel(logging.DEBUG if enabled else logging.INFO)

    if enabled and _file_handler is None:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            log_path = os.path.join(LOG_DIR, datetime.now().strftime("vesc_can_flash_%Y%m%d_%H%M%S.log"))
            _file_handler = logging.FileHandler(log_path, encoding="utf-8")
            _file_handler.setLevel(logging.DEBUG)
            _file_handler.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(_file_handler)
            logger.info(f"Logging to file: {log_path}")
        except OSError as e:
            logger.warning(f"Could not set up file logging in {LOG_DIR}: {e}")
    elif not enabled and _file_handler is not None:
        logger.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None


class MIC():
    def __init__(self, id):
        self.id = int(id)
        self.logger = logger

        os_name = platform.system()
        if os_name == "Windows":
            self.interface = "pcan"
            self.channel = "PCAN_USBBUS1"
        elif os_name == "Linux":
            self.interface = "socketcan"
            self.channel = 'can0'
        else:
            raise Exception("Unsupported OS: {}".format(os_name))

        if not self.check_can_interface():
            raise Exception("CAN interface not found: {}\nPlease check your PCAN connection".format(self.interface))

    def setup_motor(self):
        self.logger.debug(f"MIC[{self.id}]: setup_motor: opening CAN connection ({self.interface}/{self.channel})")
        self.motor = pybldc.PyBldcCan(logger=self.logger, controller_id=self.id, interface=self.interface, channel=self.channel)

    def tear_down_motor(self):
        self.logger.debug(f"MIC[{self.id}]: tear_down_motor: closing CAN connection")
        self.motor.shutdown()

    def ping(self, timeout=1.0):
        self.logger.debug(f"MIC[{self.id}]: ping: sending (timeout={timeout}s)")
        result = self.motor.ping(timeout=timeout)
        self.logger.debug(f"MIC[{self.id}]: ping: result={result}")
        return result


    def check_can_interface(self):
        try:
            bus = can.interface.Bus(channel=self.channel, interface=self.interface)
            bus.shutdown()
            return True
        except Exception as e:
            self.logger.warning(f"MIC: check_can_interface: {self.interface}/{self.channel} not available: {e}")
            return False

    def retrieve_info(self, timeout = 5):
        def parse_payload(payload_bytes):
            # Layout (after stripping each CAN frame's leading fragment-offset byte):
            # [0]=unused, [1]=fw major, [2]=fw minor, [3:N]=null-terminated hw name,
            # [N+1:N+13]=12-byte UUID, [N+13]=reserved, [N+14]=fw status.
            min_len = 3 + 1 + 1 + 12 + 1 + 1  # shortest possible: 1-char hw name
            if len(payload_bytes) < min_len:
                self.logger.warning(f"MIC[{self.id}]: retrieve_info: payload too short to parse ({len(payload_bytes)} bytes).")
                return None

            try:
                hw_name_end = payload_bytes.index(0x00, 3)
            except ValueError:
                self.logger.warning(f"MIC[{self.id}]: retrieve_info: malformed payload, HW name terminator not found.")
                return None

            uuid_start = hw_name_end + 1
            uuid_end = uuid_start + 12
            status_index = uuid_end + 1  # one reserved byte follows the UUID

            if len(payload_bytes) <= status_index:
                self.logger.warning(f"MIC[{self.id}]: retrieve_info: payload too short to parse ({len(payload_bytes)} bytes).")
                return None

            fw_status = None
            if payload_bytes[status_index] == 0x00:
                fw_status = "STABLE"
            elif payload_bytes[status_index] == 0x01:
                fw_status = "BETA"
            elif payload_bytes[status_index] == 0x02:
                fw_status = "ALPHA"

            parsed_data = {
                "fw_version": f"v{payload_bytes[1]}.{payload_bytes[2]:02d}",
                "fw_status": fw_status,
                "hw_name": ''.join(chr(b) for b in payload_bytes[3:hw_name_end]),
                "UUID": ' '.join(f"{v:02X}" for v in payload_bytes[uuid_start:uuid_end]),
            }

            self.logger.info(
                f"MIC[{self.id}]: retrieve_info: parsed fw={parsed_data['fw_version']} "
                f"{parsed_data['fw_status']}, hw={parsed_data['hw_name']}, uuid={parsed_data['UUID']}"
            )
            return parsed_data

        self.logger.info(f"MIC[{self.id}]: retrieve_info: requesting device info (timeout={timeout}s)")

        filters = [
            {"can_id": 0x500, "can_mask": 0x7FF, "extended": True},
            {"can_id": 0x700, "can_mask": 0x7FF, "extended": True},
        ]

        bus = can.interface.Bus(
            channel=self.channel,
            interface=self.interface,
            can_filters=filters,
        )

        command_msg = can.Message(
            arbitration_id=0x800 + self.id,
            is_extended_id=True,
            data=[0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0]
        )

        try:
            bus.send(command_msg)
        except can.CanError as e:
            self.logger.error(f"MIC[{self.id}]: retrieve_info: failed to send request: {e}")
            bus.shutdown()
            return None

        payload_bytes = bytearray()
        got_end_frame = False
        frame_count = 0
        while True:
            msg = bus.recv(timeout)
            if msg is None:
                self.logger.warning(
                    f"MIC[{self.id}]: retrieve_info: timeout waiting for response "
                    f"({frame_count} chunk frame(s) received so far)."
                )
                break

            if msg.arbitration_id == 0x700:
                got_end_frame = True
                self.logger.debug(f"MIC[{self.id}]: retrieve_info: end-of-transfer frame received")
                break
            elif msg.arbitration_id == 0x500:
                # Each frame is [fragment_offset, data...]; place the data at its
                # real offset in the reassembled buffer rather than assuming a
                # fixed number/size of frames.
                frame_count += 1
                offset = msg.data[0]
                chunk = msg.data[1:]
                end = offset + len(chunk)
                if end > len(payload_bytes):
                    payload_bytes.extend(bytes(end - len(payload_bytes)))
                payload_bytes[offset:end] = chunk
                self.logger.debug(
                    f"MIC[{self.id}]: retrieve_info: chunk #{frame_count} offset={offset} len={len(chunk)}"
                )

        self.logger.debug(
            f"MIC[{self.id}]: retrieve_info: reassembled {len(payload_bytes)} data bytes, "
            f"end_frame={got_end_frame}: {' '.join(f'{b:02X}' for b in payload_bytes)}"
        )

        parse_payload_result = parse_payload(payload_bytes)

        bus.shutdown()

        return parse_payload_result
