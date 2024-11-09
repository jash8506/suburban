import minimalmodbus as mb
import influxdb
import time
import struct
import threading
import datetime

time.sleep(30)
meter=mb.Instrument('/dev/ttyUSB0',1,mode='rtu')
meter.serial.baudrate=9600
meter.serial.timeout=0.1
db_client=influxdb.InfluxDBClient('localhost', database='dwelling')

def log_to_influx(meter, db_client):
    threading.Timer(1.0, log_to_influx, args=[meter, db_client]).start()
    try:
        reg_list=meter.read_registers(0,32, functioncode=4)
        v,i,w,va,var,pf = [struct.unpack('>f', struct.pack('>HH', *reg_list[a:a+2]))[0] for a in range(0,31,6)]
        reg_list=meter.read_registers(70,10,functioncode=4)
        hz,kwh_imp,kwh_exp,kvarh_imp,kvarh_exp = [struct.unpack('>f', struct.pack('>HH', *reg_list[a:a+2]))[0] for a in range(0,9,2)] 
        json_body = [
            {
                "measurement": "grid",
                "time": datetime.datetime.utcnow().isoformat()[:-7]+'Z',
                "fields": {
                    "V": v,
                    "I": i,
                    "W": w,
                    "VA": va,
                    "VAR": var,
                    "PF": pf,
                    "Hz": hz,
                }
            },
        ]
        db_client.write_points(json_body)
    except ValueError as e:
        print e
        meter.flushInput()
        meter.flushOutput()            
    finally:
        pass

log_to_influx(meter, db_client)
