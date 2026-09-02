import datetime
import os
import yaml
from serial.tools import list_ports
from zmqhelper import ZMQServiceBase, Client
from motors import RotationController, RotationControllerELL, LinearController, LCController
from datetime import datetime


class MotorAPTZMQService(ZMQServiceBase):
    """
    ZMQServiceBase does:
      - binds a ROUTER socket for clients (tcp://*:55000)
      - binds a DEALER socket for workers (inproc://workers)
      - starts N worker threads that call self.process_request
      - proxies ROUTER <> DEALER
    """
    def __init__(self, config_file, n_workers=6):
        # global logger
        
        self.config_file = config_file
        self.config_dir  = './config/'
        self.config = self.load_config()
        self.time_start = datetime.now()
        
        cParams = self.config['config_setup']
        if 'redis_host' not in cParams or cParams['register_redis'] is False:
            cParams['redis_host'] = None 
        if 'loki_host' not in cParams:
            cParams['loki_host'] = None
        if 'redis_port' not in cParams:
            cParams['redis_port'] = None
        if 'loki_port' not in cParams:
            cParams['loki_port'] = None
        
        super().__init__(rep_port = cParams['req_port'], 
            n_workers= n_workers,
            http_port = cParams['http_port'],
            loki_host = cParams['loki_host'],
            loki_port = cParams['loki_port'],
            redis_host = cParams['redis_host'],
            redis_port = cParams['redis_port'],
            service_name = cParams['service_name']
        )

        # these will be populated in setup()
        self._motor = []
        self._lcc   = []
        self.setup()

        self.logger.info("")
        self.logger.info(f'{self.service_name} APT motor server Started at {self.time_start}')
        self.logger.info(f"Config: {self.config}")
        
    def load_config(self):
        """Load YAML config file."""
        cfg_path = os.path.join(self.config_dir, self.config_file)
        print(f"Loading config from {cfg_path}")
        with open(cfg_path, 'r') as f:
            return yaml.safe_load(f)
        
    def setup(self):
        """Load YAML config & initialize motor + LCController lists."""
        config = self.load_config()
        motor_dicts = config['motors']
        try:
            for m in motor_dicts:
                t = m.get('type','').lower()
                if t == 'rotational':
                    self._motor.append(RotationController(m))
                elif t == 'rotationalell':
                    self._motor.append(RotationControllerELL(m))
                elif t == 'linear':
                    self._motor.append(LinearController(m))
                elif t == 'lccontroller':
                    port = self.find_com('thorlabs lcc25')
                    self._lcc.append(LCController(port))
        except Exception as e:
            self.logger.error(f"Error setting up motors: {e}")
            raise RuntimeError(f"Failed to initialize motors: {e}")

    def handle_request(self, message: str) -> str:
        """
        Called in each worker thread.
        """
        try:
            parts = message.split()
            cmd   = parts[0].lower()
            self.logger.debug(f"Received command: {message}")

            # ping
            if cmd == 'test':
                self.logger.debug("Ping received")
                return "Connected"

            # APT list
            if cmd == 'apt':
                names = ','.join(m.attributes['name'] for m in self._motor) + ','
                self.logger.debug(f"List of APT motors: {names}")
                return names

            # motor commands
            if cmd in ('for','back','goto','getpos','getapos','home','done'):
                # interpret args
                if cmd in ('for','back','goto'):
                    distance = float(parts[1])
                    idx      = int(parts[2])
                    if cmd == 'for':
                        resp = self._motor[idx].mRel(distance)
                    elif cmd == 'back':
                        resp = self._motor[idx].mRel(-distance)
                    else:  # goto
                        resp = self._motor[idx].mAbs(distance)
                    self.logger.debug(f"Motor command: {cmd} {distance} on motor {idx}")

                elif cmd == 'getpos':
                    idx  = int(parts[1])
                    resp = self._motor[idx].getPos()
                    self.logger.debug(f"Get position command: {idx} -> {resp}")
                elif cmd == 'getapos':
                    idx  = int(parts[1])
                    resp = self._motor[idx].getAPos()
                    self.logger.debug(f"Get absolute position command: {idx} -> {resp}")
                elif cmd == 'home':
                    idx  = int(parts[1])
                    error = self._motor[idx].mHome()
                    resp = 'Homed Motor' if error == 'Success' else error
                    self.logger.debug(f"Home command: {idx}")
                else: # done
                    idx  = int(parts[1])
                    self._motor[idx].cleanUpAPT()
                    resp = "Cleaned"
                    self.logger.debug(f"Done command: {idx}")

                return str(resp)

            # LCController commands
            if cmd.startswith('lc'):
                # dispatch on the remainder of the command
                lc_cmd = cmd[2:]
                args   = [p.decode() for p in parts[1:]]
                lcc    = self._lcc[0]  # assuming single LCController
                method = {
                    'id':        lcc.get_name,
                    'setvolt':   lambda: lcc.set_voltage(int(args[0]), float(args[1])),
                    'getvolt':   lambda: lcc.get_voltage(int(args[0])),
                    'setfreq':   lambda: lcc.set_frequency(float(args[0])),
                    'getfreq':   lambda: lcc.get_frequency(),
                    'setoutmod': lambda: lcc.set_out_mode(int(args[0])),
                    'getoutmod': lambda: lcc.get_out_mode(),
                    'setoutenable': lambda: lcc.set_out_enable(int(args[0])),
                    'getoutenable': lambda: lcc.get_out_enable(),
                    'setextmod': lambda: lcc.set_ext_mod(int(args[0])),
                    'getextmod': lambda: lcc.get_ext_mod(),
                    'setpreset': lambda: lcc.set_preset(int(args[0])),
                    'getpreset': lambda: lcc.get_preset(int(args[0])),
                    'saveparams': lambda: lcc.save_params(),
                    'restore':    lambda: lcc.restore_def_params(),
                    'setdwelltime': lambda: lcc.set_test_dwell_time(float(args[0])),
                    'getdwelltime': lambda: lcc.get_test_dwell_time(),
                    'setinc':     lambda: lcc.set_test_inc(float(args[0])),
                    'gettestinc': lambda: lcc.get_test_inc(),
                    'settestvolt':lambda: lcc.set_test_volt(args[0], float(args[1])),
                    'gettestvolt':lambda: lcc.get_test_volt(),
                    'runtestmod': lambda: lcc.run_test_mode(),
                    'remtog':     lambda: lcc.remote_toggle(int(args[0])),
                }.get(lc_cmd)
                self.logger.debug(f"LC command: {lc_cmd} with args {args}")

                if method is None:
                    raise ValueError(f"Unknown LC command ‘{cmd}’")
                resp = method()
                return str(resp)

            # unknown
            return "Invalid Command"

        except Exception as e:
            err = f"Error: {e}"
            print(err)
            return err

    @staticmethod
    def find_com(substring: str) -> str:
        """Helper to locate a Thorlabs LCC on a serial port."""
        for port in list_ports.comports():
            if substring.lower() in port[1].lower():
                return port.device
        raise RuntimeError(f"Could not find device matching {substring}")

if __name__ == '__main__':
    config_file = 'apt_motors.yaml'
    service = MotorAPTZMQService(config_file)
    service.start()  # block until keyboard interrupt

