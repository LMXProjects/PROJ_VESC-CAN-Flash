import pybldc
import logging
import platform
import can

class MIC():
    def __init__(self, id):
        self.id = int(id)
        
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
        
        self.logger = logging.getLogger("pybldc")
        self.logger.setLevel(logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)

        formatter = logging.Formatter('[%(levelname)s] %(message)s')
        console_handler.setFormatter(formatter)

        self.logger.addHandler(console_handler)

    def setup_motor(self):
        self.motor = pybldc.PyBldcCan(logger=self.logger, controller_id=self.id, interface=self.interface, channel=self.channel)
        
    def tear_down_motor(self):
        self.motor.shutdown()
        
    def ping(self):
        return self.motor.ping()
            
    def check_can_interface(self):
        try:
            bus = can.interface.Bus(channel=self.channel, interface=self.interface)
            bus.shutdown()
            return True
        except Exception as e:
            return False
        
    def retrieve_info(self, timeout = 5): 
        def parse_payload(payload_bytes):
            if len(payload_bytes) < 56:
                print("Payload too short to parse.")
                return None

            fw_status = None
            if payload_bytes[29] == 0x00:
                fw_status = "STABLE"
            elif payload_bytes[29] == 0x01:
                fw_status = "BETA"
            elif payload_bytes[29] == 0x02:
                fw_status = "ALPHA"

            parsed_data = {
                "fw_version": f"v{payload_bytes[2]}.{payload_bytes[3]:02d}",
                "fw_status": fw_status,
                "hw_name": ''.join([chr(b) for b in payload_bytes[4:8] + payload_bytes[9:13]]),
                "UUID": ' '.join(f"{v:02X}" for v in payload_bytes[14:16] + payload_bytes[17:24] + payload_bytes[25:28]),
            }
            
            return parsed_data
 
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
            print(f"Failed to send: {e}")
            return

        payload_bytes = []
        while True:
            msg = bus.recv(timeout)
            if msg is None:
                print("Timeout reached, no more messages.")
                break

            if msg.arbitration_id == 0x700:
                break
            elif msg.arbitration_id == 0x500:
                payload_bytes.extend(msg.data)
                
        parse_payload_result = parse_payload(payload_bytes)
        
        bus.shutdown()
        
        return parse_payload_result