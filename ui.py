from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QProgressBar, 
    QHBoxLayout, QSizePolicy, QMessageBox,
    QHBoxLayout, QToolButton,
)
from PySide6.QtCore import QThread, Signal, QObject, Qt, QSize
from PySide6.QtGui import QIcon, QIntValidator
import resources_rc
from mic import MIC
import os
        
class UploadWorker(QObject):
    progress = Signal(int)
    finished = Signal(bool, str)
    start_progress = Signal()
    
    def __init__(self, firmware_path, motor_id):
        super().__init__()
        self.firmware_path = firmware_path
        self.motor_id = motor_id

    def run(self):
        try:
            mic = MIC(self.motor_id)
            mic.setup_motor()
        except Exception as e:
            self.finished.emit(False, str(e))
            return
        
        if not mic.ping():
            self.finished.emit(False, "Motor not found, try another ID.")
            mic.tear_down_motor()
            del mic
            return
        
        result = False
        first_update = True
        for upload_progress in mic.motor.upload(self.firmware_path):
            if not isinstance(upload_progress, bool):
                if first_update:
                    self.start_progress.emit()
                    first_update = False
                self.progress.emit(int(upload_progress))
            else:
                result = upload_progress
                
        mic.tear_down_motor()
        del mic
        self.finished.emit(result, "")    
        
class InfoWorker(QObject):
    finished = Signal(bool, str)
    infos = Signal(dict)
    
    def __init__(self, id):
        super().__init__()
        self.id = id
        
    def run(self):
        mic = MIC(self.id)
        
        mic.setup_motor()
        if not mic.ping():
            self.finished.emit(False, "Motor not found, try another ID.")
            mic.tear_down_motor()
            del mic
            return
        mic.tear_down_motor()
        
        retrieved_info = mic.retrieve_info()
        
        if retrieved_info is None:
            self.finished.emit(False, "Failed to retrieve information.")
        else:
            self.infos.emit(retrieved_info)  
            self.finished.emit(True, "Information retrieved successfully.") 
        
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Run Process")
        self.setWindowIcon(QIcon(":/LMX-Projects-Logo-Noir.ico"))
        
        width = 800
        height = 200
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

        id_layout.addWidget(self.id_label)
        id_layout.addWidget(self.id_input)

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

    def start_upload(self):
        id_value = self.id_input.text()
        if not self.selected_file or not id_value:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Fail to start upload")
            msg_box.setIcon(QMessageBox.Warning)
            msg_box.setText("Please select a firmware file and enter a valid ID.")
            msg_box.exec()
            return
        
        self.progress_bar.show()
        self.progress_bar.setRange(0, 0)

        self.upload_thread = QThread()
        self.worker = UploadWorker(self.selected_file, int(id_value))
        self.worker.moveToThread(self.upload_thread)

        self.worker.start_progress.connect(self.set_progress_mode)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.upload_done)
        self.worker.finished.connect(self.upload_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.upload_thread.finished.connect(self.upload_thread.deleteLater)

        self.upload_thread.started.connect(self.worker.run)
        self.upload_thread.start()
        self.update_button.setEnabled(False)
    
    def upload_done(self, success: bool, message: str = None):
        self.progress_bar.hide()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.run_button.setEnabled(True)
        self.update_button.setEnabled(True)
        
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
        self.run_button.setEnabled(False)
        
    def update_info_labels(self, retrieved_info):
        self.fw_label.setText(f"FW : {retrieved_info['fw_version']} {retrieved_info['fw_status']}")
        self.hw_label.setText(f"HW : {retrieved_info['hw_name']}")
        self.uuid_label.setText(f"UUID : {retrieved_info['UUID']}")