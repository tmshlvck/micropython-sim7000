"""
main.py

Copyright (C) 2020 Tomas Hlavacek (tmshlvck@gmail.com)

Tests / examples for sim driver

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""

import usys
import uasyncio
import machine
import uselect
import sim


# Disable PIN:
# AT+CPIN="1234"
# AT+CLCK="SC",0,"1234"

DEBUG = True

CONFIG = {
  'MQTT_APN': 'internet',
  'MQTT_CLIENTID': 'clientid',
  'MQTT_BROKER': 'mqtt.example.com',
  'MQTT_USER': 'username',
  'MQTT_PASS': 'xyzabcdefgh',

  'MODEM_POWER_PIN': 4,
  'MODEM_RESET_PIN': 5,
  'MODEM_RX_PIN': 26,
  'MODEM_TX_PIN': 27,

  'NTP_SERVER': 'ntp.nic.cz',
  }


# Utils for debugging
def d(msg):
  if DEBUG:
    print(msg)

lastasc2 = ''
async def readline(prompt=""):
  global lastasc2

  cin = uasyncio.StreamReader(usys.stdin)
  ss = uio.StringIO()
  ss.seek(0)
  ss_cur = 0

  usys.stdout.write("%s" % prompt)
  while True:
    ch = await cin.read(1)
    asc2 = ord(ch)
    if 31 < asc2 < 127: # ASCII printable characters
      ss.seek(ss_cur)
      ss.write(ch)
      ss_cur += 1
      usys.stdout.write(ch)
    #elif asc2 == 127: # DEL
    elif asc2 == 8: # BS
      ss_cur -= 1 if ss_cur > 0 else 0
      usys.stdout.write(ch)
    elif asc2 == 13 or asc2 == 10: # CR|LF
      if lastasc2 == 13 or lastasc2 == 10: # avoid duplicate LF from mpfshell
        continue
      else:
        lastasc2 = asc2

      usys.stdout.write(b'\n')
      break

    lastasc2 = asc2

  ss.seek(0)
  return ss.read(ss_cur)



 
async def shell(uplink, data):
  global DEBUG

  while True:
    l = await readline("# ")
    l = l.strip()
    try:
      if l == 'help':
        print("Help - Available commands:")
        print("  help")
        print("  exit")
        print("  status")
        print("  simd | simdebug")
        print("  debug")
        print("  undebug")
        print("  uplinkstop")
        print("  uplinkstart")
        print("  getsms")
        print("  delsms <smsid>")
        print("  sendsms <telnumber> <message>")
        print("  reset")

      elif l == 'exit':
        usys.exit(0)

      elif l == 'status':
        try:
          print("Uplink: %s" % str(await uplink.get_status()))
        except Exception as e:
          print("Uplink failed to retreive status!")
          usys.print_exception(e)

        print("Data:")
        print(str(data))

      elif l == 'data':
        print("Data:")
        print(str(data))

      elif l == 'simd' or l == 'simdebug':
        try:
          sim = SIM(CONFIG['MODEM_POWER_PIN'], CONFIG['MODEM_RESET_PIN'], CONFIG['MODEM_RX_PIN'], CONFIG['MODEM_TX_PIN'])
          await sim.init()
          print("GSM running, enter 'stop' to exit GSM shell")
          await sim.debug_shell()
        except:
          raise
        finally:
          await sim.deinit()

      elif l == 'debug':
        DEBUG = True

      elif l == 'undebug':
        DEBUG = False

      elif l == 'uplinkstop':
        print(await uplink.stop())

      elif l == 'uplinkstart':
        print(uplink.start(data))

      elif l.startswith('getsms'):
        print(await uplink.sim.get_sms())

      elif l.startswith('delsms'):
        _,smsid = l.split(' ', 1)
        print(await uplink.sim.del_sms(int(smsid)))

      elif l.startswith('sendsms'):
        _,tel,msg = l.split(' ', 2)
        print(await uplink.sim.send_sms(tel, msg))

      elif l == 'reset':
        machine.reset()
      else:
        if l != "":
          print("unknown command")
    except Exception as e:
      usys.print_exception(e)


async def main():
  print("Press ctrl-c in next 10 seconds to stop the main.py script")
  await uasyncio.sleep(10)

  print("Starting up beetle")
  uplink = sim.MQTTUplink(CONFIG)
  data = {}

  tdog = uasyncio.create_task(dog(uplink, data))

  uplink.start(data)

  tshell = uasyncio.create_task(shell(uplink, data))
    
  print("All tasks running")

  await uasyncio.gather(uplink.outt, )



uasyncio.run(main())

