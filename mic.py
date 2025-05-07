import pybldc
import logging
import platform
import can

class MIC():
    def __init__(self, id):
        self.id = id
        
        os_name = platform.system()
        if os_name == "Windows":
            self.interface = "pcan"
            self.channel = "PCAN_USBBUS1"
        elif os_name == "Linux":
            self.interface = "socketcan"
            self.channel = 'can0'
        else:
            raise Exception("Unsupported OS: {}".format(os_name))    
        
        if not self.check_can_interface(self.interface, self.channel):
            raise Exception("CAN interface not found: {} {}\nPlease check your PCAN connection".format(self.interface, self.channel))        
        
        self.logger = logging.getLogger("pybldc")
        self.logger.setLevel(logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter('[%(levelname)s] %(message)s')
        console_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)

        self.motor = pybldc.PyBldcCan(logger=self.logger, controller_id=self.id, interface=self.interface, channel=self.channel)
        
    def ping(self):
        return self.motor.ping()
    
    def upload(self, firmware_path, progress_callback=None, finished_callback=None):
        self.logger.info("VESC found, flashing firmware...")
        
        result = False
        for upload_progress in self.motor.upload(
            firmware_path,
            timeout=5.0,
            ping_repeat=3,
            is_bootloader=False,
        ):
            if not isinstance(upload_progress, bool):
                if progress_callback:
                    progress_callback(int(upload_progress))
            else:
                result = upload_progress
                if finished_callback:
                    finished_callback(result)
                
        if result is True:
            self.logger.info("Uploading succeeded")
        else:
            self.logger.error("Uploading failed")
            exit(1)
            
    def check_can_interface(self, interface, channel):
        try:
            bus = can.interface.Bus(channel=channel, interface=interface)
            bus.shutdown()
            return True
        except Exception as e:
            return False