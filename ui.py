from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QProgressBar,
    QHBoxLayout, QSizePolicy, QMessageBox,
    QHBoxLayout, QToolButton, QCheckBox,
)
from PySide6.QtCore import QThread, Signal, QObject, Qt, QSize
from PySide6.QtGui import QIcon, QIntValidator
import resources_rc
from mic import MIC, LOGGER_NAME
import can
import logging
import os
import time

logger = logging.getLogger(LOGGER_NAME)

REBOOT_TIMEOUT_S = 60 # decompressing + flashing the final image on-device can take 40s+
REBOOT_SCAN_CONFIRM_COUNT = 3  # consecutive frames from an ID before treating it as "the device is back"
REBOOT_STATUS_INTERVAL_S = 1.0
# Same ranges ScanWorker uses: CAN_PACKET_PROCESS_SHORT_BUFFER (0x800+id) and
# CAN_PACKET_STATUS (0x900+id) both carry the controller's ID in their low byte.
REBOOT_SCAN_FILTER_RANGES = [0x800, 0x900]


class UploadWorker(QObject):
    progress = Signal(int)
    finished = Signal(bool, str)
    start_progress = Signal()
    status = Signal(str)
    infos = Signal(dict)
    id_changed = Signal(int)

    def __init__(self, firmware_path, motor_id, upload_bootloader=False, bootloader_path=""):
        super().__init__()
        self.firmware_path = firmware_path
        self.motor_id = motor_id
        self.upload_bootloader = upload_bootloader
        self.bootloader_path = bootloader_path

    def _run_upload_generator(self, generator, phase_label):
        result = False
        first_update = True
        for upload_progress in generator:
            if not isinstance(upload_progress, bool):
                if first_update:
                    self.start_progress.emit()
                    first_update = False
                self.progress.emit(int(upload_progress))
            else:
                result = upload_progress
        logger.info(f"UploadWorker[{self.motor_id}]: {phase_label} result={result}")
        return result

    def _try_get_info(self, target_id=None):
        if target_id is None:
            target_id = self.motor_id
        # mic.retrieve_info() opens its own CAN bus on the same channel, so the
        # pybldc bus from setup_motor()/ping() must be torn down first.
        try:
            mic = MIC(target_id)
            mic.setup_motor()
            try:
                pinged = mic.ping()
            finally:
                mic.tear_down_motor()
            if not pinged:
                return None
            return mic.retrieve_info()
        except Exception as e:
            logger.debug(f"UploadWorker[{self.motor_id}]: get_info({target_id}) raised {e!r}")
            return None

    def _wait_for_device(self, timeout=REBOOT_TIMEOUT_S):
        """
        A successful update can change the device's CAN ID out from under us
        (a board's app config -- e.g. its configured CAN ID -- ships as part
        of the firmware, and we've seen a board switch from ID 35 to ID 10
        once a real update actually took effect). So instead of re-pinging
        the original motor_id, passively scan for ANY device broadcasting
        again (same approach as ScanWorker), then query whichever ID
        actually responds.

        Returns (info, found_id), or (None, None) on timeout/failure.
        """
        try:
            probe = MIC(self.motor_id)
        except Exception as e:
            logger.error(f"UploadWorker[{self.motor_id}]: reboot scan: CAN interface not available: {e}")
            return None, None

        filters = [
            {"can_id": filter_id, "can_mask": 0xFF00, "extended": True}
            for filter_id in REBOOT_SCAN_FILTER_RANGES
        ]
        try:
            bus = can.interface.Bus(channel=probe.channel, interface=probe.interface, can_filters=filters)
        except Exception as e:
            logger.error(f"UploadWorker[{self.motor_id}]: reboot scan: failed to open CAN interface: {e}")
            return None, None

        start = time.monotonic()
        deadline = start + timeout
        counts = {}
        found_id = None
        last_status = 0.0
        try:
            while True:
                elapsed = time.monotonic() - start
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if elapsed - last_status >= REBOOT_STATUS_INTERVAL_S:
                    self.status.emit(f"Upload done. Waiting for device to reboot... ({elapsed:.0f}s / {timeout:.0f}s)")
                    last_status = elapsed
                msg = bus.recv(min(REBOOT_STATUS_INTERVAL_S, remaining))
                if msg is None:
                    continue
                candidate_id = msg.arbitration_id & 0xFF
                count = counts.get(candidate_id, 0) + 1
                counts[candidate_id] = count
                logger.debug(
                    f"UploadWorker[{self.motor_id}]: reboot scan: candidate ID {candidate_id} "
                    f"seen ({count}/{REBOOT_SCAN_CONFIRM_COUNT})"
                )
                if count >= REBOOT_SCAN_CONFIRM_COUNT:
                    found_id = candidate_id
                    break
        finally:
            bus.shutdown()

        if found_id is None:
            logger.error(f"UploadWorker[{self.motor_id}]: reboot scan: no device detected within {timeout}s")
            return None, None

        logger.info(f"UploadWorker[{self.motor_id}]: reboot scan: device detected at ID {found_id}")
        self.status.emit(f"Device detected at ID {found_id}. Retrieving info...")

        return self._try_get_info(found_id), found_id

    def run(self):
        logger.info(f"UploadWorker[{self.motor_id}]: starting upload of {self.firmware_path}")

        before_info = self._try_get_info()
        logger.info(f"UploadWorker[{self.motor_id}]: pre-upload info: {before_info}")

        try:
            mic = MIC(self.motor_id)
            mic.setup_motor()
        except Exception as e:
            logger.error(f"UploadWorker[{self.motor_id}]: setup failed: {e}")
            self.finished.emit(False, str(e))
            return

        if not mic.ping():
            logger.warning(f"UploadWorker[{self.motor_id}]: initial ping failed, aborting")
            self.finished.emit(False, "Motor not found, try another ID.")
            mic.tear_down_motor()
            del mic
            return

        if self.upload_bootloader:
            logger.info(f"UploadWorker[{self.motor_id}]: uploading bootloader from {self.bootloader_path}")
            self.status.emit("Uploading bootloader...")
            bootloader_result = self._run_upload_generator(
                mic.motor.upload(self.bootloader_path, is_bootloader=True),
                "bootloader upload",
            )
            if not bootloader_result:
                mic.tear_down_motor()
                del mic
                self.finished.emit(False, "Bootloader upload failed.")
                return
            self.status.emit("Bootloader uploaded. Uploading firmware...")

        logger.info(f"UploadWorker[{self.motor_id}]: writing firmware")
        result = self._run_upload_generator(mic.motor.upload(self.firmware_path), "firmware write phase")

        mic.tear_down_motor()
        del mic

        if not result:
            self.finished.emit(False, "Firmware upload failed.")
            return

        self.status.emit("Upload done. Waiting for device to reboot...")
        logger.info(f"UploadWorker[{self.motor_id}]: waiting up to {REBOOT_TIMEOUT_S}s for device to reboot")

        info, found_id = self._wait_for_device()
        if info is None:
            logger.error(f"UploadWorker[{self.motor_id}]: no device responded within {REBOOT_TIMEOUT_S}s after reboot")
            self.finished.emit(False, f"Firmware uploaded, but no device responded within {REBOOT_TIMEOUT_S}s after reboot.")
            return

        address_changed = found_id != self.motor_id
        if address_changed:
            logger.info(f"UploadWorker[{self.motor_id}]: device now responds at ID {found_id} (was {self.motor_id})")
            self.id_changed.emit(found_id)

        logger.info(f"UploadWorker[{self.motor_id}]: device responded after reboot: {info}")
        self.infos.emit(info)

        # An address change on its own proves the device booted new firmware
        # (the old firmware never answered at found_id), so only treat an
        # unchanged fw/hw as suspicious when the address stayed the same.
        if (
            not address_changed
            and before_info is not None
            and info.get("fw_version") == before_info.get("fw_version")
            and info.get("hw_name") == before_info.get("hw_name")
        ):
            logger.warning(
                f"UploadWorker[{self.motor_id}]: device responded but fw/hw unchanged "
                f"({info.get('fw_version')}, {info.get('hw_name')}) -- update likely did not apply"
            )
            self.finished.emit(
                False,
                f"Device rebooted, but firmware appears unchanged "
                f"({info.get('fw_version')}, {info.get('hw_name')}). The update likely did not apply.",
            )
            return

        self.finished.emit(True, "Firmware updated successfully. Device is back online.")


class InfoWorker(QObject):
    finished = Signal(bool, str)
    infos = Signal(dict)

    def __init__(self, id):
        super().__init__()
        self.id = id

    def run(self):
        logger.info(f"InfoWorker[{self.id}]: retrieving info")
        mic = MIC(self.id)

        mic.setup_motor()
        if not mic.ping():
            logger.warning(f"InfoWorker[{self.id}]: ping failed, aborting")
            self.finished.emit(False, "Motor not found, try another ID.")
            mic.tear_down_motor()
            del mic
            return
        mic.tear_down_motor()

        retrieved_info = mic.retrieve_info()

        if retrieved_info is None:
            logger.warning(f"InfoWorker[{self.id}]: retrieve_info failed")
            self.finished.emit(False, "Failed to retrieve information.")
        else:
            logger.info(f"InfoWorker[{self.id}]: retrieve_info succeeded")
            self.infos.emit(retrieved_info)
            self.finished.emit(True, "Information retrieved successfully.")


class ScanWorker(QObject):
    finished = Signal(list, str)

    SCAN_DURATION_S = 5.0
    CONFIRM_COUNT = 3  # stop early once one ID has been seen this many times
    # CAN_PACKET_PROCESS_SHORT_BUFFER (0x800 + controller_id) and CAN_PACKET_STATUS
    # (0x900 + controller_id) frames both carry the controller's ID in their low
    # byte, so listen passively for either and take whichever confirms first.
    FILTER_RANGES = [0x800, 0x900]
    FILTER_MASK = 0xFF00

    def run(self):
        logger.info(f"ScanWorker: starting scan (up to {self.SCAN_DURATION_S}s)")
        try:
            probe = MIC(0)
        except Exception as e:
            logger.error(f"ScanWorker: CAN interface not available: {e}")
            self.finished.emit([], f"CAN interface not available: {e}")
            return

        filters = [
            {"can_id": filter_id, "can_mask": self.FILTER_MASK, "extended": True}
            for filter_id in self.FILTER_RANGES
        ]
        try:
            bus = can.interface.Bus(channel=probe.channel, interface=probe.interface, can_filters=filters)
        except Exception as e:
            logger.error(f"ScanWorker: failed to open CAN interface: {e}")
            self.finished.emit([], f"Failed to open CAN interface: {e}")
            return

        counts = {}
        deadline = time.monotonic() + self.SCAN_DURATION_S
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                msg = bus.recv(remaining)
                if msg is None:
                    break
                candidate_id = msg.arbitration_id & 0xFF
                count = counts.get(candidate_id, 0) + 1
                counts[candidate_id] = count
                logger.debug(
                    f"ScanWorker: candidate ID {candidate_id} seen ({count}/{self.CONFIRM_COUNT}) "
                    f"via 0x{msg.arbitration_id:03X}"
                )
                if count >= self.CONFIRM_COUNT:
                    break
        finally:
            bus.shutdown()

        found_ids = set(counts)

        if found_ids:
            ids = sorted(found_ids)
            ids_str = ", ".join(str(i) for i in ids)
            logger.info(f"ScanWorker: found {ids}")
            self.finished.emit(ids, f"Found {len(ids)} device(s): {ids_str}")
        else:
            logger.warning(f"ScanWorker: no devices detected after {self.SCAN_DURATION_S}s")
            self.finished.emit([], f"No devices detected on the bus (timed out after {int(self.SCAN_DURATION_S)}s).")


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()

        def is_dark_theme():
            return self.palette().color(self.backgroundRole()).lightness() < 128

        self.setWindowTitle("Run Process")
        if is_dark_theme():
            self.setWindowIcon(QIcon(":light_icon.ico"))
        else:
            self.setWindowIcon(QIcon(":dark_icon.ico"))

        # Set the window size 
        width = 500 # Adjusted without right panel, old = 800
        height = 300
        self.resize(width, height)
        self.setMinimumSize(width, height)

        main_layout = QHBoxLayout()
  
        left_panel = QWidget()
        self.left_layout = QVBoxLayout(left_panel)

        # ID input
        id_layout = QHBoxLayout()
        self.id_label = QLabel("Enter ID:")
        self.id_input = QLineEdit()
        self.id_input.setValidator(QIntValidator())

        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.start_scan)

        id_layout.addWidget(self.id_label)
        id_layout.addWidget(self.id_input)
        id_layout.addWidget(self.scan_button)

        # File selector
        file_layout = QHBoxLayout()

        self.file_path_edit = QLineEdit()
        self.file_path_edit.setPlaceholderText("Select firmware .bin file")
        self.file_path_edit.setReadOnly(True)

        self.file_button = QToolButton()
        self.file_button.setIcon(QIcon.fromTheme("folder"))
        self.file_button.setIconSize(QSize(16, 16))
        self.file_button.setStyleSheet("QToolButton { border: none; padding: 0px; }")
        self.file_button.clicked.connect(self.select_file)

        file_layout.addWidget(self.file_path_edit)
        file_layout.addWidget(self.file_button)

        # Bootloader checkbox + selector
        self.bootloader_checkbox = QCheckBox("Upload bootloader")
        self.bootloader_checkbox.toggled.connect(self.on_bootloader_toggled)

        bootloader_layout = QHBoxLayout()
        self.bootloader_path_edit = QLineEdit()
        self.bootloader_path_edit.setPlaceholderText("Select bootloader .bin file")
        self.bootloader_path_edit.setReadOnly(True)
        self.bootloader_path_edit.setEnabled(False)

        self.bootloader_button = QToolButton()
        self.bootloader_button.setIcon(QIcon.fromTheme("folder"))
        self.bootloader_button.setIconSize(QSize(16, 16))
        self.bootloader_button.setStyleSheet("QToolButton { border: none; padding: 0px; }")
        self.bootloader_button.clicked.connect(self.select_bootloader_file)
        self.bootloader_button.setEnabled(False)

        bootloader_layout.addWidget(self.bootloader_path_edit)
        bootloader_layout.addWidget(self.bootloader_button)

        self.selected_bootloader_file = ""

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.hide()

        # Run button
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.start_upload)

        # Result label
        self.result_label = QLabel()
        self.result_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.result_label.setWordWrap(True)
        self.result_label.setAlignment(Qt.AlignCenter)
        self.result_label.setStyleSheet("QLabel { font-size: 16px; }")
        self.result_label.setText("")
        self.result_label.setVisible(False)

        # Add widgets to layout
        self.left_layout.addLayout(id_layout)
        self.left_layout.addLayout(file_layout)
        self.left_layout.addWidget(self.bootloader_checkbox)
        self.left_layout.addLayout(bootloader_layout)
        self.left_layout.addWidget(self.run_button)
        self.left_layout.addWidget(self.progress_bar)
        self.left_layout.addWidget(self.result_label)

        self.selected_file = ""


        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        self.fw_label = QLabel("FW :")
        self.hw_label = QLabel("HW :")
        self.uuid_label = QLabel("UUID :")

        self.update_button = QPushButton("Get Info")
        self.update_button.clicked.connect(self.retrieve_info_labels)

        right_layout.addWidget(self.fw_label)
        right_layout.addWidget(self.hw_label)
        right_layout.addWidget(self.uuid_label)
        right_layout.addWidget(self.update_button)
        right_layout.addStretch()

        # Add left and right panels to main layout
        main_layout.addWidget(left_panel, stretch=1)
        main_layout.addWidget(right_panel, stretch=1)

        self.setLayout(main_layout)

    def _set_controls_enabled(self, enabled: bool):
        self.run_button.setEnabled(enabled)
        self.update_button.setEnabled(enabled)
        self.scan_button.setEnabled(enabled)

    def select_file(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Select Firmware File",
            "",
            "Binary Files (*.bin);;All Files (*)"
        )
        if file_name:
            self.selected_file = file_name
            self.file_path_edit.setText(os.path.basename(file_name))

    def on_bootloader_toggled(self, checked):
        self.bootloader_path_edit.setEnabled(checked)
        self.bootloader_button.setEnabled(checked)

    def select_bootloader_file(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Select Bootloader File",
            "",
            "Binary Files (*.bin);;All Files (*)"
        )
        if file_name:
            self.selected_bootloader_file = file_name
            self.bootloader_path_edit.setText(os.path.basename(file_name))

    def start_scan(self):
        self.progress_bar.show()
        self.progress_bar.setRange(0, 0)
        self.result_label.setText("Scanning for devices...")
        self.result_label.setStyleSheet("QLabel { color: gray; }")
        self.result_label.setVisible(True)

        self.scan_thread = QThread()
        self.scan_worker = ScanWorker()
        self.scan_worker.moveToThread(self.scan_thread)

        self.scan_worker.finished.connect(self.scan_done)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)

        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_thread.start()

        self._set_controls_enabled(False)

    def scan_done(self, found_ids, message):
        self.progress_bar.hide()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self._set_controls_enabled(True)

        if found_ids:
            self.id_input.setText(str(found_ids[0]))
            self.result_label.setText(f"✅ {message}")
            self.result_label.setStyleSheet("QLabel { color: green; }")
        else:
            self.result_label.setText(f"❌ {message}")
            self.result_label.setStyleSheet("QLabel { color: red; }")
        self.result_label.setVisible(True)

    def start_upload(self):
        id_value = self.id_input.text()
        if not self.selected_file or not id_value:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Fail to start upload")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("Please select a firmware file and enter a valid ID.")
            msg_box.exec()
            return

        upload_bootloader = self.bootloader_checkbox.isChecked()
        if upload_bootloader and not self.selected_bootloader_file:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Fail to start upload")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("Please select a bootloader file, or uncheck 'Upload bootloader'.")
            msg_box.exec()
            return

        self.progress_bar.show()
        self.progress_bar.setRange(0, 0)

        self.upload_thread = QThread()
        self.worker = UploadWorker(
            self.selected_file,
            int(id_value),
            upload_bootloader=upload_bootloader,
            bootloader_path=self.selected_bootloader_file,
        )
        self.worker.moveToThread(self.upload_thread)

        self.worker.start_progress.connect(self.set_progress_mode)
        self.worker.progress.connect(self.update_progress)
        self.worker.status.connect(self.waiting_for_device)
        self.worker.infos.connect(self.update_info_labels)
        self.worker.id_changed.connect(self.on_id_changed)
        self.worker.finished.connect(self.upload_done)
        self.worker.finished.connect(self.upload_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.upload_thread.finished.connect(self.upload_thread.deleteLater)

        self.upload_thread.started.connect(self.worker.run)
        self.upload_thread.start()
        self._set_controls_enabled(False)

    def on_id_changed(self, new_id):
        self.id_input.setText(str(new_id))

    def waiting_for_device(self, message):
        self.progress_bar.setRange(0, 0)
        self.result_label.setText(message)
        self.result_label.setStyleSheet("QLabel { color: gray; }")
        self.result_label.setVisible(True)

    def upload_done(self, success: bool, message: str = None):
        self.progress_bar.hide()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self._set_controls_enabled(True)

        if success:
            if message:
                self.result_label.setText(f"✅ {message}")
                self.result_label.setStyleSheet("QLabel { color: green; }")
                self.result_label.setVisible(True)
            else:
                self.result_label.setText("✅ Firmware upload succeeded.")
                self.result_label.setStyleSheet("QLabel { color: green; }")
                self.result_label.setVisible(True)
        else:
            if message:
                self.result_label.setText(f"❌ {message}")
                self.result_label.setStyleSheet("QLabel { color: red; }")
                self.result_label.setVisible(True)
            else:
                self.result_label.setText("❌ Firmware upload failed.")
                self.result_label.setStyleSheet("QLabel { color: red; }")
                self.result_label.setVisible(True)

    def set_progress_mode(self):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def update_progress(self, value):
        self.progress_bar.setValue(value)

    def retrieve_info_labels(self):
        id_value = self.id_input.text()
        if not id_value:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Fail to start upload")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("Please enter a valid ID.")
            msg_box.exec()
            return

        self.progress_bar.show()
        self.progress_bar.setRange(0, 0)

        self.info_thread = QThread()
        self.worker = InfoWorker(id_value)
        self.worker.moveToThread(self.info_thread)

        self.worker.finished.connect(self.upload_done)
        self.worker.infos.connect(self.update_info_labels)
        self.worker.finished.connect(self.info_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.info_thread.finished.connect(self.info_thread.deleteLater)

        self.info_thread.started.connect(self.worker.run)
        self.info_thread.start()
        self._set_controls_enabled(False)

    def update_info_labels(self, retrieved_info):
        self.fw_label.setText(f"FW : {retrieved_info['fw_version']} {retrieved_info['fw_status']}")
        self.hw_label.setText(f"HW : {retrieved_info['hw_name']}")
        self.uuid_label.setText(f"UUID : {retrieved_info['UUID']}")