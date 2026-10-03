'''
Example for Pico running CircuitPython 10.0.3. Reads and prints onboard CPU temp to shell.
'''

import microcontroller

print(f'Onboard CPU temp: {microcontroller.cpu.temperature}*C')
